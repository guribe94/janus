"""janus land and janus commit-state.

land is the one place a unit's work reaches the landing branch, on the host that owns the unit's worktree (its own git, its own
credentials: a unit on another host is landed there, over ssh, never by shipping its tree to the coordinator): merge the branch's tip
into the unit's branch (a conflict stops it and names the files), squash to one commit whose parent is the tip that was merged,
and move the landing branch to it only if the branch still is where the merge looked (expected ref: update-ref with the old
value, or a fast-forward only, and with --push a plain non-forced push). When the branch moved meanwhile it merges the new tip
and tries again, at most LAND_TRIES times. No gate and no test run happens here: tests are the project's own explicit commands
(janus run). A merge that is clean does not prove the merged tree is correct; land says when it integrated moved work."""
import re, shlex
from pathlib import Path
from janus.actions.backlog import checklist_set
from janus.actions.swarm import write_index
from janus.backlog import unit_items
from janus.beads import Beads
from janus.branch import landing_branch, unapplied_patch
from janus.context import Err, hrun, now, qp, say, sh, ts
from janus.rigor import review_gap
from janus.units import all_units, own_tree

LAND_TRIES = 3

class Where:
    """A worktree on a host that is not this one: git commands run there over ssh."""
    def __init__(self, h, path): self.h, self.path = h, str(path)
    def at(self, path): return Where(self.h, path)
    def __str__(self): return self.path

def at(wt, path): return wt.at(path) if isinstance(wt, Where) else path

def git(wt, *a, timeout=120, inp=None):
    if isinstance(wt, Where): return hrun(wt.h, "git -C " + qp(wt.path) + " " + " ".join(shlex.quote(str(x)) for x in a), timeout=timeout, inp=inp)
    return sh(["git", "-C", str(wt), *a], timeout=timeout, inp=inp)

def follow_upstream(wt, br, dry=False):
    """Keep the landing branch here at its upstream: fetch it and fast-forward br (move_local: ff-only in its checkout, else
    update-ref), so what is judged landed here is what is published, and new units are cut from the current tip. The one
    place for it: the orchestrator is refused `git merge` by hand (hooks GIT_MUT), and the branch lagged 67 commits once units
    landed on their own hosts. (lag, error text or None); lag 0 when it has no upstream or is current. dry: nothing moves."""
    rcu, ups, _ = git(wt, "rev-parse", "--abbrev-ref", f"{br}@{{upstream}}"); ups = ups.strip()
    if rcu or "/" not in ups: return 0, None
    if not dry and git(wt, "fetch", "-q", ups.split("/", 1)[0], timeout=120)[0]: say(f"note: git fetch {ups.split('/', 1)[0]} failed; comparing with {ups} as last fetched")
    lag = int(git(wt, "rev-list", "--count", f"{br}..{ups}")[1].strip() or 0)
    if not lag or dry: return lag, None
    return lag, move_local(wt, br, git(wt, "rev-parse", ups)[1].strip(), tip(wt, br))

def contained(wt, br):
    """True when every file the branch changed since it left br has br's tip content (squash-landed already: nothing to land)."""
    mb = git(wt, "merge-base", "HEAD", br)[1].strip()
    if not mb: return False
    fs = [f for f in git(wt, "diff", "--name-only", mb, "HEAD")[1].splitlines() if f]
    return bool(fs) and git(wt, "diff", "--quiet", br, "HEAD", "--", *fs)[0] == 0

def move_local(wt, br, new, base):
    """Move the landing branch here from base to new: git merge --ff-only in its checkout when it is checked out (that checkout's
    files follow), else update-ref with the old value. The error text, or None. Either way it fails when the branch moved."""
    wl = git(wt, "worktree", "list", "--porcelain")[1]
    co = next((blk.split("\n")[0][9:] for blk in wl.split("\n\n") if f"branch refs/heads/{br}" in blk), None)
    if co:
        rc, o, e = git(at(wt, co), "merge", "--ff-only", new, timeout=300)
        return f"{co} (where {br} is checked out) did not take the landing commit {new[:9]} fast-forward: {(e or o).strip()[-160:]}" if rc else None
    rc, o, e = git(wt, "update-ref", f"refs/heads/{br}", new, base)
    return f"update-ref {br} failed: {e.strip()[:120]}" if rc else None

