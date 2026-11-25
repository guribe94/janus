"""janus tidy: bookkeeping without hand operations (landed branches, old run files, stopped containers, closed units'
remote copies, old test roots, cache entries over a host's disk budget)."""
import json, os, re, shlex, tempfile
from pathlib import Path
from janus.actions.landing import follow_upstream
from janus.actions.lifecycle import uncollected
from janus.beads import Beads
from janus.board import board, board_rows
from janus.cap import capv
from janus.context import FIXTURE_MARK, Err, LockLost, fl, herdr, hrun, now, qp, refuse_agent, say, sh, ts
from janus.branch import contained, landed, landing_branch
from janus.probe import pueue
from janus.project import label_owner, own_card, tmux_name, unit_tmux
from janus.actions.launch import tmux_target
from janus.transcripts import card_fields
from janus.units import all_units, herdr_agents, orphan_tabs, ws_id

def tidy_plan(cfg, units, ag=None, rows=None, kept=None):
    """[(kind, target, why)] of what janus tidy removes: worktrees under paths.worktrees that are clean (no change, no untracked
    file, no stash of their branch), whose tree is contained in the landing branch and that no open unit uses (as its cwd, or by name: soak 44, a
    worktree whose folder or branch is named after an open unit is that unit's whatever cwd the unit recorded -- a unit launched
    without a worktree records the project root, and the tree its agent then made was listed as "no open unit"); herdr panes
    of closed units; board cards whose process is gone and that belong to no open unit."""
    P = []; names = {u["name"] for u in units}
    used = {Path(u["cwd"]).resolve() for u in units if u.get("cwd")}
    wd = cfg.p("worktrees", "worktrees")
    for d in sorted(wd.iterdir()) if wd.is_dir() else []:
        if not (d / ".git").is_file() or d.resolve() in used or d.resolve() == cfg.root.resolve(): continue
        keep = (lambda why: kept.append((str(d), why))) if kept is not None else (lambda why: None)
        br = sh(["git", "-C", str(d), "symbolic-ref", "-q", "--short", "HEAD"])[1].strip()
        # row 235: a tree an open unit made for its own jobs (<unit>-proof) is that unit's too; a relaunch's tree (<unit>-r<N>,
        # relaunch_name) is the relaunch's own, never its stranded original's
        own = next((u for n in (d.name, br) for u in sorted(names) if n == u or n.startswith(u + "-") and not re.fullmatch(r"-r\d+", n[len(u):])), None)
        if own: keep(f"open unit {own}'s worktree (its folder or branch has, or starts with, the unit's name)"); continue
        rc, o, e = sh(["git", "-C", str(d), "status", "--porcelain", "--ignore-submodules=none"])
        if rc or o.strip(): keep(f"{len(o.splitlines())} uncommitted change(s)" if not rc else "git status failed"); continue
        if br and any(l.startswith((f"WIP on {br}:", f"On {br}:")) for l in sh(["git", "-C", str(d), "stash", "list", "--format=%gs"])[1].splitlines()):
            keep(f"stashed changes on {br}"); continue
        lt = landed(cfg, {"cwd": str(d)})
        ups = upstream_of(d, landing_branch(cfg, d)) if lt is not True else None
        if ups and (sh(["git", "-C", str(d), "merge-base", "--is-ancestor", "HEAD", ups])[0] == 0 or contained(d, ups)): lt = True
        if lt is not True and landed_at(cfg, d, br, [landing_branch(cfg, d)] + ([ups] if ups else [])): lt = True
        if lt is not True:
            mb = sh(["git", "-C", str(d), "merge-base", "HEAD", landing_branch(cfg, d)])[1].strip()
            n = len(sh(["git", "-C", str(d), "diff", "--name-only", mb, "HEAD"])[1].split()) if mb else 0
            keep(f"no open unit, but its {n} changed file(s) are not all on the landing branch: land them, or decide" if lt is False else "landing state unknown"); continue
        P.append(("worktree", str(d), f"clean, contained in the landing branch{', branch ' + br + ' kept' if br else ''}, no open unit"))
    au = all_units(cfg); ws = ws_id(cfg)
    for k, a in (ag if ag is not None else herdr_agents(cfg)).items():
        if not ws or a.get("workspace_id") != ws: continue   # only panes in this project's workspace (cross-project safety)
        if k in names or k == "orchestrator" or au.get(k, {}).get("status") != "closed": continue
        P.append(("pane", a["pane_id"], f"herdr agent {k}: its unit is closed ({a.get('agent_status')})"))
    P += [("tab", t, f"tab {lab!r}: unit {n} is closed") for t, n, lab in orphan_tabs(cfg, names)]
    for card, r in (rows if rows is not None else board_rows(cfg)).items():
        if r.get("gone") and card not in names and own_card(cfg, card_fields(cfg, card)): P.append(("card", card, "its process is gone and it belongs to no open unit"))
    return P

