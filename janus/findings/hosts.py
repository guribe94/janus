"""Findings about hosts and processes: CPU burners and tests outside the lanes on this machine, changed running scripts,
lane stalls, idle slots and job ages, thrash acts, tidy and disk; enforce (warn, then stop a burner or a direct build)."""
import os, re, time, tomllib
from pathlib import Path
from janus.actions.jobs import LANES, lane_age, orphan_jobs, slots
from janus.project import label_owner, own_label, project_id, tmux_name, unit_tmux, untagged
from janus.backlog import ready_work
from janus.beads import Beads
from janus.board import board
from janus.cap import capv, host_cap
from janus.context import AGENT_RX, Err, FIXTURE_MARK, ancestors, fl, hrun, now, projects, sh, ts
from janus.platform import env_janus, secs
from janus.probe import margins, room
from janus.templates import STOP_GROUP
from janus.units import all_units, ws_id

# Test fixtures never trip live findings (janus-fixes10 item 6): the test suite runs every process fixture under a temporary root
# whose name holds FIXTURE_MARK (and sets JANUS_IN_TESTS for itself); the live checks skip a process whose command line holds the
# mark, unless the checker is the test suite (which must see its own fixtures).
def fixture_proc(cmd): return FIXTURE_MARK in (cmd or "") and not os.environ.get("JANUS_IN_TESTS")

TEST_RX = re.compile(r"\bcargo(-\w+)? (test|nextest|build|clippy|bench)\b|\bpytest\b|\b(npm|pnpm) (run )?test\b|\bxcodebuild\b.*\btest\b")

VIA_LANE = re.compile(r"janus(\.py)? (run|gate)\b|\bssh\b|\bpueue\b|\bgrep\b|\brg\b")   # plus paths.lane_scripts (project wrappers)

def via_lane(cfg, c):
    return bool(VIA_LANE.search(c)) or any(os.path.basename(x) in c for x in cfg.paths.get("lane_scripts", []))

def proc_findings(cfg, ps_text=None, viol=None):
    """On this machine (the Mac): CPU burners an agent started (busy over 10 min, under an agent or orphaned from one), and test
    or build runs outside the pueue lanes (janus run / janus gate put them in a lane). ps_text: a ps table (tests). viol: a list
    that gets each burner's process data for enforce."""
    o = ps_text if ps_text is not None else sh(["ps", "-Aww", "-o", "pid=,ppid=,pgid=,pcpu=,etime=,command="], timeout=10)[1]
    t, pg = {}, {}
    for l in o.splitlines():
        f = l.split(None, 5)
        if len(f) == 6 and f[0].isdigit(): t[int(f[0])] = (int(f[1]), fl(f[3]), f[4], f[5]); pg[int(f[0])] = int(f[2])
    up = lambda p: [q for q in ancestors({k: (v[0],) for k, v in t.items()}, p)[1:]]
    kids = {}
    for k, v in t.items(): kids.setdefault(v[0], []).append(k)
    def lane_below(p, depth=0):   # a descendant runs janus run/gate: the build goes through a lane (a shim, a wrapper script)
        return depth < 6 and any(re.search(r"janus(\.py)? (run|gate)\b", t[c][3]) or lane_below(c, depth + 1) for c in kids.get(p, []))
    F = []
    for p, (pp, cpu, et, c) in t.items():
        if AGENT_RX.search(c) or "pueued" in c or fixture_proc(c): continue
        anc = up(p); agent = next((q for q in anc if AGENT_RX.search(t[q][3]) or "janus.py api-run" in t[q][3]), None)
        lane = any("pueued" in t[q][3] for q in anc)
        if cpu >= 80 and secs(et) >= 600 and not lane and (agent or pp == 1) and not re.search(r"^/(System|usr/libexec|usr/sbin)|\.app/", c):
            # cross-project safety (re-check N3): whose burner it is, by project id (its own environment, else its agent's);
            # another project's is neither shown nor touched; one without an id (an agent janus did not start, an orphan) is
            # reported only; only a burner under this project's own agents is warned and stopped
            e = env_janus(p); e = e if e.get("JANUS_PROJECT") else env_janus(agent) if agent else {}
            proj = e.get("JANUS_PROJECT"); mine = proj == project_id(cfg)
            if proj and not mine: continue
            F.append((f"burner:{p}", f"CPU burner pid {p} ({cpu:.0f}% for {et}) {'started by agent pid ' + str(agent) if agent else 'orphaned'}: {c[:70]}; "
                      + ("it is stopped after [enforce] grace_min unless it moves to a lane (janus run)" if mine else "reported only (not under this project's agents)")))
            if viol is not None and mine: viol.append({"key": f"burner:{p}", "kind": "burner", "host": next((n for n, h in cfg.hosts.items() if h.get("local")), "local"),
                              "pid": p, "pgid": pg.get(p, 0), "agent_pgid": pg.get(agent or -1), "unit": e.get("JANUS_UNIT")})
        if TEST_RX.search(c) and not lane and not via_lane(cfg, c) and not any(TEST_RX.search(t[q][3]) or via_lane(cfg, t[q][3]) for q in anc) and not lane_below(p):
            e = env_janus(p) if viol is not None else {}   # an agent's (JANUS_UNIT): warned once, then stopped (enforce)
            if e.get("JANUS_PROJECT") and e["JANUS_PROJECT"] != project_id(cfg): continue   # another project's: not shown, not touched
            u = e.get("JANUS_UNIT") if e.get("JANUS_PROJECT") == project_id(cfg) else None
            F.append((f"localtest:{p}", f"test/build run on this machine outside the pueue lanes (pid {p}, {et}){' by ' + u if u else ''}: {c[:70]}; use janus run"
                      + ("; it is stopped after [enforce] grace_min" if u else "")))
            if u: viol.append({"key": f"localtest:{p}", "kind": "direct", "host": next((n for n, h in cfg.hosts.items() if h.get("local")), "local"),
                               "pid": p, "pgid": pg.get(p, 0), "agent_pgid": pg.get(agent or -1), "unit": u})
    return F