def tip(wt, br): return git(wt, "rev-parse", br)[1].strip()

def merge_state(wt, br):
    """(rc, conflicted paths) of merging br into HEAD: git merge-tree, else a trial merge that is always aborted."""
    rc, o, e = git(wt, "merge-tree", "--write-tree", "--name-only", "HEAD", br)
    if rc in (0, 1): return rc, [l for l in o.split("\n\n")[0].splitlines()[1:] if l.strip()] if rc else []
    rc2, o2, e2 = git(wt, "merge", "--no-commit", "--no-ff", br, timeout=600)   # git before 2.38 has no merge-tree --write-tree
    cf = git(wt, "diff", "--name-only", "--diff-filter=U")[1].split(); git(wt, "merge", "--abort")
    if rc2 and not cf: raise Err(f"git merge-tree and a trial merge failed: {(e or o).strip()[:80]}; {(e2 or o2).strip()[:80]}")
    return (1, cf) if cf else (0, [])

def cmd_land(cfg, a):
    """janus land <unit> [--push]: see the module text. --dry-run checks the merge and says what it would do, changing nothing."""
    ld = cfg.c.get("landing", {})
    u = next((x for x in Beads(cfg).units() if x["name"] == a.unit), None) or all_units(cfg).get(a.unit)
    if not u: raise Err(f"no unit {a.unit}")
    gap = review_gap(cfg, u)   # the unit's rigor level may require reviews first (rigor.R); below careful it never does
    if gap and not getattr(a, "force_review", False): raise Err(f"refused: {gap}; janus land {a.unit} --force-review lands it anyway")
    hn = getattr(a, "host", None) or (u.get("host") if u.get("rcwd") and not cfg.hosts.get(u.get("host"), {}).get("local") and u.get("host") in cfg.hosts else None)
    if hn and not cfg.hosts.get(hn, {}).get("local"):   # the unit's tree is on another host: its git, there
        if hn not in cfg.hosts: raise Err(f"unknown host {hn}")
        wt = Where(cfg.hosts[hn], u.get("rcwd") or u.get("cwd")); say(f"landing {a.unit} on {hn} ({wt}): merge, squash and push happen there")
        if git(wt, "rev-parse", "--git-dir")[0]: raise Err(f"{wt} on {hn} is not a git worktree (a unit launched before copies carried their repository): land the collected patch here instead (janus collect, git apply --3way)")
    else:
        wt = Path(u.get("cwd", "/"))
        if not own_tree(cfg, u, [x for x in Beads(cfg).units()]) or not wt.is_dir(): raise Err(f"{a.unit} has no worktree of its own ({wt}): nothing to land")
        up = unapplied_patch(cfg, u)
        if up: raise Err(f"{a.unit}'s collected patch {up} is not applied in {wt} (the unit ran on a host without the repository): "
                         f"git -C {wt} apply --3way {up}, commit, then janus land {a.unit} again; do not close it before (its work exists only in that patch)")
    rc, branch, e = git(wt, "symbolic-ref", "--short", "HEAD"); branch = branch.strip()
    if rc: raise Err(f"{wt} is not on a branch")
    br = ld.get("branch") or ((u.get("repo") or {}).get("landing") or "main" if isinstance(wt, Where) else landing_branch(cfg, wt))   # a remote copy: the branch its seed was cut from
    if branch == br: raise Err(f"{wt} is on {br} itself")
    if git(wt, "status", "--porcelain", "--untracked-files=no")[1].strip(): raise Err(f"{wt} has uncommitted changes: commit them first")
    if git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0: raise Err(f"{wt} has a merge in progress (MERGE_HEAD): finish or abort it")
    push = a.push or bool(ld.get("push")); rem = cfg.c.get("git", {}).get("remote") or "origin"; integrated = 0
    for attempt in range(1, LAND_TRIES + 1):
        base = tip(wt, br)
        if not base: raise Err(f"no branch {br}")
        lag, err = follow_upstream(wt, br, cfg.run.dry)   # the landing branch here may lag its upstream (already pushed): fast-forwarded, never landed on stale
        if err: raise Err(f"{br} here is {lag} commit(s) behind its upstream (already pushed) and did not fast-forward: {err}; then janus land {a.unit} again")
        if lag: say(f"{br} here was {lag} commit(s) behind its upstream: fast-forwarded" if not cfg.run.dry else f"+ fast-forward {br} ({lag} behind its upstream)"); base = tip(wt, br)
        ahead = int(git(wt, "rev-list", "--count", f"{br}..HEAD")[1].strip() or 0); behind = int(git(wt, "rev-list", "--count", f"HEAD..{br}")[1].strip() or 0)
        if not ahead: raise Err(f"{branch} has no commits over {br}: nothing to land")
        if contained(wt, br):
            if retry_writeback(cfg, u, wt, rem, br): return 0   # a landing whose accounting failed: finished now, never landed again
            raise Err(f"{a.unit} is already landed: {br} holds its tree ({branch} has no change since): janus close {a.unit}")
        say(f"land {a.unit}: {branch} in {wt}, {ahead} commit(s) ahead of {br} ({base[:9]}), {behind} behind" + (f" [attempt {attempt}/{LAND_TRIES}]" if attempt > 1 else ""))
        rc, files = merge_state(wt, br)
        if rc == 1:
            say(f"STOP: merging {br} into {branch} conflicts in {len(files)} file(s): {', '.join(files[:8])}")
            say(f"resolve in {wt}: git merge {br}, fix, commit; then janus land {a.unit} again (the tree is left as it was)"); return 3
        if cfg.run.dry: say(f"+ merge {br} (clean, {behind} behind); commit-tree -p {base[:9]}; move {br} (expected {base[:9]}); {ahead} commit(s) squashed" + ("; push, not forced" if push else "")); return 0
        if behind:
            rc, o, e = git(wt, "merge", "--no-edit", br, timeout=600)
            if rc: git(wt, "merge", "--abort"); raise Err(f"git merge {br} failed although merge-tree was clean: {(e or o).strip()[-160:]}; aborted")
            say(f"merged {br} into {branch}"); integrated += 1
        tree = git(wt, "rev-parse", "HEAD^{tree}")[1].strip(); source = git(wt, "rev-parse", "HEAD")[1].strip()
        subj = git(wt, "log", "--format=%s", f"{base}..HEAD", "--no-merges")[1].splitlines()
        msg = f"{a.unit}: land {branch} ({len(subj)} commit(s), squashed)\n\n" + "\n".join(f"- {s}" for s in subj[:40]) + "\n"
        rc, new, e = git(wt, "commit-tree", tree, "-p", base, "-F", "-", inp=msg)
        if rc: raise Err(f"git commit-tree failed: {e.strip()[:120]}")
        new = new.strip(); pushed = False
        if push:   # plain push, never forced: the remote refuses it unless it still holds what the commit was built on
            rc, o, e = git(wt, "push", "-q", rem, f"{new}:refs/heads/{br}", timeout=300)
            if rc:
                say(f"push refused ({(e or o).strip()[-100:]}): {br} moved on {rem}; fetching and merging the new tip")
                git(wt, "fetch", "-q", rem, timeout=120); upref = f"{rem}/{br}"
                if git(wt, "merge-base", "--is-ancestor", br, upref)[0] == 0 and git(wt, "rev-parse", upref)[1].strip() != base:
                    err = move_local(wt, br, git(wt, "rev-parse", upref)[1].strip(), base)
                    if err: raise Err(err)
                    continue
                raise Err(f"NOT PUBLISHED: push of {new[:9]} to {rem} was refused ({(e or o).strip()[-100:]}) and {br} did not move there or here. The integrated result "
                          f"stays on {branch} in {wt}; fix the cause (access, protection, a moved {br} that {br} here cannot follow), then janus land {a.unit} --push again")
            pushed = True
        err = move_local(wt, br, new, base)
        if err and not pushed:
            if attempt < LAND_TRIES: say(f"{br} moved while {a.unit} was merged: merging the new tip"); continue
            raise Err(f"{br} kept moving: {LAND_TRIES} attempts; run janus land {a.unit} again when it settles")
        if err: say(f"note: pushed to {rem}, but {br} here did not follow ({err}): fetch, then fast-forward it")
        say(f"landed {a.unit}: {br} {base[:9]} -> {new[:9]} ({len(subj)} commit(s) squashed)" + (f"; pushed to {rem}" if pushed else "; not pushed") + f"; close is separate (janus close {a.unit})")
        if isinstance(wt, Where) and not pushed: say(f"note: {br} moved only in {wt} on {hn}: publish with `janus land {a.unit} --push` (the host needs access to {rem}), or fetch it: git fetch ssh://{wt.h['ssh']}{wt.path} {br}")
        if integrated: say(f"note: {br} had moved, so this integrated other work; the merge was clean, which does not prove the merged tree is right: run the tests that matter (janus run) if they do")
        if u.get("id"): Beads(cfg).comment(u["id"], f"{ts()} landed on {br} as {new[:9]}")
        if pushed: record_publication(cfg, u, wt, rem, br, new, source)
        elif not push: say(f"note: not published, so {a.unit}'s checklist item gets no evidence: that needs a verified push (janus land {a.unit} --push)")
        return 0
    raise Err(f"{br} kept moving: {LAND_TRIES} attempts; run janus land {a.unit} again when it settles")