# The whole mess orchestration makes (owner core rule, janus-fixes10 item 5b), each by a safe rule. [tidy] in janus.toml:
#   branches = ["wk/*"]   branch name patterns tidy may delete (default none): merged or squash-landed (contained in the landing
#                         branch), not checked out anywhere; on the remote too (remote = "origin") when its tip is the same commit
#   keep_days = 14        job run files (runs/<jid>.*) and test roots older than this go
#   every_min = 60        the watcher refreshes the reclaimable list this often (check: tidy:<n>, disk:<host>)
# [hosts.X] disk_budget_gb: the host's janus root plus its cache_dirs; over it, check raises disk:<host> and tidy removes the
# oldest top-level entries of cache_dirs (build targets, compiler caches) until under budget. Containers: our own stopped ones
# (name prefix janus-run-) only. Remote copies of closed units: only when nothing is uncollected.
TIDY_DEFAULTS = {"branches": [], "remote": "origin", "keep_days": 14, "every_min": 60, "auto": False, "artifacts": []}

def tset(cfg, k): return cfg.c.get("tidy", {}).get(k, TIDY_DEFAULTS[k])

# Recorded holds (soak 41): `janus tidy hold <target>` keeps one item a clean worktree an open unit still depends on (not as its
# cwd) would otherwise be removed with the rest, so the orchestrator no longer skips the whole tidy and tidy:<n> keeps firing.
# A hold matches a target equal to it, under it (a path prefix) or containing it; `janus tidy holds` lists them, `unhold` clears.
# Paths are compared resolved (soak 45): tidy names a worktree as it is configured (<root>/../worktrees/<name>, or through a
# symlink), the orchestrator holds it by its real path, and the raw strings never matched, so the held tree was removed.
def real_path(t): return os.path.realpath(t) if os.path.isabs(t) else t

def tidy_holds(cfg): return cfg.jget("tidy-holds.json")

def hold_item(cfg, target, why=""):
    h = tidy_holds(cfg); h[target] = {"why": why, "at": int(now())}; cfg.jput("tidy-holds.json", h)

def unhold_item(cfg, target):
    h = tidy_holds(cfg)
    if target in h: del h[target]; cfg.jput("tidy-holds.json", h); return True
    return False

def held_by(cfg, target, holds=None):
    """The hold string that keeps this target, or None."""
    hs = tidy_holds(cfg) if holds is None else holds; t = real_path(target)
    return next((h for h in hs for r in (real_path(h),) if t == r or t.startswith(r.rstrip("/") + "/") or r in t or h in target), None)

def apply_holds(cfg, P, holds=None):
    """P without the items a recorded hold keeps."""
    hs = tidy_holds(cfg) if holds is None else holds
    return [x for x in P if not held_by(cfg, x[1], hs)]

def tidy_artifacts(cfg, units):
    """[(kind, target, why, 0)]: per-worktree rebuildable output ([tidy] artifacts globs, e.g. target/ and node_modules/) inside
    a CLOSED unit's own worktree, so a worktree that stays around does not pin build output (keep-build-output rule; inert unless
    [tidy] artifacts is set). An open unit's tree is never touched."""
    pats = [p for p in (tset(cfg, "artifacts") or []) if p]
    if not pats: return []
    names = {u["name"] for u in units}; au = all_units(cfg); P = []; wd = cfg.p("worktrees", "worktrees")
    for d in sorted(wd.iterdir()) if wd.is_dir() else []:
        if d.name in names or not d.is_dir() or au.get(d.name, {}).get("status") != "closed": continue
        for pat in pats:
            for m in d.glob(pat):
                if m.is_dir(): P.append(("artifact", str(m), f"rebuildable output ({pat}) in closed unit {d.name}'s worktree", 0))
    return P

OUT_BULK_MB = 50   # a file in a closed unit's output at least this big is bulk (bundles, snapshots), never its report
OUT_KEEP = {"REPORT.md", "report.json", "backup.patch", "changes.patch"}   # what a closed unit's output is kept for