# Stop a process group after one warning (register item 23): TERM the group, KILL what is left after 5 s. When the group also
# holds an agent (claude, codex) or pueued, only the process and its descendants are stopped, never the agent.
def enforce(cfg, F, viol):
    """Warn, then stop (janus-fixes8; register items 22, 23). The first time a violation is seen its unit gets a board direction;
    when it is still there [enforce] grace_min (default 5) later, its process group is stopped: a CPU burner's (stop_burners,
    default true), an agent's direct build or test, here or on a remote host (stop_direct, default true since imp-autopilot), and
    a Claude/Anthropic API process janus did not start inside this project's unit trees (stop_api, default false: report only).
    One warning per unit: a unit warned in the last day gets no second one, its next violation is stopped at once.
    Running lane jobs are never touched. Only the registered orchestrator's check and the watcher act; anyone else only reports."""
    ec = cfg.c.get("enforce", {}); grace = ec.get("grace_min", 5) * 60; st = cfg.jget("enforce.json"); new = {}; notes = {}
    wu = {k: t for k, t in (st.get("_units") or {}).items() if now() - t < 86400}   # unit -> when it got its one warning
    for v in viol:
        s = st.get(v["key"]) or {}; stop_ok = ec.get({"burner": "stop_burners", "api": "stop_api"}.get(v["kind"], "stop_direct"), v["kind"] != "api") and v.get("stop", True)
        u = v.get("unit")
        if not s.get("warned") and u in wu and stop_ok:   # already warned once: no second warning, stopped at once
            s = {"warned": wu[u] - grace, "unit": u}
        if not s.get("warned"):
            if u: board(cfg, "direct", u, f"janus: your process {v['pid']} ({v['kind']}) on {v['host']} breaks the lane rule: "
                      + ("stop it now and run builds and tests through the lane" if v["kind"] == "direct" else "stop this CPU burner now") +
                      (f"; janus stops it at {ts(now() + grace)}" if stop_ok else ""))
            s = {"warned": now(), "unit": u}
            if u: wu[u] = now()
            notes[v["key"]] = f"warned {u or 'no unit'} at {ts()}" + (f"; stops at {ts(now() + grace)}" if stop_ok else "")
        elif stop_ok and not s.get("stopped") and now() - s["warned"] >= grace:
            h = cfg.hosts[v["host"]]
            rc, o, e = hrun(h, STOP_GROUP % {"pid": v["pid"], "pgid": v["pgid"] if v["pgid"] != v.get("agent_pgid") else 0}, timeout=30)
            s["stopped"] = now(); notes[v["key"]] = f"STOPPED after the warning at {ts(s['warned'])}: {o.strip() or e.strip()[:80]}"
            try: cfg.f("watch.log").open("a").write(f"{ts()} enforce: {v['key']} {notes[v['key']]}\n")
            except OSError: pass
            if u:
                x = all_units(cfg).get(u)
                if x and x.get("id"): Beads(cfg).comment(x["id"], f"{ts()} janus stopped {v['kind']} pid {v['pid']} on {v['host']} after one warning")
        else: notes[v["key"]] = (f"warned at {ts(s['warned'])}" + (f"; stopped at {ts(s['stopped'])}" if s.get("stopped") else f"; stops at {ts(s['warned'] + grace)}" if stop_ok else ""))
        new[v["key"]] = s
    cfg.jput("enforce.json", {**new, "_units": wu})
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
    """Running shell scripts whose file was changed after they started (janus-fixes7, failure class 26): a shell reads its script
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
            if wt:   # item 21: name why a job is held in admission (disk/mem/psi with numbers), not just that it waits
                why = (p.get("cap_pause") or "").strip()
                if not why and p.get("cap_admit", "").strip() == "disk":
                    why = f"disk {p.get('disk_gb')}G free, floor {capv(cfg, h, 'min_disk_gb')}G"
                why = why or (p.get("cap_admit") or "admission blocked").strip()
                F.append((f"blocked:{n}:{lane}", f"{n} {lane}: {wt} job(s) waiting in admission ({why}; jobs hold {p.get('cap_rc', '?')} cores "
                          f"and {p.get('cap_rm', '?')} MB, agents {p.get('cap_ram', '?')} MB, PSI {p.get('cap_psi', '?')}): free the cause "
                          f"(janus tidy for disk, or wait for the load/PSI to fall), or cancel and re-queue elsewhere"))
            if q and (g.get("status") != "Running" or len(r) < par):
                F.append((f"stalled:{n}:{lane}", f"{n} {lane} STALLED: {len(q)} queued, {len(r)} of {par} slots running, group {g.get('status')}: janus check retunes the group; a paused group needs its cause fixed"))
            elif q and not wt and lane != "land" and len(r) < (h.get("gate_slots", slots(h, cfg)) if lane == "gates" else 1 if lane == "gates-quick" else slots(h, cfg)) and \
                    (p["cap_admit"].strip() == "yes" if "cap_admit" in p else host_cap(cfg, n, p, list(units))["free"] > 0):
                # idle by the admission's own verdict (row 48): work queued in pueue, none held by admission, and admission would
                # take one more now; jobs waiting inside their scripts are held by admission, never idle
                F.append((f"idle:{n}:{lane}", f"{n} {lane} IDLE: {len(q)} queued in pueue, {len(r)} running, and admission would admit one now: janus queue {n} to see why pueue holds it"))
            if q: backlog.setdefault(lane, []).append(n)
            elif len(r) < par and not wt and p.get("cap_admit", "yes").strip() == "yes": free.setdefault(lane, []).append(n)   # free by admission (row 48)
            for x in mine:
                s = x["status"]; info = s.get(st(x), {}) if isinstance(s, dict) else {}
                try: since = time.mktime(time.strptime(info.get("enqueued_at", "")[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError: continue
                if st(x) in ("Queued", "Running") and now() - since > lane_age(cfg, lane) * 60 and own_label(cfg, x) is not None:   # own jobs only
                    F.append((f"age:{n}:{x['id']}", f"{n}:{x['id']} {lane} {x.get('label')} {st(x).lower()} {int((now() - since) / 60)} min, over the {lane_age(cfg, lane)} min limit: janus cancel {n}:{x['id']} and re-queue it, or ack with why it is right"))
        if int(p.get("old_rule", 0) or 0):
            F.append((f"old-rule:{n}", f"{n}: {p['old_rule']} job(s) still wait under the old cap rule ({p.get('old_rule_jobs', '').strip()[:120]}): "
                                        "re-queue them with janus run, then stop the old tasks"))
        lg, lr = int(p.get("legacy_wait", 0) or 0), int(p.get("legacy_run", 0) or 0)
        if lg and lr < h.get("legacy_slots", 3):
            F.append((f"stalled:{n}:legacy", f"{n} old-script queue STALLED: {lg} waiting, {lr} of {h.get('legacy_slots', 3)} running (its own admission holds them): move them to janus run"))
    for lane, ns in backlog.items():
        for f in free.get(lane, []):
            if cfg.hosts[f].get("local") == cfg.hosts[ns[0]].get("local") and not (lane == "land"):
                F.append((f"idle:{f}:{lane}:vs", f"{f} has free {lane} slots while {', '.join(ns)} has a backlog: re-queue waiting jobs on {f} (janus cancel, janus run --host {f})"))
    return F

def other_projects(cfg):
    """(resolved root, registered orchestrator pid or '') of every project in ~/.config/janus/projects other than this one."""
    out = []
    for r in projects():
        if r.resolve() == cfg.root.resolve(): continue
        try: st = r / tomllib.loads((r / "janus.toml").read_text()).get("project", {}).get("state", ".janus"); o = ((st / "session_pid").read_text().split() or [""])[0]
        except (OSError, ValueError): o = ""
        out.append((str(r.resolve()), o))
    return out

def host_findings(cfg, hs, units, viol):
    """host:, margin:, idle:<host>, pressure:, scheduler:, direct: (each also goes to viol for enforce), orphan:job: and
    orphan:tmux: from each host's probe."""
    F = []; names = {u["name"] for u in units}; un = cfg.jget("hosts.json")
    orch_pid = (cfg.f("session_pid").read_text().split() or [""])[0] if cfg.f("session_pid").exists() else ""   # the orchestrator is not flagged
    others = other_projects(cfg); orch = {orch_pid, *(o for _, o in others)} - {""}; pid_self = project_id(cfg)
    def other_root(r):   # under another listed project (the deepest listed root holding r is not this project)
        hold = [x for x in [str(cfg.root.resolve())] + [x for x, _ in others] if r == x or r.startswith(x.rstrip("/") + "/")]
        return bool(hold) and max(hold, key=len) != str(cfg.root.resolve())
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
            F.append((f"margin:{n}:{'+'.join(kinds)}", f"host {n} over its margins: {'; '.join(mg) or why}; launch elsewhere, or free it (janus tidy for disk)"))
        elif r > 0 and ready: F.append((f"idle:{n}", f"host {n} has room for {r} but {ready} unit(s) are ready: janus launch"))
        hc = host_cap(cfg, n, p, units)
        if hc["pause"]:   # back-pressure (janus-fixes8 item 2): reported, never acted on by killing
            F.append((f"pressure:{n}", f"host {n} admission PAUSED ({hc['pause']}): new jobs wait; its {hc['rj']} running job(s) are not killed; it resumes below the limits; place new work on another host meanwhile"))
        # one scheduler per host (item 1): any other job-running process, or an old queue that still runs or holds work
        sch = list(p.get("sched", []))
        lg, lr = int(fl(p.get("legacy_wait"))), int(fl(p.get("legacy_run")))
        if lg or lr: sch.append(f"old queue: {lr} running, {lg} waiting (legacy_probe)")
        unt = [x for x in p.get("res", []) if " untracked " in x]
        if unt: sch.append(f"{len(unt)} job(s) running without a janus reservation ({', '.join(x.split()[-1] for x in unt[:3])})")
        if sch: F.append((f"scheduler:{n}", f"host {n} has job-running work outside its janus lanes, which must be the only admission: "
                          + "; ".join(sch[:4])[:300] + ": retire the old queue (or move its jobs to janus run)"))
        for d in p.get("direct", []):   # remote lane enforcement (register item 22)
            f = d.split(None, 4)
            if len(f) < 4 or not f[0].isdigit(): continue
            F.append((f"direct:{n}:{f[0]}", f"agent {f[2]} runs a build or test directly on {n} (pid {f[0]}, {int(fl(f[3])) // 60} min, outside the lanes): "
                      f"{(f[4] if len(f) > 4 else '')[:80]}; it must use {cfg.hosts[n].get('root', '~')}/run/{f[2]}.lane"))
            viol.append({"key": f"direct:{n}:{f[0]}", "kind": "direct", "host": n, "pid": int(f[0]), "pgid": int(fl(f[1])), "unit": f[2]})
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
                      + ("; it is in a unit's tree: stopped after [enforce] grace_min when [enforce] stop_api = true" if mine else "; reported only (not in this project's unit trees)")))
            viol.append({"key": f"spend:proc:{n}:{f[0]}", "kind": "api", "host": n, "pid": int(f[0]), "pgid": int(fl(f[1])), "unit": None, "stop": mine})
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

def orphan_job_findings(cfg, hs, units, seen):
    """orphan:job:<host>:<task> for a job whose owner is gone (orphan_jobs; the watcher cancels it), unless already reported."""
    F = []
    for r, why in orphan_jobs(cfg, hs, units):   # the watcher cancels these (item 3); check only reports them
        k = f"orphan:job:{r}"
        if k not in seen: F.append((k, f"orphan job {r}: {why}; the watcher cancels it (janus cancel {r})"))
    return F
