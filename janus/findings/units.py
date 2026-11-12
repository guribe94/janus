"""Findings about units: parked units that cannot resume, budgets nearly spent with a stale report, runs capped without
a verdict, the waste pause and a due report, unreconciled ledger runs, and peers (peers.md, overlap)."""
import concurrent.futures as cf, json, re, time
from pathlib import Path
from janus.actions.jobs import job_state, unit_jobs
from janus.autopilot import apc, held
from janus.board import board_dir, board_questions, board_rows
from janus.budgets import bset, finish_reserve, last_checkpoint, names_some, unit_spend, waste_paused, work_written
from janus.context import DIFF_X, atomic_write, fl, hrun, now, qp, sh, ts
from janus.ledger import ledger_read, recost_plan, reserved, spend
from janus.platform import herdr
from janus.project import project_id
from janus.transcripts import card_fields, transcript_where
from janus.units import ENDED, all_units, api_out, own_tree, report_path, unit_runs, unit_state, verdict, waiting_line_jobs

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
    """parked:<unit>: a job it waits on can never finish; parked:<unit>:resume: its jobs finished but the resume failed;
    parked:<unit>:finished: its jobs finished and it is still parked (item 27, the watcher resumes it on its next pass)."""
    js = {j: job_state(hs, j)[0] for j in pk}; F = []
    if any(x in ("paused", "stashed", "locked") for x in js.values()):
        F.append((f"parked:{u['name']}", f"{u['name']} is parked on {', '.join(f'{j} {x}' for j, x in js.items())}: a job that is not queued or running never resumes it: start or re-queue that job, or janus resume --force"))
    re_ = u.get("resume_error") or {}
    if re_ and all(x in ("done", "gone") for x in js.values()):
        F.append((f"parked:{u['name']}:resume", f"{u['name']}'s jobs finished but it cannot be resumed ({re_.get('why', '')[:160]}): "
                  f"janus launch --resume-of {u['name']} with a new budget, or janus close {u['name']}"))
    elif all(x in ("done", "gone") for x in js.values()):
        # item 27: its jobs all finished and it is still parked. Only the watcher's next pass would wake it, and check said
        # nothing about it, so an orchestrator reading check could not see the unit waiting (or wake it). Seen live:
        # test-exec-race2 sat parked on asgard:182-184 with no resume_error yet, and check was silent.
        F.append((f"parked:{u['name']}:finished", f"{u['name']}'s job(s) {', '.join(pk)} finished and it is still parked (no verdict): "
                  f"the watcher resumes it on its next pass; janus resume {u['name']} wakes it now"))
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
    # A status report is due at the clock checkpoints ([report] at / every_h) AND at event checkpoints: a landing, a close, an
    # owner instruction or a decision since the last report is worth telling the owner at once, not at the next clock tick.
    cp = last_checkpoint(cfg); sent = cfg.mtime("report_sent")
    if cp and sent < cp:
        F.append(("report-due", f"a status report to the owner is due (checkpoint {ts(cp)}): janus report, send it, then janus report --sent"))
    else:
        k, t = report_event(cfg)
        if t and sent < t:
            F.append(("report-due", f"a status report to the owner is due (a {k} event at {ts(t)}): janus report, send it, then janus report --sent"))
    return F

REPORT_EVENTS = ("landing", "close", "instruction", "decision")   # events worth telling the owner at once; launch is routine

