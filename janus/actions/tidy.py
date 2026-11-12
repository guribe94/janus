"""janus tidy: bookkeeping without hand operations (landed branches, old run files, stopped containers, closed units'
remote copies, old test roots, cache entries over a host's disk budget)."""
import re, shlex, tempfile
from pathlib import Path
from janus.actions.lifecycle import uncollected
from janus.beads import Beads
from janus.board import board, board_rows
from janus.context import FIXTURE_MARK, fl, herdr, hrun, now, qp, refuse_agent, say, sh, ts
from janus.gates import landed, landing_branch
from janus.project import own_card
from janus.transcripts import card_fields
from janus.units import all_units, herdr_agents, ws_id

def tidy_plan(cfg, units, ag=None, rows=None, kept=None):
    """[(kind, target, why)] of what janus tidy removes: worktrees under paths.worktrees that are clean (no change, no untracked
    file, no stash of their branch), whose tree is contained in the landing branch and that no open unit uses; herdr panes
    of closed units; board cards whose process is gone and that belong to no open unit."""
    P = []; names = {u["name"] for u in units}
    used = {Path(u["cwd"]).resolve() for u in units if u.get("cwd")}
    wd = cfg.p("worktrees", "worktrees")
    for d in sorted(wd.iterdir()) if wd.is_dir() else []:
        if not (d / ".git").is_file() or d.resolve() in used or d.resolve() == cfg.root.resolve(): continue
        keep = (lambda why: kept.append((str(d), why))) if kept is not None else (lambda why: None)
        rc, o, e = sh(["git", "-C", str(d), "status", "--porcelain", "--ignore-submodules=none"])
        if rc or o.strip(): keep(f"{len(o.splitlines())} uncommitted change(s)" if not rc else "git status failed"); continue
        br = sh(["git", "-C", str(d), "symbolic-ref", "-q", "--short", "HEAD"])[1].strip()
        if br and any(l.startswith((f"WIP on {br}:", f"On {br}:")) for l in sh(["git", "-C", str(d), "stash", "list", "--format=%gs"])[1].splitlines()):
            keep(f"stashed changes on {br}"); continue
        lt = landed(cfg, {"cwd": str(d)})
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
def tidy_holds(cfg): return cfg.jget("tidy-holds.json")

def hold_item(cfg, target, why=""):
    h = tidy_holds(cfg); h[target] = {"why": why, "at": int(now())}; cfg.jput("tidy-holds.json", h)

def unhold_item(cfg, target):
    h = tidy_holds(cfg)
    if target in h: del h[target]; cfg.jput("tidy-holds.json", h); return True
    return False

def held_by(cfg, target, holds=None):
    """The hold string that keeps this target, or None."""
    hs = tidy_holds(cfg) if holds is None else holds
    return next((h for h in hs if target == h or target.startswith(h.rstrip("/") + "/") or h in target), None)

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

TIDY_HOST = r'''root=%(root)s; rd=%(rd)s; kd=%(kd)d
[ -n "$root" ] && [ -d "$root/worktrees" ] && for d in "$root"/worktrees/*/; do [ -d "$d" ] && n=${d%%/}; [ -d "$d" ] && echo "copy ${n##*/} $(du -sm "$d" 2>/dev/null | cut -f1)"; done
[ -d "$rd" ] && echo "runfiles $(find "$rd" -maxdepth 1 -type f -mtime +$kd \( -name '*.log' -o -name '*.sh' -o -name '*.rc' -o -name '*.cmd' -o -name '*.out' -o -name '*.err' -o -name '*.done' \) 2>/dev/null | wc -l | tr -d ' ')"
command -v podman >/dev/null 2>&1 && podman ps -a --filter name=^janus-run- --filter status=exited --format '{{.Names}}' 2>/dev/null | sed 's/^/container /'
for c in %(caches)s; do [ -d "$c" ] || continue; for e in "$c"/*; do [ -e "$e" ] && echo "cache $(du -sm "$e" 2>/dev/null | cut -f1) $(stat -c %%Y "$e" 2>/dev/null || stat -f %%m "$e") $e"; done; done
[ -n "$root" ] && [ -d "$root" ] && echo "rootmb $(du -sm "$root" 2>/dev/null | cut -f1)"; true'''

