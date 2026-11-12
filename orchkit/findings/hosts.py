"""Findings about hosts and processes: CPU burners and tests outside the lanes on this machine, changed running scripts,
lane stalls, idle slots and job ages, thrash acts, tidy and disk; enforce (warn, then stop a burner or a direct build)."""
import os, re, time
from pathlib import Path
from orchkit.actions.jobs import LANES, lane_age, orphan_jobs, slots
from orchkit.backlog import ready_work
from orchkit.beads import Beads
from orchkit.board import board
from orchkit.cap import host_cap
from orchkit.context import AGENT_RX, Err, FIXTURE_MARK, ancestors, fl, hrun, now, sh, ts
from orchkit.platform import secs
from orchkit.probe import margins, room
from orchkit.templates import STOP_GROUP
from orchkit.units import all_units, ws_id

# Test fixtures never trip live findings (orch-fixes10 item 6): the test suite runs every process fixture under a temporary root
# whose name holds FIXTURE_MARK (and sets ORCH_IN_TESTS for itself); the live checks skip a process whose command line holds the
# mark, unless the checker is the test suite (which must see its own fixtures).
def fixture_proc(cmd): return FIXTURE_MARK in (cmd or "") and not os.environ.get("ORCH_IN_TESTS")

TEST_RX = re.compile(r"\bcargo(-\w+)? (test|nextest|build|clippy|bench)\b|\bpytest\b|\b(npm|pnpm) (run )?test\b|\bxcodebuild\b.*\btest\b")

VIA_LANE = re.compile(r"orch(\.py)? (run|gate)\b|\bssh\b|\bpueue\b|\bgrep\b|\brg\b")   # plus paths.lane_scripts (project wrappers)

def via_lane(cfg, c):
    return bool(VIA_LANE.search(c)) or any(os.path.basename(x) in c for x in cfg.paths.get("lane_scripts", []))

def proc_findings(cfg, ps_text=None, viol=None):
    """On this machine (the Mac): CPU burners an agent started (busy over 10 min, under an agent or orphaned from one), and test
    or build runs outside the pueue lanes (orch run / orch gate put them in a lane). ps_text: a ps table (tests). viol: a list
    that gets each burner's process data for enforce."""
    o = ps_text if ps_text is not None else sh(["ps", "-Aww", "-o", "pid=,ppid=,pgid=,pcpu=,etime=,command="], timeout=10)[1]
    t, pg = {}, {}
    for l in o.splitlines():
        f = l.split(None, 5)
        if len(f) == 6 and f[0].isdigit(): t[int(f[0])] = (int(f[1]), fl(f[3]), f[4], f[5]); pg[int(f[0])] = int(f[2])
    up = lambda p: [q for q in ancestors({k: (v[0],) for k, v in t.items()}, p)[1:]]
    kids = {}
    for k, v in t.items(): kids.setdefault(v[0], []).append(k)
    def lane_below(p, depth=0):   # a descendant runs orch run/gate: the build goes through a lane (a shim, a wrapper script)
        return depth < 6 and any(re.search(r"orch(\.py)? (run|gate)\b", t[c][3]) or lane_below(c, depth + 1) for c in kids.get(p, []))
    launchers = {os.path.basename(m["launcher"]) for m in cfg.models.values() if m.get("launcher")}   # older project launchers
    F = []
    for p, (pp, cpu, et, c) in t.items():
        if AGENT_RX.search(c) or "pueued" in c or fixture_proc(c): continue
        anc = up(p); agent = next((q for q in anc if AGENT_RX.search(t[q][3]) or "orch.py api-run" in t[q][3] or any(x in t[q][3] for x in launchers)), None)
        lane = any("pueued" in t[q][3] for q in anc)
        if cpu >= 80 and secs(et) >= 600 and not lane and (agent or pp == 1) and not re.search(r"^/(System|usr/libexec|usr/sbin)|\.app/", c):
            F.append((f"burner:{p}", f"CPU burner pid {p} ({cpu:.0f}% for {et}) {'started by agent pid ' + str(agent) if agent else 'orphaned'}: {c[:70]}; it is stopped after [enforce] grace_min unless it moves to a lane (orch run)"))
            if viol is not None: viol.append({"key": f"burner:{p}", "kind": "burner", "host": next((n for n, h in cfg.hosts.items() if h.get("local")), "local"),
                              "pid": p, "pgid": pg.get(p, 0), "agent_pgid": pg.get(agent or -1), "unit": env_unit(p)})
        if TEST_RX.search(c) and not lane and not via_lane(cfg, c) and not any(TEST_RX.search(t[q][3]) or via_lane(cfg, t[q][3]) for q in anc) and not lane_below(p):
            F.append((f"localtest:{p}", f"test/build run on this machine outside the pueue lanes (pid {p}, {et}): {c[:70]}; use orch run"))
    return F

