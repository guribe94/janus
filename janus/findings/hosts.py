"""Findings about hosts: unreachable, margins, admission blocked or paused, lane stalls, idle slots and job ages, orphans, thrash
acts, tidy and disk. Observations only: each clears when the condition does; nothing is stopped from here."""
import time
from janus.actions.jobs import LANES, job_of, lane_age, lane_stall, lane_wait, orphan_jobs, slots
from janus.project import label_owner, own_label, project_id, tmux_name, unit_tmux, untagged
from janus.backlog import ready_work
from janus.board import board_script
from janus.msgboard import STALE_MIN
from janus.cap import capv, host_cap
from janus.context import Err, fl, projects, ts, now
from janus.probe import margins, room
from janus.units import orphan_tabs, ws_id

def queue_waits(cfg, n, p, lands=(), most=8):
    """What host n's unfinished lane jobs are, lane by lane in queue order ('jobs: n:5 unit:a running, ...')."""
    out = []
    tasks = sorted(((p.get("pueue") or {}).get("tasks") or {}).values(), key=lambda t: t.get("id", 0))
    for lane in LANES:
        rows = [t for t in tasks if t.get("group") == lane and (t.get("status") if isinstance(t.get("status"), str) else next(iter(t.get("status") or {}), "")) in ("Queued", "Running")]
        if rows: out.append(f"{lane}: " + ", ".join(f"{n}:{t.get('id')} {t.get('label') or '?'}" for t in rows[:most]))
    return "; ".join(out) or "no queued work"

def tidy_findings(cfg):
    """disk:<host> (over its disk budget) and tidy:<n> (what tidy can reclaim), from the watcher's last tidy pass."""
    t = cfg.jget("tidy.json"); P = t.get("plan", [])
    F = [(f"disk:{x[1]}", f"host {x[1]} over its disk budget: {x[2]}; janus tidy --dry-run, then janus tidy") for x in P if x[0] == "disk"]
    rest = [x for x in P if x[0] != "disk"]
    if rest:
        kinds = {}
        for x in rest: kinds[x[0]] = kinds.get(x[0], 0) + 1
        mb = sum(x[3] for x in rest)
        F.append((f"tidy:{len(rest)}", f"tidy can reclaim {len(rest)} item(s) ({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}"
                  + (f", {mb / 1024:.1f} GB" if mb else "") + f", as of {ts(t.get('at', 0))}): janus tidy --dry-run, then janus tidy"))
    return F

def thrash_findings(cfg):
    """thrash:<when>: each stop and re-queue of the last 12 h is reported once in check (the watcher acts; check tells)."""
    return [(f"thrash:{k}", v) for k, v in cfg.jget("thrash-acts.json").items() if now() - int(k) < 12 * 3600]

