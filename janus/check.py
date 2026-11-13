"""janus check, status, capacity and ack: one pass that probes every host, reads the units and collects the findings.

collect_findings returns registered Findings (findings/__init__.py); run_check filters acks, records open problems and writes
the Live part of HANDOFF.md (a writer only: the registered orchestrator or the watcher)."""
import contextlib, json, re
from janus.actions.jobs import slots
from janus.actions.lifecycle import sync_remote_outputs   # item 11 addendum: sync a remote report before judging parked/orphan
from janus.backlog import ready_work
from janus.autopilot import autopilot_findings, unattended_findings
from janus.beads import Beads
from janus.board import board, board_dir, board_files, board_findings
from janus.context import DECISION_ACKS, Err, LB, note_event, LE, NOTES0, atomic_write, handoff_path, hrun, is_orchestrator, now, say, ts
from janus.decisions import decision_findings
from janus.findings import ackable, ack_ttl, finding, live_acks
from janus.findings.hosts import (enforce, host_findings, lane_findings, orphan_job_findings, pane_findings, proc_findings,
    script_findings, thrash_findings, tidy_findings)
from janus.findings.awareness import note_findings, related_findings   # imp-awareness: related:<a>:<b>, note:<agent>:<n>
from janus.findings.memory import memory_findings, rule_findings
from janus.findings.quality import quality_findings, review_findings, test_signal_findings
from janus.findings.sched import overrun_findings
from janus.findings.units import (budget_findings, cap_findings, card_findings, ledger_findings, peer_findings,
    report_findings, reserve_findings, resumable_findings, spend_findings, unit_findings)
from janus.findings.work import (backlog_findings, gate_findings, git_findings, known_failure_findings, landing_findings,
    orchestrator_findings, session_findings)
from janus.ledger import committed
from janus.issue import auto_log_issues, repeated_issue_findings
from janus.project import own_card
from janus.transcripts import card_fields
from janus.intents import intent_findings, intent_lines
from janus.reset import context_findings
from janus.probe import cap_line, probe_all, pueue, room
from janus.units import all_units, herdr_agents, unit_state

def board_upkeep(cfg, units):
    """The shipped board's own files (a writer only): .master (this machine's host name), .hosts (other hosts from janus.toml,
    for sync and rule) and .notify (a new question or block runs `janus watch --alert` at once); cards of closed units archived."""
    if cfg.run.ro or cfg.run.dry or cfg.paths.get("board"): return
    board_files(cfg)
    names = {u["name"] for u in units}; au = all_units(cfg)
    for c in (board_dir(cfg) / "agents").glob("*.md") if (board_dir(cfg) / "agents").is_dir() else []:
        if c.stem not in names and au.get(c.stem, {}).get("status") == "closed" and own_card(cfg, card_fields(cfg, c.stem)): board(cfg, "close", c.stem)

