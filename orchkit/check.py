"""orch check, status, capacity and ack: one pass that probes every host, reads the units and collects the findings.

collect_findings returns registered Findings (findings/__init__.py); run_check filters acks, records open problems and writes
the Live part of HANDOFF.md (a writer only: the registered orchestrator or the watcher)."""
import json, re, sys
from orchkit.actions.jobs import LANES, job_state, orphan_jobs, slots, unit_jobs
from orchkit.backlog import ready_work
from orchkit.beads import Beads
from orchkit.board import board, board_dir, board_files, board_findings, board_questions, board_rows
from orchkit.budgets import unit_spend
from orchkit.cap import host_cap
from orchkit.context import Err, LB, LE, NOTES0, fl, handoff_path, handoff_stale, herdr, hrun, is_orchestrator, now, say, ts
from orchkit.findings import ackable, finding, live_acks
from orchkit.findings.hosts import enforce, lane_findings, proc_findings, script_findings, thrash_findings, tidy_findings
from orchkit.findings.memory import memory_findings, rule_findings
from orchkit.findings.units import budget_findings, parked_findings, peer_findings, report_findings, unreconciled
from orchkit.findings.work import backlog_findings, gate_findings, git_findings, known_failure_findings, landing_findings
from orchkit.ledger import committed, ledger_read, spend
from orchkit.platform import launchd_watcher_loaded
from orchkit.probe import cap_line, margins, probe_all, pueue, room
from orchkit.units import ENDED, all_units, api_out, herdr_agents, unit_state, verdict, waiting_line_jobs, ws_id

def board_upkeep(cfg, units):
    """The shipped board's own files (a writer only): .master (this machine's host name), .hosts (other hosts from orch.toml,
    for sync and rule) and .notify (a new question or block runs `orch watch --alert` at once); cards of closed units archived."""
    if cfg.run.ro or cfg.run.dry or cfg.paths.get("board"): return
    board_files(cfg)
    names = {u["name"] for u in units}; au = all_units(cfg)
    for c in (board_dir(cfg) / "agents").glob("*.md") if (board_dir(cfg) / "agents").is_dir() else []:
        if c.stem not in names and au.get(c.stem, {}).get("status") == "closed": board(cfg, "close", c.stem)

