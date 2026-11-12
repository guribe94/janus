"""Findings about units: parked units that cannot resume, budgets nearly spent with a stale report, runs capped without
a verdict, the waste pause and a due report, unreconciled ledger runs, and peers (peers.md, overlap)."""
import concurrent.futures as cf, re, time
from pathlib import Path
from orchkit.actions.jobs import job_state, unit_jobs
from orchkit.board import board_dir
from orchkit.budgets import bset, last_checkpoint, unit_spend, waste_paused, work_written
from orchkit.context import DIFF_X, atomic_write, fl, hrun, now, qp, sh, ts
from orchkit.ledger import ledger_read
from orchkit.transcripts import card_fields, transcript_where
from orchkit.units import ENDED, all_units, api_out, own_tree, report_path, unit_runs

def unreconciled(cfg, pv, units):
    """[(run id, unit, budget, why)] of runs with a running row and no done row whose agent is gone: the unit is closed, the run
    was superseded by a later run of its unit, its unit's agent ended (units carry the state check computed), or the run is
    over a day old and no unit records it."""
    au = all_units(cfg); st = {u["name"]: u for u in units}; byrun = {k: u for u in au.values() for k in unit_runs(u)}; out = []
    for k, r in ledger_read(cfg, pv)[0].items():
        if r["done"] or not r["running"]: continue
        u = byrun.get(k) or au.get(r["unit"]); live = st.get(r["unit"])
        try: age = now() - time.mktime(time.strptime(r["ts"][:19], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError): age = 0
        if not u: why = "no unit records it" if age > 86400 else None
        elif u.get("status") == "closed" and not live: why = f"its unit {r['unit']} is closed"
        elif live and live.get("run") and live["run"] != k and k in unit_runs(live): why = f"superseded by run {live['run']}"
        elif live and live.get("run") == k and live.get("state") in ("done", "failed", "gone"): why = f"its agent ended ({live['state']})"
        else: why = None
        if why: out.append((k, r["unit"], r["budget"] or 0.0, why))
    return out

def parked_findings(cfg, hs, u, pk):
    """parked:<unit>: a job it waits on can never finish; parked:<unit>:resume: its jobs finished but the resume failed."""
    js = {j: job_state(hs, j)[0] for j in pk}; F = []
    if any(x in ("paused", "stashed", "locked") for x in js.values()):
        F.append((f"parked:{u['name']}", f"{u['name']} is parked on {', '.join(f'{j} {x}' for j, x in js.items())}: a job that is not queued or running never resumes it: start or re-queue that job, or orch resume --force"))
    re_ = u.get("resume_error") or {}
    if re_ and all(x in ("done", "gone") for x in js.values()):
        F.append((f"parked:{u['name']}:resume", f"{u['name']}'s jobs finished but it cannot be resumed ({re_.get('why', '')[:160]}): "
                  f"orch launch --resume-of {u['name']} with a new budget, or orch close {u['name']}"))
    return F

def budget_findings(cfg, units):
    F = []
    for u in units:
        if u.get("state") not in ("working", "queued") or not u.get("budget"): continue
        sp, how = unit_spend(cfg, u)
        if sp is None or sp < bset(cfg, "warn_pct") / 100 * u["budget"]: continue
        try: age = (now() - report_path(cfg, u).stat().st_mtime) / 60
        except OSError: age = (now() - u.get("started", now())) / 60
        if age >= bset(cfg, "report_stale_min"):
            F.append((f"budget80:{u['name']}", f"{u['name']} has spent about ${sp:.2f} of its ${u['budget']:g} budget ({100 * sp / u['budget']:.0f}%, {how}) "
                      f"and its report has not changed for {age:.0f} min: direct it to write its state to the report now (board.sh direct), "
                      f"or stop it and resume with a larger --budget (--resume-of keeps its report)"))
    return F

def report_findings(cfg, units):
    F = []
    for u in units:
        if u.get("state") in ENDED and not u.get("verdict") and api_out(cfg, u).get("subtype") == "error_max_budget_usd":
            w = "a wrap-up resume did not write one either" if u.get("wrapup") else "the watcher resumes it once to write its report"
            F.append((f"waste:{u['name']}", f"{u['name']} ended at its budget cap (${fl(api_out(cfg, u).get('total_cost_usd')):.2f}) without a verdict ({w}); "
                      f"{'work was written' if work_written(cfg, u) else 'nothing was written'}: read its report, resume it with --resume-of and a larger budget, or close it"))
    why = waste_paused(cfg)
    if why: F.append(("waste-pause", "launch and fill PAUSED: " + why))
    cp = last_checkpoint(cfg)
    if cp and cfg.mtime("report_sent") < cp: F.append(("report-due", f"a status report to the owner is due (checkpoint {ts(cp)}): orch report, send it, then orch report --sent"))
    return F

def unit_changes(cfg, u, units=()):
    """(branch, [files changed so far]) of a unit's tree: locally its commits since it left the landing branch plus uncommitted
    and untracked files; on a remote host the files that differ from the tree it was sent (base/<unit>). A shared tree (the
    project root, another open unit's cwd) has none of its own."""
    h = cfg.hosts.get(u.get("host"), {})
    if not h.get("local"):
        if u.get("via") in ("failed", "starting") or "root" not in h: return "?", []
        n = u["name"]
        rc, o, e = hrun(h, f"cd {qp(h['root'])} && [ -d base/{n} ] && diff -rq {DIFF_X} base/{n} worktrees/{n} 2>/dev/null; true", timeout=60)
        fs = []
        for l in o.splitlines():
            m = re.match(rf"Files base/{re.escape(n)}/(.+) and worktrees/{re.escape(n)}/.+ differ$", l) or re.match(rf"Only in (?:base|worktrees)/{re.escape(n)}/?(.*): (.+)$", l)
            if m: fs.append(m.group(1) if m.lastindex == 1 else str(Path(m.group(1)) / m.group(2)) if m.group(1) else m.group(2))
        b = sh(["git", "-C", u["cwd"], "rev-parse", "--abbrev-ref", "HEAD"])[1].strip() if u.get("cwd") and Path(u["cwd"]).is_dir() else ""
        return (b or "?"), sorted(set(fs))   # the copy carries no .git: the branch of the worktree it was sent from
    cwd = u.get("cwd")
    if not cwd or not Path(cwd).is_dir() or not own_tree(cfg, u, units or [u]): return "?", []
    br = sh(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"])[1].strip() or "?"
    lb = cfg.c.get("landing", {}).get("branch") or "main"
    mb = sh(["git", "-C", cwd, "merge-base", "HEAD", lb])[1].strip()
    fs = set(sh(["git", "-C", cwd, "diff", "--relative", "--name-only", mb, "--", "."])[1].split("\n")) if mb else set()
    top = sh(["git", "-C", cwd, "rev-parse", "--show-toplevel"])[1].strip()
    fs |= {str(Path(top, l[3:].split(" -> ")[-1].strip('"')).relative_to(Path(cwd).resolve())) for l in
           sh(["git", "-C", cwd, "status", "--porcelain", "-uall", "--", "."])[1].splitlines() if len(l) > 3 and top}
    skip = [cfg.state.resolve(), board_dir(cfg).resolve()]   # orch's own files and the board are nobody's change (as in close)
    return br, sorted(f for f in fs if f and not any((Path(cwd) / f).resolve().is_relative_to(x) for x in skip))

def peer_rows(cfg, units, hs):
    """One row per running unit: name, host, model, branch, files changed, step, jobs it waits on, report, transcript."""
    live = [u for u in units if u.get("state") not in ENDED + ("failed",) or u.get("_park")]
    with cf.ThreadPoolExecutor(8) as ex: ch = list(ex.map(lambda u: unit_changes(cfg, u, units), live))
    rows = []
    for u, (br, fs) in zip(live, ch):
        c = card_fields(cfg, u["name"])
        rows.append({"name": u["name"], "host": u.get("host"), "model": u.get("model"), "branch": br, "files": fs, "state": u.get("state"),
                     "step": c.get("step") or c.get("state") or "-", "jobs": u.get("_park") or unit_jobs(hs, u["name"]),
                     "report": str(report_path(cfg, u)) if cfg.hosts.get(u.get("host"), {}).get("local") else f"{u['host']}:{cfg.hosts[u['host']].get('root')}/out/{u['name']}/REPORT.md",
                     "transcript": transcript_where(cfg, u)})
    return rows

def peers_text(rows):
    L = [f"# Peers ({ts()}; written by orch check and the watcher; one line per running unit)"]
    for r in rows:
        fs = ", ".join(r["files"][:12]) + (f" (+{len(r['files']) - 12} more)" if len(r["files"]) > 12 else "")
        L.append(f"- {r['name']} | host {r['host']} | {r['model']} | branch {r['branch']} | {r['state']} | step: {r['step'][:80]} | "
                 f"waits on: {', '.join(r['jobs']) or '-'} | files: {fs or '-'} | report {r['report']} | transcript {r['transcript']}")
    if not rows: L.append("- none running")
    return "\n".join(L) + "\n"

def peer_findings(cfg, units, hs):
    """Writes the board folder's peers.md (a writer only: the board sync then carries it to every host) and returns
    overlap:<a>:<b> for two running units that change the same file."""
    rows = peer_rows(cfg, units, hs); F = []
    if not cfg.run.ro and not cfg.run.dry and board_dir(cfg).is_dir(): atomic_write(board_dir(cfg) / "peers.md", peers_text(rows))
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            both = sorted(set(a["files"]) & set(b["files"]))
            if both: F.append((f"overlap:{a['name']}:{b['name']}", f"{a['name']} and {b['name']} both change {', '.join(both[:4])}"
                               + (f" (+{len(both) - 4})" if len(both) > 4 else "") + ": tell them (board.sh direct), or land one first"))
    return F