def dir_stats(path):
    """(bytes, newest mtime, holds an OUT_KEEP file) of the files under path, links not followed."""
    size, newest, keep = 0, 0, False
    for root, dirs, files in os.walk(path):
        for f in files:
            try: st = os.lstat(os.path.join(root, f))
            except OSError: continue
            size += st.st_size; newest = max(newest, st.st_mtime); keep = keep or f in OUT_KEEP
    return size, newest, keep

def landed_at(cfg, d, br, onto):
    """True when tree d's changes are the content of the commit that landed it: its unit's recorded publication, else the newest
    '<name>: land ' commit (janus land's subject) on a landing ref, that commit an ancestor of one of onto. A later change on the landing
    branch to one of its files kept a squash-landed tree 'unlanded' for good (rigpc-hw-session: byte-identical to its squash commit)."""
    u = all_units(cfg).get(d.name) or {}
    cands = [(u.get("published") or {}).get("commit")] + [sh(["git", "-C", str(d), "log", "-1", "--format=%H", f"--grep=^{re.escape(n)}: land ", r])[1].strip()
                                                         for n in dict.fromkeys(x for x in (d.name, br) if x) for r in onto]
    for c in dict.fromkeys(x for x in cands if x):
        if not any(sh(["git", "-C", str(d), "merge-base", "--is-ancestor", c, r])[0] == 0 for r in onto): continue
        if contained(d, c) is True: return True
    return False

def tidy_out_bulk(cfg, units):
    """[(kind, target, why, mb)]: bulky files (OUT_BULK_MB and more) and repository clones in the output folder (out/<unit>) of a
    CLOSED unit, unchanged for [tidy] keep_days; never OUT_KEEP files, never an open unit's output."""
    od = cfg.p("out", "out"); au = all_units(cfg); names = {u["name"] for u in units}; P = []
    cut = now() - tset(cfg, "keep_days") * 86400
    for d in sorted(od.iterdir()) if od.is_dir() else []:
        if not d.is_dir() or d.is_symlink() or d.name in names or au.get(d.name, {}).get("status") != "closed": continue
        whole = set()   # a folder of many files, each under OUT_BULK_MB (a cargo target dir, an extracted tarball, a repository snapshot):
        for sub in sorted(x for x in d.iterdir() if x.is_dir() and not x.is_symlink()):   # 3.7 GB of them were invisible to tidy (2026-11-25)
            size, newest, keep = dir_stats(sub)
            if keep or newest >= cut or size < OUT_BULK_MB << 20: continue
            whole.add(sub.name); P.append(("out-bulk", str(sub), f"{size >> 20} MB folder in closed unit {d.name}'s output, unchanged {tset(cfg, 'keep_days')} days", size >> 20))
        for root, dirs, files in os.walk(d):
            r = Path(root)
            if r == d: dirs[:] = [x for x in dirs if x not in whole]
            if ".git" in dirs:
                dirs.remove(".git")
                if r != d and (r / ".git").stat().st_mtime < cut:   # a nested clone (the output folder itself is never removed)
                    P.append(("out-bulk", str(r), f"repository clone in closed unit {d.name}'s output, unchanged {tset(cfg, 'keep_days')} days", 0)); dirs[:] = []; continue
            for f in files:
                q = r / f
                try: st = q.lstat()
                except OSError: continue
                if f in OUT_KEEP or q.is_symlink() or st.st_size < OUT_BULK_MB << 20 or st.st_mtime >= cut: continue
                P.append(("out-bulk", str(q), f"{st.st_size >> 20} MB in closed unit {d.name}'s output, unchanged {tset(cfg, 'keep_days')} days", st.st_size >> 20))
    return P

TIDY_HOST = r'''PATH="$HOME/.local/bin:%(bin)s:$PATH"; root=%(root)s; rd=%(rd)s; kd=%(kd)d
[ -n "$root" ] && [ -d "$root/worktrees" ] && for d in "$root"/worktrees/*/; do [ -d "$d" ] && n=${d%%/}; [ -d "$d" ] && echo "copy ${n##*/} $(du -sm "$d" 2>/dev/null | cut -f1)"; done
[ -d "$rd" ] && echo "runfiles $(find "$rd" -maxdepth 1 -type f -mtime +$kd \( -name '*.log' -o -name '*.sh' -o -name '*.rc' -o -name '*.cmd' -o -name '*.out' -o -name '*.err' -o -name '*.done' \) 2>/dev/null | wc -l | tr -d ' ')"
# a wait file whose pid is not its job any more (killed, reboot, pid reused): it read as a job waiting ([old-rule:mac]) and as one ahead
[ -d "$rd" ] && for f in "$rd"/*.wait "$rd"/*.gwait; do [ -e "$f" ] || continue; p=; read -r p _ < "$f" 2>/dev/null; b=${f##*/}; b=${b%%.*}; kill -0 "$p" 2>/dev/null && ps -p "$p" -o command= 2>/dev/null | grep -qF -- "$b" && continue; echo "stalewait $f"; done
tmux ls -F '#S' 2>/dev/null | sed 's/^/tmux /'
command -v podman >/dev/null 2>&1 && podman ps -a --filter name=^janus-run- --filter status=exited --format '{{.Names}}' 2>/dev/null | sed 's/^/container /'
for c in %(caches)s; do [ -d "$c" ] || continue; for e in "$c"/*; do [ -e "$e" ] && echo "cache $(du -sm "$e" 2>/dev/null | cut -f1) $(stat -c %%Y "$e" 2>/dev/null || stat -f %%m "$e") $e"; done; done
[ -n "$root" ] && [ -d "$root" ] && echo "rootmb $(du -sm "$root" 2>/dev/null | cut -f1)"; true'''