def collect_findings(cfg, hs, units, ag, bd_err):
    F = []; viol = []   # viol: the processes enforce may stop (burner, direct), found on the way
    if bd_err: F.append(("beads", f"beads unavailable: {bd_err}; fix bd (orch doctor), units cannot be read until then"))
    ws = ws_id(cfg); names = {u["name"] for u in units}
    for u in units: u["state"] = unit_state(u, hs, ag)   # finished units hold no build position (host_cap)
    un = cfg.jget("hosts.json")
    ready = 0
    try: ready = len(ready_work(cfg))
    except Err: pass
    for n, p in hs.items():
        if not p["ok"]:
            qd = [u["name"] for u in units if u.get("host") == n]
            F.append((f"host:{n}", f"host {n} unreachable since {ts(un.get(n))} ({p['err']}); units there: {', '.join(qd) or 'none'}; place new work elsewhere (unstarted jobs there: cancel and re-queue on another host)"))
            continue
        r, why = room(cfg, n, p, units)
        mg = margins(cfg.hosts[n], p, cfg=cfg)
        low = r < 1 and p.get("avail_mb") and fl(p.get("avail_mb")) / 1024 < cfg.hosts[n].get("reserve_gb", 2) * 0.6
        if mg or low:   # the key names the kinds (load, memory, disk), so an ack of one never hides another (H12)
            kinds = sorted({w.split()[0] for w in mg} | ({"memory"} if low else set()))
            F.append((f"margin:{n}:{'+'.join(kinds)}", f"host {n} over its margins: {'; '.join(mg) or why}; launch elsewhere, or free it (orch tidy for disk)"))
        elif r > 0 and ready: F.append((f"idle:{n}", f"host {n} has room for {r} but {ready} unit(s) are ready: orch launch"))
        hc = host_cap(cfg, n, p, units)
        if hc["pause"]:   # back-pressure (orch-fixes8 item 2): reported, never acted on by killing
            F.append((f"pressure:{n}", f"host {n} admission PAUSED ({hc['pause']}): new jobs wait; its {hc['rj']} running job(s) are not killed; it resumes below the limits; place new work on another host meanwhile"))
        # one scheduler per host (item 1): any other job-running process, or an old queue that still runs or holds work
        sch = list(p.get("sched", []))
        lg, lr = int(fl(p.get("legacy_wait"))), int(fl(p.get("legacy_run")))
        if lg or lr: sch.append(f"old queue: {lr} running, {lg} waiting (legacy_probe)")
        unt = [x for x in p.get("res", []) if " untracked " in x]
        if unt: sch.append(f"{len(unt)} job(s) running without an orch reservation ({', '.join(x.split()[-1] for x in unt[:3])})")
        if sch: F.append((f"scheduler:{n}", f"host {n} has job-running work outside its orch lanes, which must be the only admission: "
                          + "; ".join(sch[:4])[:300] + ": retire the old queue (or move its jobs to orch run)"))
        for d in p.get("direct", []):   # remote lane enforcement (register item 22)
            f = d.split(None, 4)
            if len(f) < 4 or not f[0].isdigit(): continue
            F.append((f"direct:{n}:{f[0]}", f"agent {f[2]} runs a build or test directly on {n} (pid {f[0]}, {int(fl(f[3])) // 60} min, outside the lanes): "
                      f"{(f[4] if len(f) > 4 else '')[:80]}; it must use {cfg.hosts[n].get('root', '~')}/run/{f[2]}.lane"))
            viol.append({"key": f"direct:{n}:{f[0]}", "kind": "direct", "host": n, "pid": int(f[0]), "pgid": int(fl(f[1])), "unit": f[2]})
        for t in ((p.get("pueue") or {}).get("tasks") or {}).values():
            st = t.get("status"); k = st if isinstance(st, str) else next(iter(st), "")
            lab = t.get("label") or ""
            owned = lab in names or (lab.startswith("unit:") and lab[5:] in names) or (t.get("group") in LANES and ":" in lab and not lab.startswith("unit:"))
            if k in ("Running", "Queued") and not owned:   # a lane job labelled unit:<name> whose unit is closed is an orphan too (M3)
                F.append((f"orphan:job:{n}:{t.get('id')}", f"orphan job {t.get('id')} on {n} ({lab or t.get('command', '')[:40]}) has no open unit: orch cancel {n}:{t.get('id')}"))
        for s in p.get("tmux", []):
            if s.startswith("orch-") and s[5:] not in names: F.append((f"orphan:tmux:{n}:{s}", f"orphan tmux session {s} on {n} has no open unit: collect what it wrote, then stop it (orch stop, or relaunch its unit)"))
    orch_pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    pane_units = {u.get("pane") for u in units}
    for k, a in ag.items():
        if ws and a.get("workspace_id") != ws: continue
        if k in names or a.get("pane_id") in pane_units or a.get("pane_id") == orch_pane or k == "orchestrator": continue
        F.append((f"orphan:pane:{a['pane_id']}", f"orphan agent {k} in pane {a['pane_id']} ({a.get('agent_status')}) has no open unit: orch tidy closes panes of closed units; otherwise register it as a unit or close it"))
    board = board_rows(cfg); running = 0
    BF = board_findings(cfg, names); F += BF; qids = board_questions(cfg) is not None
    for card, r in board.items():   # every card, including those other launchers posted (marked "not a unit")
        tag = "" if card in names else " (not a unit)"
        if r["state"] == "blocked" or (r["state"] == "question" and not qids):
            F.append((f"board:{card}:{r['state']}", f"{card}{tag} is {r['state'].upper()} on the board: {r['text']}; answer with board.sh direct"))
        if r["gone"]: F.append((f"board:{card}:gone", f"{card}{tag} GONE: its process ended without a done post: relaunch its unit from its report (--resume-of), or orch board-close it"))
        elif r["silent"] and r["state"] not in ("question", "blocked"):
            F.append((f"board:{card}:silent", f"{card}{tag} silent over {cfg.silent // 60} min on the board: read its report or transcript (orch transcript), then direct it"))
    for u in units:
        st = unit_state(u, hs, ag); v = verdict(cfg, u); u["state"] = st; u["verdict"] = v
        age = now() - u.get("started", now())
        # parked (orch-fixes9 item 1): recorded, or its turn ended with a WAITING line; no verdict yet
        pk = [] if v else ((u.get("parked") or {}).get("jobs") or (waiting_line_jobs(cfg, u) if st in ENDED or u.get("via") == "tmux" else []))
        if pk:
            u["_park"] = pk; u["state"] = st = "parked"
            F += parked_findings(cfg, hs, u, pk)
        elif st == "working":   # alive while its own jobs wait for admission: it pays to wait (cannot be acked)
            jw = [j for j in unit_jobs(hs, u["name"]) if job_state(hs, j)[0] in ("queued", "admission")]
            if jw:
                sp, how = unit_spend(cfg, u); hrs = max((now() - max(u.get("started", now()), u.get("resumed_at", 0))) / 3600, 1 / 60)
                rate = f"about ${sp / hrs:.2f}/h ({how})" if sp is not None else f"cost per hour unknown ({how})"
                F.append((f"waiting:{u['name']}", f"{u['name']} is alive while its job(s) {', '.join(jw[:4])} wait for admission, {rate}: "
                          f"orch park {u['name']} (stops it; it resumes, same session, when its jobs finish)"))
        if st in ("working", "queued", "parked"): running += 1
        if st == "blocked":
            r = herdr("agent", "read", u["name"], "--source", "detection") or {}
            F.append((f"unit:{u['name']}:blocked", f"{u['name']} blocked: {json.dumps(r)[:160]}; answer it (board.sh direct) or relaunch with what it needs"))
        elif st in ("done", "failed", "gone", "idle"):
            F.append((f"unit:{u['name']}:finished", f"{u['name']} finished ({st}; {v or 'no verdict'}); collect/close or relaunch"))
            aj = api_out(cfg, u)   # issue 32: another limit than the budget (turns) before the report; the budget cap is waste:<unit>
            if str(aj.get("subtype", "")).startswith("error") and aj.get("subtype") != "error_max_budget_usd" and not v:
                F.append((f"unit:{u['name']}:limit", f"{u['name']} stopped by its limit ({aj.get('subtype')}, ${aj.get('total_cost_usd', '?')}) without a final verdict: "
                          "resume it (orch resume --message, or --resume-of keeps its report)"))
        if not cfg.prov.get(cfg.models.get(u.get("model"), {}).get("provider"), {}).get("enabled", True):
            F.append((f"provider:{u['name']}", f"{u['name']} uses disabled provider of {u.get('model')}: stop it, or relaunch it on an enabled model"))
    F += peer_findings(cfg, units, hs)   # peers.md, overlap:<a>:<b> (orch-swarm item 4)
    board_upkeep(cfg, units)
    seen = {k for k, _ in F}
    for r, why in orphan_jobs(cfg, hs, units):   # the watcher cancels these (item 3); check only reports them
        k = f"orphan:job:{r}"
        if k not in seen: F.append((k, f"orphan job {r}: {why}; the watcher cancels it (orch cancel {r})"))
    cfg.jput("running.json", {"n": running})
    if cfg.caps.get("total") is not None and len(units) > cfg.caps["total"]: F.append(("cap:total", f"{len(units)} agents over total cap {cfg.caps['total']}: launch nothing more; close finished units"))
    for n, c in cfg.caps.get("host", {}).items():
        k = sum(1 for u in units if u.get("host") == n)
        if k > c: F.append((f"cap:{n}", f"{k} agents on {n} over its cap {c}"))
    F += gate_findings(cfg, hs) + known_failure_findings(cfg, names) + thrash_findings(cfg) + tidy_findings(cfg)
    F += lane_findings(cfg, hs, units)
    if any(h.get("local") for h in cfg.hosts.values()): F += proc_findings(cfg, viol=viol) + script_findings()
    for n, pv in cfg.prov.items():
        if "ledger" in pv:
            bad = ledger_read(cfg, pv)[1]
            if bad: F.append((f"ledger:{n}", f"{n} ledger {pv['ledger']} is malformed ({len(bad)} problem(s): {bad[0][:90]}); every launch is refused: orch ledger check, orch ledger repair"))
            runs = ledger_read(cfg, pv)[0]; odd = [k for k, r in runs.items() if r["done"] and r["budget"] and r["cost"] > 2 * r["budget"] + 1]
            if odd or ("cap_usd" in pv and spend(runs) > 1.5 * pv["cap_usd"]):   # issue 39: a spend reading that cannot be right
                F.append((f"ledger:{n}:implausible", f"{n} spend reading implausible: ${spend(runs):.2f} committed" + (f" vs cap ${pv['cap_usd']}" if "cap_usd" in pv else "")
                          + (f"; runs costing over twice their budget: {', '.join(odd[:3])}" if odd else "") + ": orch ledger check"))
            ur = unreconciled(cfg, pv, units)
            if ur: F.append((f"ledger:{n}:unreconciled", f"{len(ur)} {n} run(s) whose agent is gone have no done row (${sum(x[2] for x in ur):.2f} held as running: "
                             + ", ".join(f"{x[0]} {x[3]}" for x in ur[:3])[:240] + "): orch ledger reconcile"))
    F += backlog_findings(cfg, hs, units) + landing_findings(cfg, units, hs) + budget_findings(cfg, units) + report_findings(cfg, units)
    sk = cfg.f("session_kind").read_text().strip() if cfg.f("session_kind").exists() else ""
    if sk and sk != cfg.c.get("orchestrator", {}).get("kind", "claude"):
        F.append(("orchestrator:kind", f"the registered orchestrator is {sk}, but orch.toml [orchestrator] kind is {cfg.c.get('orchestrator', {}).get('kind')}: set it to {sk}"))
    F += git_findings(cfg) + memory_findings(cfg) + rule_findings(cfg)
    try:
        ins = Beads(cfg).issues("instruction")
        if ins: F.append(("instructions", f"{len(ins)} unclassified owner instruction(s) (close as done, or bd remember --key <subject>: same subject replaces): " + "; ".join(i['title'][:50] for i in ins[:3])))
    except Err: pass
    for s in handoff_stale(cfg, running): F.append(("handoff", s))
    if sys.platform == "darwin" and not launchd_watcher_loaded(): F.append(("watcher", "watcher com.orch.watch is not running: run install"))
    for n, pv in cfg.prov.items():
        if "cap_usd" in pv and committed(cfg, pv) >= pv["cap_usd"]: F.append((f"spend:{n}", f"{n} spend at its ${pv['cap_usd']} cap: launch on another provider, or ask the owner to raise the cap"))
    if not cfg.run.ro and not cfg.run.dry: F = enforce(cfg, F, viol)
    return [finding(k, t) for k, t in F]