def report_event(cfg):
    """(kind, time) of the newest report-worthy event (a landing, a close, an owner instruction, a decision: REPORT_EVENTS)
    recorded in events.json, or (None, 0). report-due fires at these event checkpoints, not only at the clock."""
    ev = cfg.jget("events.json"); xs = sorted((t, k) for k, t in ev.items() if k in REPORT_EVENTS)
    return (xs[-1][1], xs[-1][0]) if xs else (None, 0)

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
    # item 10 / finding 14: a tree with the landing branch's merge staged (MERGE_HEAD is the landing branch tip) counts the
    # branch's own files as the unit's, so every such unit overlapped every other. Diff against the landing branch's side of
    # the merge, not the merge base, and drop the branch's files from the status part too. A staged merge of any OTHER branch
    # (or a committed merge) is not the landing branch's: its base stays the merge base.
    mh = sh(["git", "-C", cwd, "rev-parse", "-q", "--verify", "MERGE_HEAD"])[1].strip()
    lbt = sh(["git", "-C", cwd, "rev-parse", "-q", "--verify", lb])[1].strip()
    # finding 10 (old 14): the landing branch may have moved on since `git merge`; MERGE_HEAD is still its own side of the merge
    # while it is in the landing branch's history, so it never falls back to the merge base (which counted the branch's files).
    merged = bool(mh and lbt and (mh == lbt or sh(["git", "-C", cwd, "merge-base", "--is-ancestor", mh, lbt])[0] == 0))
    base = lb if merged else sh(["git", "-C", cwd, "merge-base", "HEAD", lb])[1].strip()
    fs = set(sh(["git", "-C", cwd, "diff", "--relative", "--name-only", base, "--", "."])[1].split("\n")) if base else set()
    top = sh(["git", "-C", cwd, "rev-parse", "--show-toplevel"])[1].strip()
    names = {l[3:].split(" -> ")[-1].strip('"') for l in sh(["git", "-C", cwd, "status", "--porcelain", "-uall", "--", "."])[1].splitlines() if len(l) > 3}
    if merged:   # the landing branch's own files are not the unit's change
        inlb = set(sh(["git", "-C", cwd, "ls-tree", "-r", "--name-only", lb, "--", "."])[1].split("\n"))
        names = {n for n in names if n not in inlb}
    fs |= {str(Path(top, n).relative_to(Path(cwd).resolve())) for n in names if top}
    skip = [cfg.state.resolve(), board_dir(cfg).resolve()]   # janus's own files and the board are nobody's change (as in close)
    return br, sorted(f for f in fs if f and not any((Path(cwd) / f).resolve().is_relative_to(x) for x in skip))

def peer_rows(cfg, units, hs):
    """One row per running unit: name, host, model, branch, files changed, step, jobs it waits on, report, transcript."""
    live = [u for u in units if u.get("state") not in ENDED + ("failed",) or u.get("_park")]
    with cf.ThreadPoolExecutor(8) as ex: ch = list(ex.map(lambda u: unit_changes(cfg, u, units), live))
    rows = []
    for u, (br, fs) in zip(live, ch):
        c = card_fields(cfg, u["name"])
        rows.append({"name": u["name"], "host": u.get("host"), "model": u.get("model"), "branch": br, "files": fs, "state": u.get("state"),
                     "step": c.get("step") or c.get("state") or "-", "jobs": u.get("_park") or unit_jobs(cfg, hs, u["name"]),
                     "report": str(report_path(cfg, u)) if cfg.hosts.get(u.get("host"), {}).get("local") else f"{u['host']}:{cfg.hosts[u['host']].get('root')}/out/{u['name']}/REPORT.md",
                     "transcript": transcript_where(cfg, u)})
    return rows

def peers_text(rows):
    L = [f"# Peers ({ts()}; written by janus check and the watcher; one line per running unit)"]
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

def card_findings(cfg, names):
    """board:<card>:blocked|question|gone|silent for every card on the board, including other launchers' (marked not a unit)."""
    F = []; qids = board_questions(cfg) is not None
    me = project_id(cfg)
    for card, r in board_rows(cfg).items():   # every card, including those other launchers posted (marked "not a unit")
        pid = card_fields(cfg, card).get("project")
        if pid and pid != me: continue   # another project's card (a shared board): not shown
        tag = "" if card in names else " (not a unit)"
        if r["state"] == "blocked" or (r["state"] == "question" and not qids):
            F.append((f"board:{card}:{r['state']}", f"{card}{tag} is {r['state'].upper()} on the board: {r['text']}; answer with board.sh direct"))
        if r["gone"]: F.append((f"board:{card}:gone", f"{card}{tag} GONE: its process ended without a done post: relaunch its unit from its report (--resume-of), or janus board-close it"))
        elif r["silent"] and r["state"] not in ("question", "blocked"):
            F.append((f"board:{card}:silent", f"{card}{tag} silent over {cfg.silent // 60} min on the board: read its report or transcript (janus transcript), then direct it"))
    return F