def on_remote(wt, rem, br, commit):
    """Whether commit is in rem's br as fetched now (only FETCH_HEAD is written): True, False, or None when it cannot be told."""
    if git(wt, "fetch", "-q", rem, f"refs/heads/{br}", timeout=120)[0]: return None
    return {0: True, 1: False}.get(git(wt, "merge-base", "--is-ancestor", commit, "FETCH_HEAD")[0])

def save_unit(cfg, u, **kv):
    """Set kv on the unit's record as it is now: read again under the writer lock (held only for that), so what another command
    changed meanwhile is kept. An open unit, else the latest closed record of its id."""
    if cfg.run.dry or not u.get("id"): u.update(kv); return
    with cfg.writer():   # the writer lock itself (an agent's call too: command_writer takes none for it); local work only, never ssh
        bd = Beads(cfg); cur = next((x for x in bd.units() if x["id"] == u["id"]), None)
        if cur is None: cfg.run.all_units = None; cur = next((x for x in all_units(cfg).values() if x.get("id") == u["id"]), None)
        if cur is None: raise Err(f"{u['name']}: its record ({u['id']}) cannot be read now")
        cur.update(kv); bd.save(cur)
    u.update(kv)

def record_publication(cfg, u, wt, rem, br, new, source):
    """After a push: fetch rem's br again and require new in it; then the publication (source HEAD, published commit, remote and
    its url, branch) on the unit's record, then the checklist evidence (land_writeback). Git runs before the lock is taken."""
    ok = on_remote(wt, rem, br, new)
    if not ok:
        say(f"WARNING: pushed {new[:9]}, but {rem}/{br} {'could not be fetched' if ok is None else 'does not hold it'} now: no publication recorded and no checklist "
            f"evidence written; janus land {u['name']} --push again verifies it (nothing is landed twice)"); return
    url = git(wt, "remote", "get-url", rem)[1].strip()
    rc, gd, _ = git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir"); gd = Path(gd.strip())   # the repository, kept for a check after the worktree is gone
    repo = str(gd.parent if gd.name == ".git" else gd) if rc == 0 and not isinstance(wt, Where) and gd.is_absolute() else None
    pub = {"source": source, "commit": new, "remote": rem, **({"url": url} if url else {}), **({"repo": repo} if repo else {}), "branch": br, "at": int(now()), "writeback": "pending"}
    try: save_unit(cfg, u, published=pub)
    except Err as e:
        say(f"WARNING: {new[:9]} is published, but the record of it was not saved ({e}): janus land {u['name']} --push again records it (nothing is landed twice)"); return
    say(f"published: {new[:9]} is on {rem}/{br} (verified now); recorded on {u['name']}")
    land_writeback(cfg, u, pub)

