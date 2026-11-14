"""janus check, status and capacity: one pass that probes every host, reads the units and collects the findings.

Findings are derived from what is observed now (findings/__init__.py): nothing is acknowledged, nothing is stored as "open since",
and a finding is gone when its condition is. status is the same pass printed as a state view."""
import contextlib, json
from janus.actions.jobs import slots
from janus.actions.lifecycle import fetch_remote_outputs, sync_remote_outputs   # sync a remote report before judging parked/orphan
from janus.backlog import ready_work
from janus.beads import Beads
from janus.board import board, board_dir, board_files, board_findings
from janus.context import Err, hrun, is_orchestrator, now, ts
from janus.findings import finding
from janus.findings.hosts import host_findings, lane_findings, orphan_job_findings, pane_findings, sync_findings, tab_findings, thrash_findings, tidy_findings
from janus.findings.units import card_findings, cap_findings, ledger_findings, spend_findings, unit_findings
from janus.findings.work import backlog_findings, git_findings, orchestrator_findings, session_findings
from janus.ledger import committed
from janus.project import own_card
from janus.transcripts import card_fields
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

def collect_findings(cfg, hs, units, ag, bd_err, bd=None, fetched=None):
    """Every finding of one pass, as registered Findings, in the order check prints them (findings/*.py hold the families)."""
    F = []
    if bd_err: F.append(("beads", f"beads unavailable: {bd_err}; fix bd (janus doctor), units cannot be read until then"))
    names = {u["name"] for u in units}
    for u in units: u["state"] = unit_state(u, hs, ag)   # finished units hold no build position (host_cap)
    if bd is not None: sync_remote_outputs(cfg, units, bd, fetched)   # judge a remote unit on its synced report
    F += host_findings(cfg, hs, units) + pane_findings(cfg, units, ag) + tab_findings(cfg, units) + sync_findings(cfg, hs)
    F += board_findings(cfg, names) + card_findings(cfg, names)
    UF, running = unit_findings(cfg, hs, units, ag); F += UF
    board_upkeep(cfg, units)
    F += orphan_job_findings(cfg, hs, units, {k for k, _ in F})
    cfg.jput("running.json", {"n": running})
    F += cap_findings(cfg, units) + thrash_findings(cfg) + tidy_findings(cfg) + lane_findings(cfg, hs, units)
    F += ledger_findings(cfg, units) + backlog_findings(cfg, hs, units) + spend_findings(cfg)
    F += orchestrator_findings(cfg) + git_findings(cfg) + session_findings(cfg)
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

def status_text(cfg, hs, units, F):
    """The state view, derived now from hosts, units, the ready queue, spend and findings (nothing of it is stored)."""
    L = ["Status (" + ts() + ")", "Hosts:"] + ["- " + cap_line(cfg, n, p, units) for n, p in hs.items()]
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

def read_remote(cfg):
    """The read phase of a check, run before the writer lock is taken: probe every host and, for a writer, bring the output of
    each ended remote unit here (ssh, rsync: a slow host takes minutes, and held the lock that every ack waits for). Returns
    (probes, fetched); run_check judges and saves under the lock. A probe writes only hosts.json and the host facts, which no
    other writer touches."""
    hs = probe_all(cfg); fetched = {}
    if not cfg.run.ro:
        try: units = Beads(cfg).units()
        except Err: return hs, fetched
        ag = herdr_agents(cfg)
        for u in units: u["state"] = unit_state(u, hs, ag)
        fetched = fetch_remote_outputs(cfg, units)
    return hs, fetched

def run_check(cfg, cycle=True, writer=None, pre=None):
    """Returns (findings, status text, probes, units). writer (default: the caller is the registered orchestrator, or the watcher)
    saves board files and sets pueue limits; anyone else only reads. pre: read_remote's result when the caller took it before its
    own lock (the watcher)."""
    cfg.run.ro = not (writer if writer is not None else is_orchestrator(cfg))
    hs, fetched = pre if pre is not None else read_remote(cfg)   # the slow part, outside the lock
    with cfg.unlocked(): ag = herdr_agents(cfg)   # herdr's socket: not under the writer lock (a watcher pass holds it here)
    with cfg.writer() if not cfg.run.ro else contextlib.nullcontext():   # one writer at a time; readers lock nothing
        bd_err = None; units = []; bd = None
        try: bd = Beads(cfg); units = bd.units()   # read again under the lock: the records the sync judges and saves are current
        except Err as e: bd_err = str(e)
        F = collect_findings(cfg, hs, units, ag, bd_err, bd, fetched)
        if not cfg.run.ro:
            with cfg.unlocked(): tune_pueue(cfg, hs, units)   # ssh to every host: not under the state lock
        live = status_text(cfg, hs, units, F)
    return F, live, hs, units

def cmd_check(cfg, a):
    F, live, hs, units = run_check(cfg)
    if a.json: return 0, [json.dumps({"read_only": cfg.run.ro, "findings": [f.data() for f in F]}, indent=1)]
    mine = [f for f in F if f.family != "machine"]; mach = [f for f in F if f.family == "machine"]   # machine state: listed apart, last
    L = [f"[{k}] {t}" for k, t in mine] + (["machine state (no project id; not this project's, never acted on):"] + [f"  [{k}] {t}" for k, t in mach] if mach else [])
    if a.brief and len(L) > 8: L = L[:8] + [f"[more] +{len(L) - 8} more: run janus check"]
    return 0, L

def cmd_capacity(cfg, a):
    cfg.run.ro = True; hs = probe_all(cfg)
    try: units = Beads(cfg).units()
    except Err: units = []
    ag = herdr_agents(cfg)
    for u in units: u["state"] = unit_state(u, hs, ag)   # finished units hold no agent or build position
    return 0, [cap_line(cfg, n, p, units) for n, p in hs.items()]

def cmd_status(cfg, a):
    F, live, *_ = run_check(cfg, cycle=False); return 0, [live]