def tidy_host(cfg, n, h, units, au):
    """[(kind, target, why, mb)] on host n (one shell call): synced copies of closed units, old run files, our stopped containers,
    cache entries over the disk budget; plus ("disk", n, why, over_mb) when over budget."""
    caches = [c for c in h.get("cache_dirs", [])]
    root = h.get("root", "") if not h.get("local") else ""
    rc, o, e = hrun(h, TIDY_HOST % {"root": qp(root) if root else "''", "rd": qp(h["_rd"]), "kd": int(tset(cfg, "keep_days")),
                                    "caches": " ".join(qp(c) for c in caches) or "''"}, timeout=180)
    if rc: return []
    P, open_ = [], {u["name"] for u in units}; cache, total = [], 0
    for l in o.splitlines():
        f = l.split(None, 3)
        if not f: continue
        if f[0] == "copy" and len(f) >= 2 and f[1] not in open_:
            u = au.get(f[1])
            if u and u.get("status") == "closed" and u.get("host") == n:
                why = uncollected(cfg, {**u, "via": u.get("via") if u.get("via") not in ("failed", "starting") else "x"})
                if why: continue
                P.append(("remote-copy", f"{n}:{f[1]}", "its unit is closed and its copy holds nothing uncollected", int(fl(f[2]) if len(f) > 2 else 0)))
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