def lane_findings(cfg, hs, units=()):
    """Per host and lane: queued work with free slots (stalled), queued work while the host has room (idle), a host with free
    slots while another waits, and jobs over their class age limit."""
    F, free, backlog = [], {}, {}
    for n, p in hs.items():
        t = p.get("pueue")
        if not p["ok"] or not t: continue
        h = cfg.hosts[n]; groups = t.get("groups") or {}; tasks = list((t.get("tasks") or {}).values())
        for lane in LANES:
            g = groups.get(lane)
            if not g: continue
            mine = [x for x in tasks if x.get("group") == lane]
            st = lambda x: x["status"] if isinstance(x["status"], str) else next(iter(x["status"]))
            q = [x for x in mine if st(x) == "Queued"]; r = [x for x in mine if st(x) == "Running"]; par = g.get("parallel_tasks", 1)
            wt = int(p.get(f"wait_{lane}", 0) or 0)   # running tasks still waiting in janus's own admission
            adm = (p.get("cap_admit") or "").strip(); paused = (p.get("cap_pause") or "").strip()
            if wt and (paused or (adm and adm != "yes")):   # finding 7: only a job that IS blocked (admission says no, or paused)
                why = paused   # item 21: name why a job is held in admission (disk/mem/psi with numbers), not just that it waits
                if not why and adm == "disk":
                    why = f"disk {p.get('disk_gb')}G free, floor {capv(cfg, h, 'min_disk_gb')}G"
                why = why or adm or "admission blocked"
                F.append((f"blocked:{n}:{lane}", f"{n} {lane}: {wt} job(s) waiting in admission ({why}; jobs hold {p.get('cap_rc', '?')} cores "
                          f"and {p.get('cap_rm', '?')} MB, agents {p.get('cap_ram', '?')} MB, PSI {p.get('cap_psi', '?')}): free the cause "
                          f"(janus tidy for disk, or wait for the load/PSI to fall)"))
            if q and (g.get("status") != "Running" or len(r) < par):
                F.append((f"stalled:{n}:{lane}", f"{n} {lane} STALLED: {len(q)} queued, {len(r)} of {par} slots running, group {g.get('status')}: janus check retunes the group; a paused group needs its cause fixed"))
            elif q and not wt and len(r) < slots(h, cfg) and \
                    (p["cap_admit"].strip() == "yes" if "cap_admit" in p else host_cap(cfg, n, p, list(units))["free"] > 0):
                # idle by the admission's own verdict (row 48): work queued in pueue, none held by admission, and admission would
                # take one more now; jobs waiting inside their scripts are held by admission, never idle
                F.append((f"idle:{n}:{lane}", f"{n} {lane} IDLE: {len(q)} queued in pueue, {len(r)} running, and admission would admit one now: janus queue {n} to see why pueue holds it"))
            if q: backlog.setdefault(lane, []).append(n)
            elif len(r) < par and not wt and p.get("cap_admit", "yes").strip() == "yes": free.setdefault(lane, []).append(n)   # free by admission (row 48)
            # age (row 214): a job that waits (queued, or held by admission) is over the lane's wait limit, counted from enqueue; a
            # running job is stuck only when it has run past the lane's age limit (from its START) and shows no progress (its
            # own mark, runs/<job>.live: no output and no CPU for the stall window) and waits for no job of its own. A job with
            # no mark (started by an older release) cannot be judged and is left alone.
            live = dict(l.split()[:2] for l in p.get("live", []) if len(l.split()) >= 2)
            for x in mine:
                s = x["status"]; info = s.get(st(x), {}) if isinstance(s, dict) else {}
                if st(x) not in ("Queued", "Running") or own_label(cfg, x) is None: continue   # own jobs only
                waits = st(x) == "Queued" or job_of(x) in p.get("waitjob", []); ref = f"{n}:{x['id']}"
                try: since = time.mktime(time.strptime(((not waits and info.get("start")) or info.get("enqueued_at", ""))[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError: continue
                mins = int((now() - since) / 60); tok = job_of(x); quiet = fl(live.get(tok)) if live.get(tok) is not None else None
                if waits:
                    if mins > lane_wait(cfg, lane):
                        F.append((f"age:{ref}", f"{ref} {lane} {x.get('label')} waiting {mins} min ({'queued' if st(x) == 'Queued' else 'held by admission'}), over the "
                                  f"{lane_wait(cfg, lane)} min wait limit ([lanes.{lane}] wait_min): janus queue {n} says what holds it; janus cancel {ref} and re-queue it "
                                  "where there is room, or ack with why it is right"))
                elif mins > lane_age(cfg, lane) and quiet is not None and quiet >= lane_stall(cfg, lane) * 60:
                    F.append((f"age:{ref}", f"{ref} {lane} {x.get('label')} STUCK: running {mins} min (limit {lane_age(cfg, lane)}) with no progress for {int(quiet / 60)} min "
                              f"(no output, no CPU; stall limit {lane_stall(cfg, lane)}): janus cancel {ref} and re-queue it, or ack with why it is right"))
        if int(p.get("old_rule", 0) or 0):
            F.append((f"old-rule:{n}", f"{n}: {p['old_rule']} job(s) still wait under the old cap rule ({p.get('old_rule_jobs', '').strip()[:120]}): "
                                        "re-queue them with janus run, then stop the old tasks"))
        lg, lr = int(p.get("legacy_wait", 0) or 0), int(p.get("legacy_run", 0) or 0)
        if lg and lr < h.get("legacy_slots", 3):
            F.append((f"stalled:{n}:legacy", f"{n} old-script queue STALLED: {lg} waiting, {lr} of {h.get('legacy_slots', 3)} running (its own admission holds them): move them to janus run"))
    for lane, ns in backlog.items():
        for f in free.get(lane, []):
            if cfg.hosts[f].get("local") == cfg.hosts[ns[0]].get("local"):
                F.append((f"idle:{f}:{lane}:vs", f"{f} has free {lane} slots while {', '.join(ns)} has a backlog: re-queue waiting jobs on {f} (janus cancel, janus run --host {f})"))
    return F

def other_projects(cfg):
    """(resolved root, registered orchestrator pid or '') of every project in ~/.config/janus/projects other than this one."""
    out = []
    for r in projects():
        if r.resolve() == cfg.root.resolve(): continue
        try: o = ((r / ".janus" / "session_pid").read_text().split() or [""])[0]
        except (OSError, ValueError): o = ""
        out.append((str(r.resolve()), o))
    return out

def host_findings(cfg, hs, units):
    """host:, margin:, idle:<host>, pressure:, scheduler:, orphan:job: and orphan:tmux: from each host's probe."""
    F = []; names = {u["name"] for u in units}; un = cfg.jget("hosts.json")
    orch_pid = (cfg.f("session_pid").read_text().split() or [""])[0] if cfg.f("session_pid").exists() else ""   # the orchestrator is not flagged
    others = other_projects(cfg); orch = {orch_pid, *(o for _, o in others)} - {""}; pid_self = project_id(cfg)
    def other_root(r):   # under another listed project (the deepest listed root holding r is not this project)
        hold = [x for x in [str(cfg.root.resolve())] + [x for x, _ in others] if r == x or r.startswith(x.rstrip("/") + "/")]
        return bool(hold) and max(hold, key=len) != str(cfg.root.resolve())
    ready = []   # the ready work an agent could be launched on (titles), not every ready bead
    try: ready = [str(i.get("title", ""))[:40] for i in ready_work(cfg)]
    except Err: pass
    work = f"{len(ready)} ready work item(s) in beads have no unit ({'; '.join(ready[:3])}{'...' if len(ready) > 3 else ''})"
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
            F.append((f"margin:{n}:{'+'.join(kinds)}", f"host {n} over its margins: {'; '.join(mg) or why}; launch elsewhere, or free it (janus tidy for disk)"))
        elif r > 0 and ready: F.append((f"idle:{n}", f"host {n} has room for {r} and {work}: write a brief for each and janus launch it"))
        elif ready and (p.get("cap_pause") or "").strip():   # skill-fix52: the waiting work is said with the pause, never dropped
            F.append((f"idle:{n}", f"host {n} takes no work while its admission is paused ({p['cap_pause'].strip()}), and {work}: launch on another host, or when it clears"))
        hc = host_cap(cfg, n, p, units)
        if hc["pause"]:   # back-pressure (janus-fixes8 item 2): reported, never acted on by killing
            F.append((f"pressure:{n}", f"host {n} admission PAUSED ({hc['pause']}): new jobs wait; its {hc['rj']} running job(s) are not killed; it resumes below the limits; place new work on another host meanwhile; it holds " + queue_waits(cfg, n, p)))
        # one scheduler per host (item 1): any other job-running process, or an old queue that still runs or holds work
        sch = list(p.get("sched", []))
        lg, lr = int(fl(p.get("legacy_wait"))), int(fl(p.get("legacy_run")))
        if lg or lr: sch.append(f"old queue: {lr} running, {lg} waiting (legacy_probe)")
        unt = [x for x in p.get("res", []) if " untracked " in x]
        if unt: sch.append(f"{len(unt)} job(s) running without a janus reservation ({', '.join(x.split()[-1] for x in unt[:3])})")
        if sch: F.append((f"scheduler:{n}", f"host {n} has job-running work outside its janus lanes, which must be the only admission: "
                          + "; ".join(sch[:4])[:300] + ": retire the old queue (or move its jobs to janus run)"))
        info = {x.split()[0]: x.split(None, 3)[1:] for x in p.get("apiinfo", []) if x.split()}   # pid -> [tty, owner, root]
        for a in p.get("apiproc", []):   # spend safety: an API process janus did not start spends outside the ledger
            f = a.split(None, 3)
            if len(f) < 3 or not f[0].isdigit() or (cfg.hosts[n].get("local") and f[0] in orch): continue
            tty, owner, root = (info.get(f[0], []) + ["", "", ""])[:3]
            # cross-project safety (re-check N2): another project's (its unit tree, its root or cwd, its orchestrator) and the
            # owner's own sessions (a terminal janus did not start) are neither shown nor touched; only this project's unit
            # trees are ever stopped, and only with [enforce] stop_api = true; the rest is reported
            mine = owner == pid_self
            if not mine and (owner not in ("", "-") or tty not in ("", "-", "?", "??") or other_root(root)): continue
            F.append((f"spend:proc:{n}:{f[0]}", f"Claude/Anthropic API process pid {f[0]} on {n} ({f[2]}) was not started by janus (an API key, no JANUS_UNIT): "
                      f"it spends outside the ledger and the caps: {(f[3] if len(f) > 3 else '')[:70]}; launch API work through janus"
                      + ("" if mine else "; reported only (not in this project's unit trees)")))
        for t in ((p.get("pueue") or {}).get("tasks") or {}).values():
            st = t.get("status"); k = st if isinstance(st, str) else next(iter(st), "")
            if k not in ("Running", "Queued"): continue
            lab = own_label(cfg, t)   # cross-project safety: only this project's jobs; one with no project id is machine state
            if lab is None:
                if untagged(t): F.append((f"machine:job:{n}:{t.get('id')}", f"job {t.get('id')} on {n} ({t.get('label') or t.get('command', '')[:40]}) carries no project id: machine state, not this project's; janus does not act on it"))
                continue
            owned = lab in names or (lab.startswith("unit:") and lab[5:] in names) or (t.get("group") in LANES and ":" in lab and not lab.startswith("unit:"))
            if not owned:   # a lane job labelled unit:<name> whose unit is closed is an orphan too (M3)
                F.append((f"orphan:job:{n}:{t.get('id')}", f"orphan job {t.get('id')} on {n} ({lab or t.get('command', '')[:40]}) has no open unit: janus cancel {n}:{t.get('id')}"))
        mine = {unit_tmux(u) for u in units}
        for s in p.get("tmux", []):
            if not s.startswith("janus-") or s in mine: continue
            base, pid = label_owner(s)
            if pid is None: F.append((f"machine:tmux:{n}:{s}", f"tmux session {s} on {n} carries no project id: machine state, not this project's; janus does not act on it"))
            elif pid == tmux_name(cfg, "x").rpartition("@")[2]: F.append((f"orphan:tmux:{n}:{s}", f"orphan tmux session {s} on {n} has no open unit: collect what it wrote, then stop it (janus stop, or relaunch its unit)"))
    return F

def pane_findings(cfg, units, ag):
    """orphan:pane: an agent in this project's herdr workspace with no open unit (not the orchestrator). Without the project's
    workspace no pane is this project's: nothing is reported (cross-project safety)."""
    F = []; ws = ws_id(cfg); names = {u["name"] for u in units}
    if not ws: return F
    janus_pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    pane_units = {u.get("pane") for u in units}
    for k, a in ag.items():
        if a.get("workspace_id") != ws: continue
        if k in names or a.get("pane_id") in pane_units or a.get("pane_id") == janus_pane or k == "orchestrator": continue
        F.append((f"orphan:pane:{a['pane_id']}", f"orphan agent {k} in pane {a['pane_id']} ({a.get('agent_status')}) has no open unit: janus tidy closes panes of closed units; otherwise register it as a unit or close it"))
    return F

def tab_findings(cfg, units):
    """tab:<unit>:orphan: a tab janus opened for a unit is still open although the unit is closed (close, park and stop close
    it; this one was left by an older janus or a failed close). Only tabs with janus's own label (units.orphan_tabs)."""
    return [(f"tab:{n}:orphan", f"tab {t} ({lab}) is still open but unit {n} is closed: janus tidy closes it")
            for t, n, lab in orphan_tabs(cfg, {u["name"] for u in units})]

def sync_findings(cfg, hs):
    """board:stale-sync: posts wait in a host's outbox for over STALE_MIN minutes (the host's probe asks its board.sh outbox):
    the watcher is not merging the board, so questions and blocks from that host are not seen here."""
    late = {n: int(p["outbox_min"]) for n, p in hs.items() if str(p.get("outbox_min", "")).isdigit() and int(p["outbox_min"]) > STALE_MIN}
    if not late or cfg.paths.get("board"): return []
    return [("board:stale-sync", "board posts wait unsynced on " + ", ".join(f"{n} ({m} min)" for n, m in sorted(late.items()))
             + f": the watcher's board sync is not running (it syncs every minute of a pass); run {board_script(cfg)} sync now, then see "
             + f"{cfg.f('watch.log')} (pass skipped or failed) and janus doctor (watcher not loaded: run install)")]

def orphan_job_findings(cfg, hs, units, seen):
    """orphan:job:<host>:<task> for a job whose owner is gone (orphan_jobs; the watcher cancels it), unless already reported."""
    F = []
    for r, why in orphan_jobs(cfg, hs, units):   # the watcher cancels these (item 3); check only reports them
        k = f"orphan:job:{r}"
        if k not in seen: F.append((k, f"orphan job {r}: {why}; the watcher cancels it (janus cancel {r})"))
    return F