def unit_findings(cfg, hs, units, ag):
    """(findings, running units): each unit's state and verdict (set on it), parked:, waiting:, unit:<name>:blocked|finished|
    limit, provider:<name>."""
    F = []; running = 0
    for u in units:
        st = unit_state(u, hs, ag); v = verdict(cfg, u); u["state"] = st; u["verdict"] = v
        age = now() - u.get("started", now())
        # parked (janus-fixes9 item 1): recorded, or its turn ended with a WAITING line; no verdict yet. A recorded park wins over
        # a verdict (item 11): a resumed-then-parked unit is parked, not finished.
        rec = (u.get("parked") or {}).get("jobs")
        pk = rec or ([] if v else (waiting_line_jobs(cfg, u) if st in ENDED or u.get("via") == "tmux" else []))
        if pk:
            u["_park"] = pk; u["state"] = st = "parked"
            F += parked_findings(cfg, hs, u, pk)
        elif st == "working":   # alive while its own jobs wait for admission: it pays to wait (cannot be acked)
            jw = [j for j in unit_jobs(cfg, hs, u["name"]) if job_state(hs, j)[0] in ("queued", "admission")]
            if jw:
                sp, how = unit_spend(cfg, u); hrs = max((now() - max(u.get("started", now()), u.get("resumed_at", 0))) / 3600, 1 / 60)
                rate = f"about ${sp / hrs:.2f}/h ({how})" if sp is not None else f"cost per hour unknown ({how})"
                F.append((f"waiting:{u['name']}", f"{u['name']} is alive while its job(s) {', '.join(jw[:4])} wait for admission, {rate}: "
                          f"janus park {u['name']} (stops it; it resumes, same session, when its jobs finish)"))
        if st in ("working", "queued", "parked"): running += 1
        if st == "blocked":
            r = herdr("agent", "read", u["name"], "--source", "detection") or {}
            F.append((f"unit:{u['name']}:blocked", f"{u['name']} blocked: {json.dumps(r)[:160]}; answer it (board.sh direct) or relaunch with what it needs"))
        elif st in ("done", "failed", "gone", "idle"):
            F.append((f"unit:{u['name']}:finished", f"{u['name']} finished ({st}; {v or 'no verdict'}); collect/close or relaunch"))
            aj = api_out(cfg, u)   # issue 32: another limit than the budget (turns) before the report; the budget cap is waste:<unit>
            if str(aj.get("subtype", "")).startswith("error") and aj.get("subtype") != "error_max_budget_usd" and not v:
                F.append((f"unit:{u['name']}:limit", f"{u['name']} stopped by its limit ({aj.get('subtype')}, ${aj.get('total_cost_usd', '?')}) without a final verdict: "
                          "resume it (janus resume --message, or --resume-of keeps its report)"))
        if not cfg.prov.get(cfg.models.get(u.get("model"), {}).get("provider"), {}).get("enabled", True):
            F.append((f"provider:{u['name']}", f"{u['name']} uses disabled provider of {u.get('model')}: stop it, or relaunch it on an enabled model"))
    return F, running

def cap_findings(cfg, units):
    """cap:total and cap:<host>: more agents than the owner's caps."""
    F = []
    if cfg.caps.get("total") is not None and len(units) > cfg.caps["total"]: F.append(("cap:total", f"{len(units)} agents over total cap {cfg.caps['total']}: launch nothing more; close finished units"))
    for n, c in cfg.caps.get("host", {}).items():
        k = sum(1 for u in units if u.get("host") == n)
        if k > c: F.append((f"cap:{n}", f"{k} agents on {n} over its cap {c}"))
    return F

def ledger_findings(cfg, units):
    """ledger:<provider> (malformed), ledger:<provider>:implausible, ledger:<provider>:unreconciled, ledger:<provider>:recost."""
    F = []
    for n, pv in cfg.prov.items():
        if "ledger" in pv:
            bad = ledger_read(cfg, pv)[1]
            if bad: F.append((f"ledger:{n}", f"{n} ledger {pv['ledger']} is malformed ({len(bad)} problem(s): {bad[0][:90]}); every launch is refused: janus ledger check, janus ledger repair"))
            runs = ledger_read(cfg, pv)[0]; odd = [k for k, r in runs.items() if r["done"] and r["budget"] and r["cost"] > 2 * r["budget"] + 1]
            if odd or ("cap_usd" in pv and spend(runs) > 1.5 * pv["cap_usd"]):   # issue 39: a spend reading that cannot be right
                F.append((f"ledger:{n}:implausible", f"{n} spend reading implausible: ${spend(runs):.2f} committed" + (f" vs cap ${pv['cap_usd']}" if "cap_usd" in pv else "")
                          + (f"; runs costing over twice their budget: {', '.join(odd[:3])}" if odd else "") + ": janus ledger check"))
            rc = recost_plan(cfg, pv) if not bad else []   # rows that charge a resumed run its session's whole total again
            if rc: F.append((f"ledger:{n}:recost", f"{len(rc)} {n} done row(s) charge a resumed run its session's whole total again (${sum(c - own for _, _, c, own in rc):.2f} "
                             f"counted twice: {', '.join(x[0] for x in rc[:3])}): janus ledger recost --dry-run, then janus ledger recost"))
            ur = unreconciled(cfg, pv, units)   # each check: the done spend against the committed, and runs held for agents that are gone
            done = sum(r["cost"] or 0.0 for r in runs.values() if r["done"])
            if ur: F.append((f"ledger:{n}:unreconciled", f"{len(ur)} {n} run(s) whose agent is gone have no done row (done ${done:.2f} vs committed ${spend(runs):.2f}; ${sum(x[2] for x in ur):.2f} held as running: "
                             + ", ".join(f"{x[0]} {x[3]}" for x in ur[:3])[:240] + "): janus ledger reconcile"))
    return F