def tidy_branches(cfg, units):
    """[(kind, target, why, 0)]: branches matching [tidy] branches, not checked out in any worktree, contained in the landing
    branch (ancestor, or every file they changed has the tip's content); and the remote branch when its tip is the same commit."""
    pats = tset(cfg, "branches")
    if not pats: return []
    import fnmatch
    repos = set(); wd = cfg.p("worktrees", "worktrees")
    for d in list(wd.iterdir()) if wd.is_dir() else []:
        if (d / ".git").is_file():
            c = sh(["git", "-C", str(d), "rev-parse", "--path-format=absolute", "--git-common-dir"])[1].strip()
            if c: repos.add(str(Path(c).parent))
    repos |= {str(cfg.root / r) for r in cfg.c.get("tidy", {}).get("repos", [])}
    P, rem = [], tset(cfg, "remote")
    for r in sorted(repos):
        out_ = {l.split()[1][11:] for l in sh(["git", "-C", r, "worktree", "list", "--porcelain"])[1].splitlines() if l.startswith("branch refs/heads/")}
        refs = sh(["git", "-C", r, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"])[1].splitlines()
        rrefs = dict(l.split() for l in sh(["git", "-C", r, "for-each-ref", "--format=%(refname:short) %(objectname)", f"refs/remotes/{rem}"])[1].splitlines() if len(l.split()) == 2)
        br_ = landing_branch(cfg, Path(r))
        for l in refs:
            b, sha = l.split()
            if b in out_ or not any(fnmatch.fnmatch(b, p) for p in pats) or b == br_: continue
            if sh(["git", "-C", r, "merge-base", "--is-ancestor", sha, br_])[0] != 0 and not branch_contained(r, sha, br_): continue
            P.append(("branch", f"{r}:{b}", f"contained in {br_}, not checked out", 0))
            if rrefs.get(f"{rem}/{b}") == sha: P.append(("remote-branch", f"{r}:{rem}/{b}", f"same commit as the local branch, contained in {br_}", 0))
    return P

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
    au = all_units(cfg); P = ([(k, t, w, 0) for k, t, w in tidy_plan(cfg, units)] + tidy_branches(cfg, units)
                              + tidy_roots(cfg) + tidy_artifacts(cfg, units))
    for n, h in cfg.hosts.items():
        if hs is not None and not (hs.get(n) or {}).get("ok"): continue
        P += tidy_host(cfg, n, h, units, au)
    return P

# The kinds the watcher may remove on its own with [tidy] auto = true: a clean landed worktree and a landed branch. Every other
# kind (a remote copy, a container, a cache entry, a board card, a pane) still needs the orchestrator's `janus tidy`.
TIDY_AUTO_KINDS = ("worktree", "branch", "remote-branch")

def tend_tidy(cfg, hs, units, log):
    """The watcher refreshes the reclaimable list every [tidy] every_min; with [tidy] auto = true it also removes the safe kinds
    (a clean landed worktree, a landed branch) itself, so the tidy:<n> finding does not keep firing for bookkeeping nobody does."""
    if now() - cfg.jget("tidy.json").get("at", 0) < tset(cfg, "every_min") * 60: return
    try: P = apply_holds(cfg, tidy_all(cfg, units, hs))
    except Exception as e: log(f"tidy pass failed: {e}"); return
    cfg.jput("tidy.json", {"at": int(now()), "plan": [list(x) for x in P]})
    if tset(cfg, "auto") and not cfg.run.dry and not cfg.run.ro:
        for kind, t, why, mb in P:
            if kind not in TIDY_AUTO_KINDS: continue
            rc, e = tidy_do(cfg, kind, t); log(f"tidy auto {kind} {t}: {'removed' if rc == 0 else 'NOT removed ' + (e or '').strip()[:80]}")

def tidy_do(cfg, kind, t):
    """Carry out one tidy item; (rc, detail)."""
    if kind == "worktree":
        top = sh(["git", "-C", t, "rev-parse", "--path-format=absolute", "--git-common-dir"])[1].strip()
        rc, o, e = sh(["git", "-C", str(Path(top).parent) if top else t, "worktree", "remove", t]); return rc, e   # never --force: clean only
    if kind == "pane": return (0, "") if herdr("pane", "close", t) is not None else (1, "herdr pane close failed")
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
    if kind == "run-files":
        return hrun(h, f"find {qp(h['_rd'])} -maxdepth 1 -type f -mtime +{int(tset(cfg, 'keep_days'))} \\( -name '*.log' -o -name '*.sh' -o -name '*.rc' -o -name '*.cmd' -o -name '*.out' -o -name '*.err' -o -name '*.done' \\) -exec rm -f {{}} +", timeout=300)[0], ""
    if kind == "container":
        if not x.startswith("janus-run-"): return 1, "not ours"
        return hrun(h, f"podman rm {shlex.quote(x)} >/dev/null", timeout=60)[0], ""
    if kind == "cache":
        if not any(x.startswith(c.rstrip("/") + "/") for c in h.get("cache_dirs", [])): return 1, "not under a cache_dirs entry"
        return hrun(h, f"rm -rf {qp(x)}", timeout=600)[0], ""
    if kind == "artifact":
        wd = cfg.p("worktrees", "worktrees").resolve(); q = Path(t)
        if not (q.is_dir() and q.resolve().is_relative_to(wd)): return 1, "not a worktree artifact"
        import shutil; shutil.rmtree(t, ignore_errors=True); return 0, ""
    return 1, f"unknown kind {kind}"

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
    units = Beads(cfg).units(); kept = []; au = all_units(cfg)
    P = [(k, t, w, 0) for k, t, w in tidy_plan(cfg, units, kept=kept)] + tidy_branches(cfg, units) + tidy_roots(cfg) + tidy_artifacts(cfg, units)
    for n, h in cfg.hosts.items(): P += tidy_host(cfg, n, h, units, au)
    P = apply_holds(cfg, P)
    for d, why in kept: say(f"kept worktree {d}: {why}")
    if not cfg.run.dry and not cfg.run.ro: cfg.jput("tidy.json", {"at": int(now()), "plan": [list(x) for x in P if x[0] == "disk"]})
    if not [x for x in P if x[0] != "disk"]: say("nothing to tidy" + "".join(f"; {x[1]}: {x[2]}" for x in P if x[0] == "disk")); return
    for kind, t, why, mb in P:
        if kind == "disk": say(f"disk {t}: {why}"); continue
        if cfg.run.dry: say(f"would remove {kind} {t}: {why}" + (f" ({mb} MB)" if mb else "")); continue
        rc, e = tidy_do(cfg, kind, t)
        say(f"{'removed' if rc == 0 else 'NOT removed'} {kind} {t}: {why}" + (f" ({(e or '').strip()[:120]})" if rc else ""))