def land_writeback(cfg, u, pub):
    """The linked item's evidence cell gets the published sha; its status stays (a landing is partial work, not the item's
    acceptance: janus backlog set <item> status=DONE --unit <unit>). The outcome is recorded on the unit (writeback done, or
    incomplete with why): janus land <unit> again retries an incomplete one without landing again."""
    items = sorted(unit_items(cfg, [u])); sha = pub["commit"][:12]
    if cfg.run.dry: say(f"+ checklist {', '.join(items) or '(no item)'}: evidence {sha}"); return
    if not items: res = "done"; say(f"note: {u['name']} names no backlog item (--item or ITEM:): no checklist evidence to write")
    else:
        try:
            with cfg.writer(): did = [checklist_set(cfg, i, evidence=sha) for i in items]   # short: a read and a rename of the file
            res = "done"; say(f"checklist: {', '.join(items)}: " + ("evidence " + sha if any(did) else f"evidence {sha} already there") + "; status unchanged")
        except (Err, OSError) as e:
            res = f"incomplete: {e}"[:300]
            say(f"WARNING: checklist writeback incomplete for {', '.join(items)} ({e}): the landing is published; janus land {u['name']} again retries the writeback (nothing is landed again)")
    try: save_unit(cfg, u, published={**pub, "writeback": res})
    except Err as e: say(f"WARNING: the writeback state of {u['name']} was not recorded ({e})")