def resumable_findings(cfg, units):
    """unit:<n>:resumable: a unit stopped by its budget waits for money (budget-follow item 7); unit:<n>:resume-limit: it was given a
    new budget [autopilot] relaunch_max times and stopped at it each time."""
    F = []
    for u in units:
        n = u["name"]; r = u.get("resumable"); p = cfg.models.get(u.get("model"), {}).get("provider")
        if u.get("resume_limit"):
            F.append((f"unit:{n}:resume-limit", f"{n} was given a new budget {u['resume_limit']} time(s) and was stopped at it each time ([autopilot] relaunch_max): "
                      f"read its report; split the work, resume it by hand (janus resume {n} --budget <usd>), or close it (janus close {n})"))
        if not r or u.get("state") in ("working", "queued"): continue
        need = r.get("need") or 0; kept = f"${reserved(cfg, need):.2f} of {p}'s cap is kept for it (${need:g} and the overshoot margin)"
        if apc(cfg, "enabled") and not held(cfg, n):
            F.append((f"unit:{n}:resumable", f"{n} was stopped by its budget ({r.get('why')}) and is resumable: it is resumed first, before any new unit, in its own "
                      f"session and tree, as soon as {p} has room; {kept}. To give it room now ask the owner to raise {p}'s cap_usd, or close it (janus close {n})"))
        else:
            F.append((f"unit:{n}:resumable", f"{n} was stopped by its budget ({r.get('why')}) and is resumable; {kept}: janus resume {n} --budget {need:g} continues it "
                      f"in its own session and tree, or close it (janus close {n})"))
    return F

def reserve_findings(cfg, units):
    """Budget-follow item 4, raised BEFORE the cap is reached. spend:<provider>:short: the open units' remaining budgets no longer
    fit under the cap (some cannot finish). spend:<provider>:reserve: they fit, but no new unit does. Both ask the owner now."""
    F = []
    for n, pv in cfg.prov.items():
        if not pv.get("cap_usd") or "ledger" not in pv: continue
        keep, need = finish_reserve(cfg, n, units)
        if not keep: continue
        cap = pv["cap_usd"]; c = spend(ledger_read(cfg, pv)[0]); free = round(cap - c - keep, 2); per = pv.get("budget_per_unit") or 0
        if c >= cap: continue                                          # spend:<provider> already says the cap is reached
        if free < 0:
            F.append((f"spend:{n}:short", f"{n} is ${-free:.2f} short of letting its open units finish: ${c:.2f} of its ${cap:g} cap is committed and {len(need)} open "
                      f"unit(s) still need ${keep:.2f} of their budgets ({names_some(need)}): ask the owner now (janus ask) to raise {n}'s cap_usd by at least "
                      f"${-free:.2f}, or close the unit that matters least (janus close <unit>); launch nothing new"))
        elif per and free < reserved(cfg, per):
            F.append((f"spend:{n}:reserve", f"{n} has ${free:.2f} free for new work, under one unit's ${per:g}: ${c:.2f} of its ${cap:g} cap is committed and ${keep:.2f} is "
                      f"kept for {len(need)} open unit(s) to finish ({names_some(need)}): new units are refused; ask the owner now (janus ask) whether to raise "
                      f"{n}'s cap_usd, before the cap is reached"))
    return F

def spend_findings(cfg):
    """spend:<provider>: a provider's committed spend is at its cap; spend:<provider>:90 and :80 when it passed 90 or 80 % of it
    (the highest only). Committed counts running runs at their reservation (budget plus overshoot margin)."""
    F = []
    for n, pv in cfg.prov.items():
        if "cap_usd" not in pv or not pv["cap_usd"]: continue
        runs = ledger_read(cfg, pv)[0]; c = spend(runs); done = sum(r["cost"] or 0.0 for r in runs.values() if r["done"]); cap = pv["cap_usd"]
        if c >= cap: F.append((f"spend:{n}", f"{n} spend at its ${cap} cap: launch on another provider, or ask the owner to raise the cap"))
        else:
            pct = next((p for p in (90, 80) if c >= cap * p / 100), None)
            if pct: F.append((f"spend:{n}:{pct}", f"{n} committed ${c:.2f} is over {pct} % of its ${cap} cap (${done:.2f} spent in finished runs, "
                               f"${c - done:.2f} held by running ones): tell the owner; launch only what matters most"))
    return F