def env_unit(pid):
    """ORCH_UNIT of a local process (macOS ps -E appends the environment after the command), or None."""
    rc, o, e = sh(["ps", "-Eww", "-p", str(pid), "-o", "command="], timeout=10)
    rc2, c, e2 = sh(["ps", "-ww", "-p", str(pid), "-o", "command="], timeout=10)
    m = re.search(r"(?:^|\s)ORCH_UNIT=(\S+)", o[len(c.rstrip("\n")):] if o.startswith(c.rstrip("\n")) else "")
    return m.group(1) if m else None

# Stop a process group after one warning (register item 23): TERM the group, KILL what is left after 5 s. When the group also
# holds an agent (claude, codex) or pueued, only the process and its descendants are stopped, never the agent.
def enforce(cfg, F, viol):
    """Warn, then stop (orch-fixes8; register items 22, 23). The first time a violation is seen its unit gets a board direction;
    when it is still there [enforce] grace_min (default 5) later, a CPU burner's process group is stopped (stop_burners,
    default true) and a remote direct build's (stop_direct, default false: flagged and warned only). Running lane jobs are
    never touched. Only the registered orchestrator's check and the watcher act; anyone else only reports."""
    ec = cfg.c.get("enforce", {}); grace = ec.get("grace_min", 5) * 60; st = cfg.jget("enforce.json"); new = {}; notes = {}
    for v in viol:
        s = st.get(v["key"]) or {}; stop_ok = ec.get("stop_burners", True) if v["kind"] == "burner" else ec.get("stop_direct", False)
        u = v.get("unit")
        if not s.get("warned"):
            if u: board(cfg, "direct", u, f"orch: your process {v['pid']} ({v['kind']}) on {v['host']} breaks the lane rule: "
                      + ("stop it now and run builds and tests through the lane" if v["kind"] == "direct" else "stop this CPU burner now") +
                      (f"; orch stops it at {ts(now() + grace)}" if stop_ok else ""))
            s = {"warned": now(), "unit": u}; notes[v["key"]] = f"warned {u or 'no unit'} at {ts()}" + (f"; stops at {ts(now() + grace)}" if stop_ok else "")
        elif stop_ok and not s.get("stopped") and now() - s["warned"] >= grace:
            h = cfg.hosts[v["host"]]
            rc, o, e = hrun(h, STOP_GROUP % {"pid": v["pid"], "pgid": v["pgid"] if v["pgid"] != v.get("agent_pgid") else 0}, timeout=30)
            s["stopped"] = now(); notes[v["key"]] = f"STOPPED after the warning at {ts(s['warned'])}: {o.strip() or e.strip()[:80]}"
            try: cfg.f("watch.log").open("a").write(f"{ts()} enforce: {v['key']} {notes[v['key']]}\n")
            except OSError: pass
            if u:
                x = all_units(cfg).get(u)
                if x and x.get("id"): Beads(cfg).comment(x["id"], f"{ts()} orch stopped {v['kind']} pid {v['pid']} on {v['host']} after one warning")
        else: notes[v["key"]] = (f"warned at {ts(s['warned'])}" + (f"; stopped at {ts(s['stopped'])}" if s.get("stopped") else f"; stops at {ts(s['warned'] + grace)}" if stop_ok else ""))
        new[v["key"]] = s
    cfg.jput("enforce.json", new)
    return [(k, t + (f" [{notes[k]}]" if k in notes else "")) for k, t in F]