def retry_writeback(cfg, u, wt, rem, br):
    """janus land on a unit already landed: True when it finished the accounting a failure left (a publication not recorded, a
    checklist writeback incomplete), after verifying the publication on the remote now; False when there is nothing to finish.
    A publication not recorded is found by the landing commit's subject on the fetched branch, and taken only when the branch's
    files have that commit's content."""
    pub = u.get("published") or {}
    if pub.get("commit") and pub.get("writeback") == "done": return False
    if cfg.run.dry: say(f"+ {u['name']} is landed: verify its publication on {rem}/{br}, then retry the checklist writeback"); return True
    if not pub.get("commit"):
        if git(wt, "fetch", "-q", rem, f"refs/heads/{br}", timeout=120)[0]: return False
        c = git(wt, "log", "-1", "--format=%H", f"--grep=^{re.escape(u['name'])}: land ", "FETCH_HEAD")[1].strip()
        mb = git(wt, "merge-base", "HEAD", c)[1].strip() if c else ""
        fs = [f for f in git(wt, "diff", "--name-only", mb, "HEAD")[1].splitlines() if f] if mb else []
        if not fs or git(wt, "diff", "--quiet", c, "HEAD", "--", *fs)[0]: return False
        say(f"{u['name']} is landed and published as {c[:9]} on {rem}/{br}, but it was not recorded: recording it now (nothing is landed again)")
        record_publication(cfg, u, wt, rem, br, c, git(wt, "rev-parse", "HEAD")[1].strip()); return True
    ok = on_remote(wt, rem, pub.get("branch") or br, pub["commit"])
    if not ok: raise Err(f"{u['name']} is landed, but its publication {pub['commit'][:12]} {'cannot be verified' if ok is None else 'is not'} on {rem}/{pub.get('branch') or br} now: "
                         "its checklist writeback stays incomplete (nothing was written); try again when the remote answers")
    say(f"{u['name']} is already landed and published as {pub['commit'][:12]}: retrying the checklist writeback (nothing is landed again)")
    land_writeback(cfg, u, pub); return True

