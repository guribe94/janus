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
from janus.actions.swarm import write_index
from janus.beads import Beads
from janus.branch import landing_branch, unapplied_patch
from janus.context import Err, hrun, qp, say, sh, ts
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
        rcu, ups, _ = git(wt, "rev-parse", "--abbrev-ref", f"{br}@{{upstream}}"); ups = ups.strip()
        if rcu == 0 and "/" in ups:   # the landing branch here may lag its upstream: landing on it would rewrite what is pushed
            if not cfg.run.dry and git(wt, "fetch", "-q", ups.split("/", 1)[0], timeout=120)[0]: say(f"note: git fetch {ups.split('/', 1)[0]} failed; comparing with {ups} as last fetched")
            lag = int(git(wt, "rev-list", "--count", f"{br}..{ups}")[1].strip() or 0)
            if lag: raise Err(f"{br} here is {lag} commit(s) behind {ups} (already pushed): fast-forward {br} first (in its checkout: git merge --ff-only {ups}), then janus land {a.unit} again")
        ahead = int(git(wt, "rev-list", "--count", f"{br}..HEAD")[1].strip() or 0); behind = int(git(wt, "rev-list", "--count", f"HEAD..{br}")[1].strip() or 0)
        if not ahead: raise Err(f"{branch} has no commits over {br}: nothing to land")
        if contained(wt, br): raise Err(f"{a.unit} is already landed: {br} holds its tree ({branch} has no change since): janus close {a.unit}")
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
        tree = git(wt, "rev-parse", "HEAD^{tree}")[1].strip()
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
        return 0
    raise Err(f"{br} kept moving: {LAND_TRIES} attempts; run janus land {a.unit} again when it settles")

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