def tune_pueue(cfg, hs, units):
    """Set each reachable host's pueue limits: jobs = cores // cores_per_job (admission by reservations decides what starts),
    agents = running agents plus the agents that fit by reservations (room), capped by the owner's caps."""
    for n, p in hs.items():
        t = p.get("pueue")
        if not p["ok"] or not t: continue
        h = cfg.hosts[n]; groups = t.get("groups") or {}
        run = lambda grp: sum(1 for x in (t.get("tasks") or {}).values() if x.get("group") == grp and "Running" in str(x.get("status")))
        want = {"jobs": slots(h, cfg), "agents": run("agents") + room(cfg, n, p, units)[0]}
        for grp, w in want.items():
            w = max(1, w); g = groups.get(grp)
            if g and g.get("parallel_tasks") != w: hrun(h, f"{pueue(h)} parallel -g {grp} {w}")

def live_state(cfg, hs, units, F):
    L = ["## Live state (" + ts() + ")", "Hosts:"] + ["- " + cap_line(cfg, n, p, units) for n, p in hs.items()]
    L.append("Units:")
    for u in units:
        L.append(f"- {u['name']} | {u.get('host')} | {u.get('model')} | {u.get('state', '?')}" + (f" on {' '.join(u['_park'][:4])}" if u.get("_park") else "")
                 + f" | {int((now() - u.get('started', now())) / 60)}m | {u.get('verdict') or 'no verdict'}")
    if not units: L.append("- none")
    try: rd = ready_work(cfg); L.append("Queue: " + (", ".join(i.get("title", "")[:40] for i in rd[:8]) or "empty"))
    except Err as e: L.append(f"Queue: unavailable ({e})")
    for n, pv in cfg.prov.items():
        if "cap_usd" in pv: L.append(f"Spend {n}: ${committed(cfg, pv):.2f} of ${pv['cap_usd']}")
    L.append("Problems:"); L += [f"- [{k}] {t}" for k, t in F] or ["- none"]
    return "\n".join(L)