SECRETS = [re.compile(x.encode()) for x in ("sk" + "-ant-[A-Za-z0-9_-]{20,}", "sk" + "-or-[A-Za-z0-9_-]{20,}", "sk" + "-proj-[A-Za-z0-9_-]{20,}",
    "\\bsk" + "-[a-f0-9]{32,}\\b", "AK" + "IA[0-9A-Z]{16}", "gh" + "p_[A-Za-z0-9_-]{20,}", "xo" + "x[A-Za-z0-9_-]{10,}",
    # a real PEM block only: the dashed BEGIN line, then a base64 line (a mention of the words in prose is not a key)
    "-----" + "BEGIN( [A-Z0-9]+)* PRIV" + "ATE KEY-----\\r?\\n[A-Za-z0-9+/=]{16,}\\r?$")]

SECRETS[-1] = re.compile(SECRETS[-1].pattern, re.M)

def cmd_commit_state(cfg, a):
    """Stage everything, scan exactly what is staged (L9), commit and push; unstage again if a secret is found."""
    if cfg.run.dry: say("+ janus index; git add -A; scan staged files; git commit; git push"); return
    write_index(cfg)   # out/INDEX.md is committed with the state (janus-swarm item 3)
    rc, o, e = sh(["git", "add", "-A"], cwd=cfg.root, timeout=120)
    for _ in range(3):   # item 7: a file vanished between listing and add (a monitor deleted a temp file); retry, then carry on
        if not rc: break
        msg = (e or o).strip()
        if not re.search(r"unable to stat|No such file|did not match|does not exist", msg):
            raise Err(f"git add failed: {msg[-200:]}")   # finding 11: a file that fails to add every time is an error, not a note
        rc, o, e = sh(["git", "add", "-A", "--ignore-errors"], cwd=cfg.root, timeout=120)
    if rc:   # finding 10: a SECOND vanished file is not an error; only a real add failure is
        msg = (e or o).strip()
        if not re.search(r"unable to stat|No such file|did not match|does not exist", msg): raise Err(f"git add failed: {msg[-200:]}")
    rc, o, e = sh(["git", "diff", "--cached", "--name-only", "-z"], cwd=cfg.root)
    paths = [x for x in o.split("\0") if x]
    hit = []
    for x in paths:
        try: b = (cfg.root / x).read_bytes()
        except OSError: continue
        if any(r.search(b) for r in SECRETS): hit.append(x)
    if hit: sh(["git", "reset", "-q"], cwd=cfg.root); raise Err("refused (unstaged again), possible secrets in: " + ", ".join(hit))
    if not paths: say("nothing to commit"); return
    for cmd in (["git", "commit", "-m", a.message], push_cmd(cfg.root, cfg)):
        rc, o, e = sh(cmd, cwd=cfg.root, timeout=120)
        if rc: raise Err(f"{shlex.join(cmd[:2])} failed: {(e or o).strip()[-200:]}")
    say(f"committed {len(paths)} path(s) and pushed")

def push_cmd(repo, cfg=None):
    """git push; on a branch without an upstream, push --set-upstream to [git] remote (default origin, else the only remote),
    so the next check can count unpushed commits (janus-fixes7)."""
    if sh(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "@{u}"])[0] == 0: return ["git", "push"]
    rem = ((cfg.c.get("git", {}) if cfg else {}).get("remote")) or ""
    if not rem:
        rs = sh(["git", "-C", str(repo), "remote"])[1].split()
        rem = "origin" if "origin" in rs or not rs else rs[0]
    return ["git", "push", "--set-upstream", rem, "HEAD"]