SCRIPT_RX = re.compile(r"^(?:\S*/)?(?:bash|sh|zsh|dash|ksh)\s+(?:-\S+\s+)*(/\S+)")

def held_inodes(pid):
    """Inodes of the files process pid has open (Linux /proc, else lsof); None when it cannot be told."""
    fd = Path(f"/proc/{pid}/fd")
    if fd.is_dir():
        out = set()
        for x in fd.iterdir():
            try: out.add(x.stat().st_ino)
            except OSError: pass
        return out
    rc, o, e = sh(["lsof", "-n", "-P", "-a", "-p", str(pid), "-Fi"], timeout=10)
    return {int(l[1:]) for l in o.splitlines() if l.startswith("i") and l[1:].isdigit()} if rc in (0, 1) and o else None

def script_findings():
    """Running shell scripts whose file was changed after they started (orch-fixes7, failure class 26): a shell reads its script
    as it goes, so an in-place edit makes it run from the old byte offset of the new text (a loop of 49,000 ledger rows). A
    file replaced by write-then-rename is safe (the shell keeps the old inode); only an edit of the inode it holds is flagged."""
    rc, o, e = sh(["ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,lstart=,command="], timeout=10)
    F = []
    for l in o.splitlines():
        f = l.split(None, 6)
        if len(f) < 7 or not f[0].isdigit(): continue
        m = SCRIPT_RX.match(f[6])
        if not m or fixture_proc(f[6]): continue
        try: start = time.mktime(time.strptime(" ".join(f[1:6]), "%a %b %d %H:%M:%S %Y")); st = os.stat(m.group(1))
        except (ValueError, OSError): continue
        if st.st_mtime <= start + 1: continue
        held = held_inodes(int(f[0]))
        if held is not None and st.st_ino not in held: continue   # replaced by rename: the old text keeps running, safe
        F.append((f"script:{f[0]}:{int(st.st_mtime)}", f"running script {m.group(1)} (pid {f[0]}) was changed after it started"
                  f" ({ts(st.st_mtime)}){' in place' if held else ''}: the shell may run from a wrong offset of the new text; stop it and relaunch,"
                  " and replace scripts by write-then-rename, never in place"))
    return F