def write_handoff(cfg, live):
    p = handoff_path(cfg); t = p.read_text() if p.exists() else ""
    notes = t.split(LE, 1)[1] if LE in t else "\n" + NOTES0
    new = f"# HANDOFF: {cfg.name}\n{LB}\n{live}\n{LE}{notes}"
    if new != t and not cfg.run.ro: p.write_text(new)

def run_check(cfg, cycle=True, writer=None):
    """Returns (findings not acknowledged, live text, probes, units). writer (default: the caller is the registered orchestrator,
    or the watcher) regenerates the Live part of HANDOFF.md, saves acks and open problems and sets pueue limits; anyone else
    only reads. The cycle stamp (last_cycle) is touched only for the orchestrator's own check."""
    cfg.run.ro = not (writer if writer is not None else is_orchestrator(cfg))
    hs = probe_all(cfg); bd_err = None; units = []
    try: units = Beads(cfg).units()
    except Err as e: bd_err = str(e)
    ag = herdr_agents(cfg)
    F = collect_findings(cfg, hs, units, ag, bd_err)
    if not cfg.run.ro: tune_pueue(cfg, hs, units)
    acks = live_acks(cfg, {k for k, _ in F}); cfg.jput("acks.json", acks)
    F = [f for f in F if f.key not in acks or not f.ackable]   # an ack never hides an unackable family (registry)
    op = cfg.jget("open.json"); op = {k: op.get(k, now()) for k, _ in F}; cfg.jput("open.json", op)
    live = live_state(cfg, hs, units, F); write_handoff(cfg, live)
    if cycle: cfg.touch("last_cycle")
    return F, live, hs, units