def collect_findings(cfg, hs, units, ag, bd_err, bd=None):
    """Every finding of one pass, as registered Findings, in the order check prints them (findings/*.py hold the families)."""
    F = []; viol = []   # viol: the processes enforce may stop (burner, direct), found on the way
    if bd_err: F.append(("beads", f"beads unavailable: {bd_err}; fix bd (janus doctor), units cannot be read until then"))
    names = {u["name"] for u in units}
    for u in units: u["state"] = unit_state(u, hs, ag)   # finished units hold no build position (host_cap)
    if bd is not None: sync_remote_outputs(cfg, units, bd)   # item 11 addendum: judge a remote unit on its synced report
    F += host_findings(cfg, hs, units, viol) + pane_findings(cfg, units, ag)
    F += board_findings(cfg, names) + card_findings(cfg, names) + note_findings(cfg)
    UF, running = unit_findings(cfg, hs, units, ag); F += UF
    F += autopilot_findings(cfg, units)   # unit:<n>:relaunch: a remote unit the autopilot leaves to the orchestrator (fix-core F4)
    F += peer_findings(cfg, units, hs)   # peers.md, overlap:<a>:<b> (janus-swarm item 4)
    F += related_findings(cfg, units, hs)   # related:<a>:<b> when two running units' work meets (imp-awareness)
    board_upkeep(cfg, units)
    F += orphan_job_findings(cfg, hs, units, {k for k, _ in F})
    cfg.jput("running.json", {"n": running})
    F += cap_findings(cfg, units)
    F += gate_findings(cfg, hs) + known_failure_findings(cfg, names) + thrash_findings(cfg) + tidy_findings(cfg) + context_findings(cfg)
    F += test_signal_findings(cfg) + quality_findings(cfg, units) + review_findings(cfg, units)
    F += lane_findings(cfg, hs, units) + overrun_findings(cfg, hs)
    if any(h.get("local") for h in cfg.hosts.values()): F += proc_findings(cfg, viol=viol) + script_findings()
    F += ledger_findings(cfg, units)
    F += backlog_findings(cfg, hs, units) + landing_findings(cfg, units, hs) + budget_findings(cfg, units) + report_findings(cfg, units)
    F += orchestrator_findings(cfg) + git_findings(cfg) + memory_findings(cfg) + rule_findings(cfg) + session_findings(cfg, running, units) + intent_findings(cfg, units)
    F += decision_findings(cfg)
    F += repeated_issue_findings(cfg)
    F += spend_findings(cfg) + reserve_findings(cfg, units) + resumable_findings(cfg, units) + unattended_findings(cfg, units)
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
    L += intent_lines(cfg)   # skill-fix52: what is meant and not finished survives a context clear here
    L.append("Problems:"); L += [f"- [{k}] {t}" for k, t in F] or ["- none"]
    return "\n".join(L)

def write_handoff(cfg, live):
    p = handoff_path(cfg); t = p.read_text() if p.exists() else ""
    notes = t.split(LE, 1)[1] if LE in t else "\n" + NOTES0
    new = f"# HANDOFF: {cfg.name}\n{LB}\n{live}\n{LE}{notes}"
    if new != t and not cfg.run.ro: atomic_write(p, new)

def acks_after(cfg, F, hs):
    """The acks still in force after this check. An ack whose finding is gone is dropped only when every host answered: a
    finding read from a host (a unit's files, its jobs) is not gone because the host did not answer once (row 234: each
    flap dropped the acks of overlap:/related: and they came back unacked)."""
    return live_acks(cfg, {k for k, _ in F} if all(p.get("ok") for p in hs.values()) else None)

def run_check(cfg, cycle=True, writer=None):
    """Returns (findings not acknowledged, live text, probes, units). writer (default: the caller is the registered orchestrator,
    or the watcher) regenerates the Live part of HANDOFF.md, saves acks and open problems and sets pueue limits; anyone else
    only reads. The cycle stamp (last_cycle) is touched only for the orchestrator's own check."""
    cfg.run.ro = not (writer if writer is not None else is_orchestrator(cfg))
    with cfg.writer() if not cfg.run.ro else contextlib.nullcontext():   # one writer at a time (panel problem 2); readers lock nothing
        hs = probe_all(cfg); bd_err = None; units = []; bd = None
        try: bd = Beads(cfg); units = bd.units()
        except Err as e: bd_err = str(e)
        ag = herdr_agents(cfg)
        F = collect_findings(cfg, hs, units, ag, bd_err, bd)
        if not cfg.run.ro and not cfg.run.dry: auto_log_issues(cfg)   # a key acked 3+ times earns an issue row once
        if not cfg.run.ro: tune_pueue(cfg, hs, units)
        acks = acks_after(cfg, F, hs); cfg.jput("acks.json", acks)
        F = [f for f in F if f.key not in acks or not f.ackable]   # an ack never hides an unackable family (registry)
        op = cfg.jget("open.json"); op = {k: op.get(k, now()) for k, _ in F}; cfg.jput("open.json", op)
        live = live_state(cfg, hs, units, F); write_handoff(cfg, live)
        if cycle: cfg.touch("last_cycle")
    return F, live, hs, units

