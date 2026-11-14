"""Findings about units, boards and spend: parked units that cannot resume, board cards (blocked, question, gone, silent),
agent caps, the provider ledgers and committed spend. Each is derived from what is observed now and clears when it clears."""
import json, time
from janus.actions.jobs import job_state, unit_jobs
from janus.board import board_questions, board_rows
from janus.context import now
from janus.ledger import ledger_read, recost_plan, spend
from janus.platform import herdr
from janus.project import project_id
from janus.transcripts import card_fields
from janus.units import ENDED, all_units, busy_dead, unit_runs, unit_state, verdict, waiting_line_jobs

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
        if card == "orchestrator": continue   # ISSUES row 59: its own card, made by its status posts, was judged like an agent's
        # and raised board:orchestrator:silent to the orchestrator itself 30 min after each post, while it cycled (it does not
        # post per cycle; the watcher judges its cycle). Also for a project's own board script, which may still flag it.
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
                F.append((f"waiting:{u['name']}", f"{u['name']} is alive while its job(s) {', '.join(jw[:4])} wait for admission: "
                          f"janus park {u['name']} (stops it; it resumes, same session, when its jobs finish)"))
        if st in ("working", "queued", "parked"): running += 1
        b = busy_dead(u)
        if b: F.append((f"unit:{u['name']}:interrupted", f"{u['name']}: its {b['op']} (pid {b.get('pid')}) stopped before it finished, so the unit is marked {'starting' if b['op'] == 'launch' else 'closing'}: "
                        + ("janus cancel / close it and launch again (its host copy and budget run are named in the record)" if b["op"] == "launch" else f"run janus close {u['name']} again (it skips the checks that already passed)")))
        if st == "blocked":
            r = herdr("agent", "read", u["name"], "--source", "detection") or {}
            F.append((f"unit:{u['name']}:blocked", f"{u['name']} blocked: {json.dumps(r)[:160]}; answer it (board.sh direct) or relaunch with what it needs"))
        elif st in ("done", "failed", "gone", "idle"):
            F.append((f"unit:{u['name']}:finished", f"{u['name']} finished ({st}; {v or 'no verdict'}); collect/close or relaunch"))
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