def cmd_check(cfg, a):
    F, live, hs, units = run_check(cfg)
    op = cfg.jget("open.json")
    if a.escalate: F = [f for f in F if now() - op.get(f.key, now()) > cfg.deadline]
    if a.json:
        return 0, [json.dumps({"read_only": cfg.run.ro, "findings": [{**f.data(), "open_since": int(op.get(f.key, now()))} for f in F]}, indent=1)]
    L = [f"[{k}] {t}" for k, t in F]
    if a.brief and len(L) > 8: L = L[:8] + [f"[more] +{len(L) - 8} more: run orch check"]
    return 0, L + (["(read-only: not the registered orchestrator session, so nothing was saved, tuned or stamped)"] if cfg.run.ro else [])

def cmd_capacity(cfg, a):
    cfg.run.ro = True; hs = probe_all(cfg)
    try: units = Beads(cfg).units()
    except Err: units = []
    ag = herdr_agents(cfg)
    for u in units: u["state"] = unit_state(u, hs, ag)   # finished units hold no agent or build position
    return 0, [cap_line(cfg, n, p, units) for n, p in hs.items()]

def cmd_status(cfg, a):
    F, live, *_ = run_check(cfg, cycle=False); return 0, [live]

def cmd_ack(cfg, a):
    k, d = a.problem, a.decision
    if not ackable(k): raise Err(f"{k} cannot be acked: it costs money every hour; act on it (orch park {k.split(':', 1)[1]})")
    acks = live_acks(cfg); acks[k] = {"t": now()}; cfg.jput("acks.json", acks); cfg.touch("last_ack")
    try:
        bd = Beads(cfg); m = re.match(r"unit:([^:]+)", k)
        u = next((u for u in bd.units() if m and u["name"] == m.group(1)), None)
        if u: i = u["id"]
        else:
            ex = bd.issues("orch-log"); i = ex[0]["id"] if ex else bd.create("orch: decisions", "orch-log")
        bd.comment(i, f"{ts()} ack {k}: {d}")
    except Err as e: say(f"ack kept locally; beads: {e}")
    say(f"acked {k}")