def orphan_session(cfg, s, units, au):
    """Why tmux session s is this project's orphan (check's orphan:tmux rule), else None: named janus-<unit>@<this project's id>,
    and no open unit records it (by its tmux name, or by its name: a unit still starting has no tmux name yet). Its unit may be
    closed, never recorded (a launch that failed after the session started), or a record another command left; a session with no
    project id is machine state and never this project's."""
    base, pid = label_owner(s)
    if pid != tmux_name(cfg, "x").rpartition("@")[2] or not base.startswith("janus-"): return None
    if s in {unit_tmux(u) for u in units} or base[6:] in {u["name"] for u in units}: return None
    u = au.get(base[6:])
    return "its unit is closed" if u and u.get("status") == "closed" else "no open unit records it"

def tidy_host(cfg, n, h, units, au):
    """[(kind, target, why, mb)] on host n (one shell call): synced copies of closed units, old run files, our stopped containers,
    cache entries over the disk budget; plus ("disk", n, why, over_mb) when over budget."""
    caches = [c for c in h.get("cache_dirs", [])]
    root = h.get("root", "") if not h.get("local") else ""
    rc, o, e = hrun(h, TIDY_HOST % {"bin": h.get("bin", ""), "root": qp(root) if root else "''", "rd": qp(h["_rd"]), "kd": int(tset(cfg, "keep_days")),
                                    "caches": " ".join(qp(c) for c in caches) or "''"}, timeout=180)
    if rc: return []
    P, open_ = [], {u["name"] for u in units or []}; cache, total = [], 0   # units None: beads unreadable, nothing that needs them is judged
    for l in o.splitlines():
        f = l.split(None, 3)
        if not f: continue
        if f[0] == "copy" and len(f) >= 2 and units is not None and f[1] not in open_:
            u = au.get(f[1])
            if u and u.get("status") == "closed" and u.get("host") == n:
                why = uncollected(cfg, {**u, "via": u.get("via") if u.get("via") not in ("failed", "starting") else "x"})
                if why: continue
                P.append(("remote-copy", f"{n}:{f[1]}", "its unit is closed and its copy holds nothing uncollected", int(fl(f[2]) if len(f) > 2 else 0)))
        elif f[0] == "tmux" and len(f) > 1 and f[1].startswith("janus-") and units is not None:   # this project's session that no open unit records: its pid holds the agent's reservation (fill fitted nothing for days)
            why = orphan_session(cfg, f[1], units, au)
            if why: P.append(("tmux", f"{n}:{f[1]}", why, 0))
        elif f[0] == "stalewait" and len(f) > 1: P.append(("stale-wait", f"{n}:{l.split(None, 1)[1]}", "a job's wait file whose process is gone (it read as a job waiting)", 0))
        elif f[0] == "runfiles" and len(f) > 1 and int(f[1] or 0): P.append(("run-files", n, f"{f[1]} job run file(s) older than {tset(cfg, 'keep_days')} days", 0))
        elif f[0] == "container" and len(f) > 1 and f[1].startswith("janus-run-"): P.append(("container", f"{n}:{f[1]}", "our own stopped container", 0))
        elif f[0] == "cache" and len(f) > 3: cache.append((int(fl(f[1])), int(fl(f[2])), f[3]))
        elif f[0] == "rootmb" and len(f) > 1: total += int(fl(f[1]))
    total += sum(c[0] for c in cache); bud = h.get("disk_budget_gb")
    if bud and total > bud * 1024:
        over = total - bud * 1024; P.append(("disk", n, f"{total / 1024:.1f} GB used by janus root and caches, budget {bud} GB", over))
        for mb, mt, path in sorted(cache, key=lambda c: c[1]):   # oldest first, until under budget
            if over <= 0: break
            P.append(("cache", f"{n}:{path}", f"cache entry, {mb} MB, last changed {ts(mt)}: over the disk budget", mb)); over -= mb
    return P