def tidy_findings(cfg):
    """disk:<host> (over its disk budget) and tidy:<n> (what tidy can reclaim), from the watcher's last tidy pass."""
    t = cfg.jget("tidy.json"); P = t.get("plan", [])
    F = [(f"disk:{x[1]}", f"host {x[1]} over its disk budget: {x[2]}; orch tidy --dry-run, then orch tidy") for x in P if x[0] == "disk"]
    rest = [x for x in P if x[0] != "disk"]
    if rest:
        kinds = {}
        for x in rest: kinds[x[0]] = kinds.get(x[0], 0) + 1
        mb = sum(x[3] for x in rest)
        F.append((f"tidy:{len(rest)}", f"tidy can reclaim {len(rest)} item(s) ({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}"
                  + (f", {mb / 1024:.1f} GB" if mb else "") + f", as of {ts(t.get('at', 0))}): orch tidy --dry-run, then orch tidy"))
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
            wt = int(p.get(f"wait_{lane}", 0) or 0)   # running tasks still waiting in orch's own admission
            if q and (g.get("status") != "Running" or len(r) < par):
                F.append((f"stalled:{n}:{lane}", f"{n} {lane} STALLED: {len(q)} queued, {len(r)} of {par} slots running, group {g.get('status')}: orch check retunes the group; a paused group needs its cause fixed"))
            elif q and not wt and lane != "land" and len(r) < (h.get("gate_slots", slots(h, cfg)) if lane == "gates" else 1 if lane == "gates-quick" else slots(h, cfg)) and \
                    (p["cap_admit"].strip() == "yes" if "cap_admit" in p else host_cap(cfg, n, p, list(units))["free"] > 0):
                # idle by the admission's own verdict (row 48): work queued in pueue, none held by admission, and admission would
                # take one more now; jobs waiting inside their scripts are held by admission, never idle
                F.append((f"idle:{n}:{lane}", f"{n} {lane} IDLE: {len(q)} queued in pueue, {len(r)} running, and admission would admit one now: orch queue {n} to see why pueue holds it"))
            if q: backlog.setdefault(lane, []).append(n)
            elif len(r) < par and not wt and p.get("cap_admit", "yes").strip() == "yes": free.setdefault(lane, []).append(n)   # free by admission (row 48)
            for x in mine:
                s = x["status"]; info = s.get(st(x), {}) if isinstance(s, dict) else {}
                try: since = time.mktime(time.strptime(info.get("enqueued_at", "")[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError: continue
                if st(x) in ("Queued", "Running") and now() - since > lane_age(cfg, lane) * 60:
                    F.append((f"age:{n}:{x['id']}", f"{n}:{x['id']} {lane} {x.get('label')} {st(x).lower()} {int((now() - since) / 60)} min, over the {lane_age(cfg, lane)} min limit: orch cancel {n}:{x['id']} and re-queue it, or ack with why it is right"))
        if int(p.get("old_rule", 0) or 0):
            F.append((f"old-rule:{n}", f"{n}: {p['old_rule']} job(s) still wait under the old cap rule ({p.get('old_rule_jobs', '').strip()[:120]}): "
                                        "re-queue them with orch run, then stop the old tasks"))
        lg, lr = int(p.get("legacy_wait", 0) or 0), int(p.get("legacy_run", 0) or 0)
        if lg and lr < h.get("legacy_slots", 3):
            F.append((f"stalled:{n}:legacy", f"{n} old-script queue STALLED: {lg} waiting, {lr} of {h.get('legacy_slots', 3)} running (its own admission holds them): move them to orch run"))
    for lane, ns in backlog.items():
        for f in free.get(lane, []):
            if cfg.hosts[f].get("local") == cfg.hosts[ns[0]].get("local") and not (lane == "land"):
                F.append((f"idle:{f}:{lane}:vs", f"{f} has free {lane} slots while {', '.join(ns)} has a backlog: re-queue waiting jobs on {f} (orch cancel, orch run --host {f})"))
    return F

def host_findings(cfg, hs, units, viol):
    """host:, margin:, idle:<host>, pressure:, scheduler:, direct: (each also goes to viol for enforce), orphan:job: and
    orphan:tmux: from each host's probe."""
    F = []; names = {u["name"] for u in units}; un = cfg.jget("hosts.json")
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
    return F

def pane_findings(cfg, units, ag):
    """orphan:pane: an agent in this project's herdr workspace with no open unit (not the orchestrator)."""
    F = []; ws = ws_id(cfg); names = {u["name"] for u in units}
    orch_pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    pane_units = {u.get("pane") for u in units}
    for k, a in ag.items():
        if ws and a.get("workspace_id") != ws: continue
        if k in names or a.get("pane_id") in pane_units or a.get("pane_id") == orch_pane or k == "orchestrator": continue
        F.append((f"orphan:pane:{a['pane_id']}", f"orphan agent {k} in pane {a['pane_id']} ({a.get('agent_status')}) has no open unit: orch tidy closes panes of closed units; otherwise register it as a unit or close it"))
    return F

def orphan_job_findings(cfg, hs, units, seen):
    """orphan:job:<host>:<task> for a job whose owner is gone (orphan_jobs; the watcher cancels it), unless already reported."""
    F = []
    for r, why in orphan_jobs(cfg, hs, units):   # the watcher cancels these (item 3); check only reports them
        k = f"orphan:job:{r}"
        if k not in seen: F.append((k, f"orphan job {r}: {why}; the watcher cancels it (orch cancel {r})"))
    return F