def cmd_check(cfg, a):
    if getattr(a, "acked", False):   # the acked-findings view: what an ack is hiding, and for how long (audit acked-findings-hidden)
        acks = live_acks(cfg)
        return 0, ([f"[{k}] acked {int((now() - v.get('t', 0)) / 60)} min ago (ttl {ack_ttl(k) // 60} min): {v.get('d', '')}"
                    for k, v in sorted(acks.items())] or ["(no live acks)"])
    F, live, hs, units = run_check(cfg)
    op = cfg.jget("open.json")
    if a.escalate: F = [f for f in F if now() - op.get(f.key, now()) > cfg.deadline]
    if a.json:
        return 0, [json.dumps({"read_only": cfg.run.ro, "findings": [{**f.data(), "open_since": int(op.get(f.key, now()))} for f in F]}, indent=1)]
    mine = [f for f in F if f.family != "machine"]; mach = [f for f in F if f.family == "machine"]   # machine state: listed apart, last
    L = [f"[{k}] {t}" for k, t in mine] + (["machine state (no project id; not this project's, never acted on):"] + [f"  [{k}] {t}" for k, t in mach] if mach else [])
    if a.brief and len(L) > 8: L = L[:8] + [f"[more] +{len(L) - 8} more: run janus check"]
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
    if not ackable(k):   # row 229: an ack of the instructions finding classified nothing and hid it; only closing or remembering clears it
        if k.split(":", 1)[0] == "instructions":
            raise Err("instructions cannot be acked: classify each instruction instead: bd close <id> once it is done, or bd remember --key <subject> for a standing rule (janus decide --instruction <id> for a decision)")
        if k.split(":", 1)[0] == "handoff":   # row 238: 12 acks in one evening hid stale notes; only writing them clears it
            raise Err("handoff cannot be acked: update the Orchestrator notes in the state folder's HANDOFF.md (what changed and what is next; move old notes to history.md when it is too long)")
        raise Err(f"{k} cannot be acked: it costs money every hour; act on it (janus park {k.split(':', 1)[-1]})")
    if k.split(":", 1)[0] in ("idle", "landing") and re.match(r"(?i)^\s*(?:i |we )?(?:will|'ll|am going to|are going to|going to|plan|intend|shall)\b", d or ""):
        raise Err(f"ack {k} with what was DONE, not what you will do (a plan is not a decision): run it, then ack")
    if k == "idle-backlog:nobrief":   # skill-fix52: an ack hides ready items without briefs once; the next needs progress
        st = cfg.jget("nobrief.json")
        if st.get("acked") is not None and st.get("n", 0) >= st["acked"]:
            raise Err(f"{k} was acked at {st['acked']} item(s) without a brief and {st.get('n', 0)} still have none: an ack hides it once, then the count must "
                      "drop. Get the briefs written (janus brief draft <items>), or janus backlog set <id> actor=owner for what no agent does")
        cfg.jput("nobrief.json", {**st, "acked": st.get("n", 0)})
    acks = live_acks(cfg); acks[k] = {"t": now(), "d": d}; cfg.jput("acks.json", acks); cfg.touch("last_ack")
    cnt = cfg.jget("ack-counts.json"); cnt[k] = cnt.get(k, 0) + 1; cfg.jput("ack-counts.json", cnt)   # a key acked 3+ times earns an issue row
    if getattr(a, "decision_class", False) or k.split(":", 1)[0] in DECISION_ACKS: note_event(cfg, "decision")   # routine acks leave the notes fresh
    try:
        bd = Beads(cfg); m = re.match(r"unit:([^:]+)", k)
        u = next((u for u in bd.units() if m and u["name"] == m.group(1)), None)
        if u: i = u["id"]
        else:
            ex = bd.issues("janus-log"); i = ex[0]["id"] if ex else bd.create("janus: decisions", "janus-log")
        bd.comment(i, f"{ts()} ack {k}: {d}")
    except Err as e: say(f"ack kept locally; beads: {e}")
    say(f"acked {k}")