def job_records(cfg):
    """{jid: '<host>:<task>'} of the jobs janus recorded here (runs/busy, for janus busy)."""
    out = {}; bd_ = cfg.f("runs") / "busy"
    for f in sorted(bd_.glob("*")) if bd_.is_dir() else []:
        try: out[f.name] = f.read_text().split("\t", 1)[0].strip()
        except OSError: pass
    return out

def tidy_job_records(cfg):
    """[("job-record", jid, why, 0)]: a job record (runs/busy/<jid>, runs/jobs/<jid>.json) whose pueue task its host no longer has,
    read from that host's own pueue (a host that does not answer keeps its records). A 2026-11-15 job's records outlived its task
    and its worktree, and nothing removed them."""
    recs = job_records(cfg); P = []
    for hn in sorted({r.split(":")[0] for r in recs.values()}):
        h = cfg.hosts.get(hn)
        if h is None: continue
        rc, o, e = hrun(h, f"{pueue(h)} status --json", timeout=30)
        if rc: continue
        try: tasks = (json.loads(o) or {}).get("tasks") or {}
        except ValueError: continue
        P += [("job-record", j, f"its task {r} is gone from {hn}'s pueue", 0) for j, r in recs.items() if r.split(":")[0] == hn and r.partition(":")[2] not in tasks]
    return P

def tidy_repos(cfg):
    """The repositories the worktrees belong to (their git common dir) plus [tidy] repos, sorted."""
    repos = set(); wd = cfg.p("worktrees", "worktrees")
    for d in list(wd.iterdir()) if wd.is_dir() else []:
        if (d / ".git").is_file():
            c = sh(["git", "-C", str(d), "rev-parse", "--path-format=absolute", "--git-common-dir"])[1].strip()
            if c: repos.add(str(Path(c).parent))
    return sorted(repos | {str(cfg.root / r) for r in cfg.c.get("tidy", {}).get("repos", [])})

def tidy_branches(cfg, units):
    """[(kind, target, why, 0)]: branches matching [tidy] branches, not checked out in any worktree, contained in the landing
    branch (ancestor, or every file they changed has the tip's content); and the remote branch when its tip is the same commit."""
    pats = tset(cfg, "branches")
    if not pats: return []
    import fnmatch
    P, rem = [], tset(cfg, "remote")
    for r in tidy_repos(cfg):
        out_ = {l.split()[1][11:] for l in sh(["git", "-C", r, "worktree", "list", "--porcelain"])[1].splitlines() if l.startswith("branch refs/heads/")}
        refs = sh(["git", "-C", r, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"])[1].splitlines()
        rrefs = dict(l.split() for l in sh(["git", "-C", r, "for-each-ref", "--format=%(refname:short) %(objectname)", f"refs/remotes/{rem}"])[1].splitlines() if len(l.split()) == 2)
        br_ = landing_branch(cfg, Path(r)); ups = upstream_of(r, br_)
        for l in refs:
            b, sha = l.split()
            if b in out_ or not any(fnmatch.fnmatch(b, p) for p in pats) or b == br_: continue
            if not any(sh(["git", "-C", r, "merge-base", "--is-ancestor", sha, x])[0] == 0 or branch_contained(r, sha, x) for x in [br_] + ([ups] if ups else [])): continue
            P.append(("branch", f"{r}:{b}", f"contained in {br_}, not checked out", 0))
            if rrefs.get(f"{rem}/{b}") == sha: P.append(("remote-branch", f"{r}:{rem}/{b}", f"same commit as the local branch, contained in {br_}", 0))
    return P

def upstream_of(repo, br):
    """br's upstream (e.g. origin/main) as last fetched, or None. Landed is judged against it too: when the tidy pass's
    fast-forward of br fails, a local br that lags its upstream kept landed worktrees and branches forever (disk filled)."""
    rc, o, e = sh(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", f"{br}@{{upstream}}"]); o = o.strip()
    return o if rc == 0 and "/" in o else None

def branch_contained(repo, sha, br):
    mb = sh(["git", "-C", repo, "merge-base", sha, br])[1].strip()
    if not mb: return False
    fs = [f for f in sh(["git", "-C", repo, "diff", "--name-only", mb, sha])[1].splitlines() if f]
    return not fs or sh(["git", "-C", repo, "diff", "--quiet", br, sha, "--", *fs])[0] == 0

def tidy_roots(cfg):
    """Test and demo roots janus or its tests created, older than keep_days... or a day for fixtures (their suite is gone)."""
    P, cut = [], now() - 86400
    for base in {Path("/tmp"), Path(tempfile.gettempdir())}:
        for d in base.glob(f"{FIXTURE_MARK}-*") if base.is_dir() else []:
            try:
                if d.is_dir() and d.stat().st_mtime < cut: P.append(("test-root", str(d), "a test suite's fixture root, over a day old", 0))
            except OSError: pass
        for pat in ("janus-test-*", "janus-rem-*"):   # roots of suites from before the fixture root
            for d in base.glob(pat) if base.is_dir() else []:
                try:
                    if d.is_dir() and not d.name.startswith(FIXTURE_MARK) and d.stat().st_mtime < cut: P.append(("test-root", str(d), "an old test root, over a day old", 0))
                except OSError: pass
    return P

def tidy_all(cfg, units, hs=None):
    """Everything tidy would do: tidy_plan (worktrees, panes, cards) + branches + per-host items + test roots + per-worktree
    rebuildable artifacts."""
    for r in tidy_repos(cfg):   # first: the landing branch at its upstream (what is landed is judged against what is published)
        lag, err = follow_upstream(Path(r), landing_branch(cfg, Path(r)), cfg.run.dry or cfg.run.ro)
        if err: say(f"note: {r}: {landing_branch(cfg, Path(r))} is {lag} behind its upstream and did not fast-forward: {err}")
        elif lag: say(f"{r}: {landing_branch(cfg, Path(r))} {'is' if cfg.run.dry or cfg.run.ro else 'was'} {lag} commit(s) behind its upstream" + ("" if cfg.run.dry or cfg.run.ro else ": fast-forwarded"))
    au = all_units(cfg); P = ([(k, t, w, 0) for k, t, w in tidy_plan(cfg, units)] + tidy_branches(cfg, units)
                              + tidy_roots(cfg) + tidy_artifacts(cfg, units) + tidy_out_bulk(cfg, units) + tidy_job_records(cfg))
    for n, h in cfg.hosts.items():
        if hs is not None and not (hs.get(n) or {}).get("ok"): continue
        P += tidy_host(cfg, n, h, units, au)
    return P

# The kinds the watcher removes on its own with [tidy] auto = true: every kind tidy finds, each already chosen by its safe rule
# (clean and landed; closed and nothing uncollected; our own stopped container; older than keep_days; over the disk budget).
# Only some once, and the rest waited for the orchestrator, whose hooks refuse hand removal: found waste filled the disk.
TIDY_AUTO_KINDS = ("worktree", "branch", "remote-branch", "artifact", "remote-copy", "run-files", "container", "cache", "test-root", "out-bulk", "pane", "tab", "card", "tmux",
                   "stale-wait", "job-record")
TIDY_HOST_KINDS = ("remote-copy", "run-files", "container", "cache", "tmux", "stale-wait")

LOW_DISK_MIN = 5   # a host under [capacity] min_disk_gb is tidied this often (a pass scans every host: tens of seconds)

def tend_tidy(cfg, hs, units, log):
    """The watcher refreshes the reclaimable list every [tidy] every_min; with [tidy] auto = true it also removes what it found
    (TIDY_AUTO_KINDS) itself, so the tidy:<n> finding does not keep firing for bookkeeping nobody does."""
    low = [n for n, p in (hs or {}).items() if p.get("ok") and p.get("disk_gb") and n in cfg.hosts
           and fl(p["disk_gb"]) < capv(cfg, cfg.hosts[n], "min_disk_gb")]   # a disk under its floor: tidy within LOW_DISK_MIN, not every_min
    if now() - cfg.jget("tidy.json").get("at", 0) < min(tset(cfg, "every_min"), LOW_DISK_MIN if low else 1e9) * 60: return
    import io, contextlib
    buf = io.StringIO()   # tidy_all's notes (a landing branch that did not fast-forward) went to the watcher's stdout, which nothing keeps
    try:
        with cfg.unlocked(), contextlib.redirect_stdout(buf): P = apply_holds(cfg, tidy_all(cfg, units, hs))   # the hosts' scan (ssh) and git: not under the writer lock
    except LockLost: raise
    except Exception as e: log(f"tidy pass failed: {e}"); return
    finally:
        for l in buf.getvalue().splitlines():
            if l.strip(): log(f"tidy: {l.strip()[:300]}")
    if low: log(f"tidy: disk under its floor on {', '.join(low)}: tidy pass early")
    cfg.jput("tidy.json", {"at": int(now()), "plan": [list(x) for x in P]})
    if tset(cfg, "auto") and not cfg.run.dry and not cfg.run.ro:
        try: taken = {str(Path(u["cwd"]).resolve()) for u in Beads(cfg).units() if u.get("cwd")}   # units read again: a tree launched into meanwhile is never removed
        except Err: return
        for kind, t, why, mb in P:
            if kind not in TIDY_AUTO_KINDS or (kind == "worktree" and str(Path(t).resolve()) in taken): continue
            if kind in TIDY_HOST_KINDS:
                with cfg.unlocked(): rc, e = tidy_do(cfg, kind, t)   # a host's rm or podman over ssh: never under the writer lock
            else: rc, e = tidy_do(cfg, kind, t)
            log(f"tidy auto {kind} {t}: {'removed' if rc == 0 else 'NOT removed ' + (e or '').strip()[:80]}")

def tidy_do(cfg, kind, t):
    """Carry out one tidy item; (rc, detail)."""
    if kind == "worktree":
        top = sh(["git", "-C", t, "rev-parse", "--path-format=absolute", "--git-common-dir"])[1].strip()
        rc, o, e = sh(["git", "-C", str(Path(top).parent) if top else t, "worktree", "remove", t]); return rc, e   # never --force: clean only
    if kind in ("pane", "tab"): return (0, "") if herdr(kind, "close", t) is not None else (1, f"herdr {kind} close failed")
    if kind == "card": rc, o, e = board(cfg, "close", t); return rc, e
    if kind in ("branch", "remote-branch"):
        r, _, b = t.rpartition(":")
        if kind == "branch": rc, o, e = sh(["git", "-C", r, "branch", "-D", b]); return rc, e   # -D: squash-landed is not 'merged'; checked above
        rem, _, rb = b.partition("/"); rc, o, e = sh(["git", "-C", r, "push", rem, "--delete", rb], timeout=60); return rc, e
    if kind == "test-root":
        q = Path(t)   # only a test root itself: named so, directly in a temp base folder
        if not re.fullmatch(rf"({re.escape(FIXTURE_MARK)}|janus-test|janus-rem)-[^/]+", q.name) or \
                str(q.parent.resolve()) not in {str(Path("/tmp").resolve()), str(Path(tempfile.gettempdir()).resolve())}: return 1, "not a test root"
        if not q.is_dir() or now() - q.stat().st_mtime < 86400: return 1, "under a day old (its suite may still run)"
        import shutil; shutil.rmtree(t, ignore_errors=True); return 0, ""
    hn, _, x = t.partition(":"); h = cfg.hosts.get(hn if kind != "run-files" else t, {})
    if kind == "remote-copy":
        root = h["root"]; return hrun(h, f"cd {qp(root)} && rm -rf worktrees/{shlex.quote(x)} base/{shlex.quote(x)} briefs/{shlex.quote(x)}.md run/{shlex.quote(x)}.* ctx/{shlex.quote(x)}; true", timeout=300)[0], ""
    if kind == "tmux": return hrun(h, f'PATH="$HOME/.local/bin:{h.get("bin", "")}:$PATH"; tmux kill-session -t {tmux_target(x, False)}', timeout=60)[0], ""
    if kind == "stale-wait":
        if not (x.endswith((".wait", ".gwait")) and os.path.dirname(x) == h.get("_rd", "").rstrip("/")): return 1, "not a wait file in the host's runs dir"
        return hrun(h, f"rm -f {qp(x)}", timeout=60)[0], ""
    if kind == "run-files":
        return hrun(h, f"find {qp(h['_rd'])} -maxdepth 1 -type f -mtime +{int(tset(cfg, 'keep_days'))} \\( -name '*.log' -o -name '*.sh' -o -name '*.rc' -o -name '*.cmd' -o -name '*.out' -o -name '*.err' -o -name '*.done' \\) -exec rm -f {{}} +", timeout=300)[0], ""
    if kind == "container":
        if not x.startswith("janus-run-"): return 1, "not ours"
        return hrun(h, f"podman rm {shlex.quote(x)} >/dev/null", timeout=60)[0], ""
    if kind == "cache":
        if not any(x.startswith(c.rstrip("/") + "/") for c in h.get("cache_dirs", [])): return 1, "not under a cache_dirs entry"
        return hrun(h, f"rm -rf {qp(x)}", timeout=600)[0], ""
    if kind == "out-bulk":
        od = cfg.p("out", "out").resolve(); q = Path(t)   # only under a unit's output folder, never the folder itself or a kept file
        if q.is_symlink() or q.name in OUT_KEEP or not q.resolve().is_relative_to(od) or q.resolve().parent == od: return 1, "not a unit's bulky output"
        if q.is_dir():
            import shutil; shutil.rmtree(t, ignore_errors=True); return 0, ""
        q.unlink(missing_ok=True); return 0, ""
    if kind == "job-record":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", t): return 1, "not a job id"
        for f in (cfg.f("runs") / "busy" / t, cfg.f("runs") / "jobs" / f"{t}.json"): f.unlink(missing_ok=True)
        return 0, ""
    if kind == "artifact":
        wd = cfg.p("worktrees", "worktrees").resolve(); q = Path(t)
        if not (q.is_dir() and q.resolve().is_relative_to(wd)): return 1, "not a worktree artifact"
        import shutil; shutil.rmtree(t, ignore_errors=True); return 0, ""
    return 1, f"unknown kind {kind}"

def disk_free_gb(path):
    """Free GB on the filesystem holding path, from df (the real measure: on APFS/CoW, du over-counts shared clones)."""
    rc, o, e = sh(["df", "-Pk", str(path)])
    try: return int(o.splitlines()[1].split()[3]) / 1048576
    except (IndexError, ValueError): return None

def cmd_tidy(cfg, a):
    act = getattr(a, "action", None)
    if act == "holds":
        return 0, ([f"hold {h}: {v.get('why') or '-'} (since {ts(v.get('at', 0))})" for h, v in tidy_holds(cfg).items()] or ["(no tidy holds)"])
    if act in ("hold", "unhold"):
        if not (getattr(a, "target", None) or "").strip(): raise Err(f"janus tidy {act} <target> [--why <text>]")
        if cfg.run.dry: say(f"+ {act} {a.target}"); return
        if act == "hold": hold_item(cfg, a.target, getattr(a, "why", "") or ""); say(f"held {a.target}: tidy will not remove it (janus tidy unhold {a.target})")
        else: say(f"released {a.target}" if unhold_item(cfg, a.target) else f"no hold on {a.target}")
        return
    if not cfg.run.dry: refuse_agent("janus tidy")   # --dry-run only reads
    free0 = disk_free_gb(cfg.root); kept = []
    try: units = Beads(cfg).units()
    except Err as e:   # beads unreadable (bd: timeout under load made tidy exit doing nothing): only the rules that need no unit run
        units = None
        say(f"note: beads could not be read ({e}): skipped the rules that need the open and closed units (worktrees, artifacts, closed units' output, "
            "remote copies, tmux sessions, panes, cards); the rest runs")
    au = all_units(cfg) if units is not None else {}
    if units is not None and cfg.run.all_units_err: say(f"note: closed units could not be read ({cfg.run.all_units_err}): their output and copies are not judged this time")
    P = tidy_branches(cfg, units or []) + tidy_roots(cfg) + tidy_job_records(cfg)
    if units is not None: P = [(k, t, w, 0) for k, t, w in tidy_plan(cfg, units, kept=kept)] + P + tidy_artifacts(cfg, units) + tidy_out_bulk(cfg, units)
    for n, h in cfg.hosts.items(): P += tidy_host(cfg, n, h, units, au)
    P = apply_holds(cfg, P)
    for d, why in kept: say(f"kept worktree {d}: {why}")
    if not cfg.run.dry and not cfg.run.ro: cfg.jput("tidy.json", {"at": int(now()), "plan": [list(x) for x in P if x[0] == "disk"]})
    free1 = disk_free_gb(cfg.root)
    if free0 is not None and free1 is not None: say(f"disk free (df, the real measure; du over-counts shared clones): {free0:.1f} GB -> {free1:.1f} GB")
    if not [x for x in P if x[0] != "disk"]: say("nothing to tidy" + "".join(f"; {x[1]}: {x[2]}" for x in P if x[0] == "disk")); return
    for kind, t, why, mb in P:
        if kind == "disk": say(f"disk {t}: {why}"); continue
        if cfg.run.dry: say(f"would remove {kind} {t}: {why}" + (f" ({mb} MB)" if mb else "")); continue
        rc, e = tidy_do(cfg, kind, t)
        say(f"{'removed' if rc == 0 else 'NOT removed'} {kind} {t}: {why}" + (f" ({(e or '').strip()[:120]})" if rc else ""))
