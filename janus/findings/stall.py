"""A pipeline step that everything waits on must never stall in silence (skill-fix57; a project's ISSUES row 144).

land:<unit>:stalled   janus land records its final gate (runs/land-<unit>.json: pid, start, gate host, worktree) while it
                      waits for it. The land is stalled when that wait is over [landing] stall_min (default 180), or when its
                      gate jobs wait on a host whose admission is paused or says no for [landing] starve_min (default 20) while
                      a host with a landing lane has a free gates lane. The finding names the jobs, the cause, what the paused
                      host's queue holds (queue_waits) and the one recovery command: janus land <unit> --host <h> --restart.
land:<unit>:recovered the watcher ran that command itself (tend_stalled_lands), only where it is provably safe
                      (recover_refusal says why not otherwise): never a silent action.
install:blocked-by-blocker  janus commits not yet installed change the code a stalled land runs: install now, then restart.
present:<unit>        a collected or closed unit whose result was never presented to the owner (janus report --presented).
It imports jobs, probe, project and context only; check.py, watch.py and the actions build on it."""
import json, os, signal, subprocess, sys, time
from pathlib import Path
from janus.actions.jobs import job_state, task_rc, wrap_token, wrapped_jobs
from janus.context import Err, JANUS_SELF, atomic_write, now, say, sh, ts
from janus.probe import FACTS
from janus.project import own_label

GATES = ("gates", "gates-quick")
RECOVER_EVERY = 6 * 3600   # the watcher restarts one unit's land at most once in this time: a second starvation is the orchestrator's
RECOVER_SHOW = 4 * 3600    # how long land:<unit>:recovered is shown (an ack hides it for the rest)
# the files a land runs: commits that change them and are not installed are what install:blocked-by-blocker names
LAND_CODE = ("janus/actions/landing.py", "janus/landhost.py", "janus/findings/stall.py", "janus/actions/jobs.py", "janus/gates.py", "janus/cap.py")


def land_file(cfg, unit): return cfg.f("runs") / f"land-{unit}.json"

def note_land(cfg, unit, **rec):
    """Record that janus land <unit> waits for its final gate (landing.run_final_gate removes it when the gate ends). Before
    that point land refused uncommitted changes and an open merge, and the merge of the landing branch is a commit in the
    worktree; after it come the squash commit and the push. So while the record exists, stopping the land loses only gate time."""
    f = land_file(cfg, unit); f.parent.mkdir(parents=True, exist_ok=True); atomic_write(f, json.dumps({"unit": unit, **rec}))

def alive(pid):
    try: os.kill(int(pid), 0); return True
    except (OSError, ValueError, TypeError): return False

def live_lands(cfg):
    """The records of the lands that wait for their gate now (their process lives)."""
    out = []
    for f in sorted(cfg.f("runs").glob("land-*.json")) if cfg.f("runs").is_dir() else []:
        try: r = json.loads(f.read_text())
        except (OSError, ValueError): continue
        if isinstance(r, dict) and r.get("unit") and alive(r.get("pid")): out.append(r)
    return out

def land_proc(pid, unit):
    """True when pid on this machine is a `janus land <unit>` (never signal a reused pid)."""
    rc, o, _ = sh(["ps", "-p", str(pid), "-o", "command="]); w = o.split()
    return rc == 0 and "land" in w and unit in w

def _st(t): s = t.get("status"); return s if isinstance(s, str) else next(iter(s or {}), "")

def _mins(t):
    s = t.get("status"); info = (s.get(_st(t)) or {}) if isinstance(s, dict) else {}
    try: return int((now() - time.mktime(time.strptime((info.get("enqueued_at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 60)
    except ValueError: return None

def host_block(p):
    """Why a host's admission takes no new job now (the pause's cause, or the admission's no); "" when it admits."""
    paused = (p.get("cap_pause") or "").strip(); adm = (p.get("cap_admit") or "").strip()
    return paused or ("" if adm in ("", "yes") else f"admission says {adm}")

def queue_waits(cfg, n, p, lands=(), most=8):
    """Queue transparency: what host n's unfinished lane jobs ARE, lane by lane in queue order ('gates: n:5 unit:a running,
    n:7 unit:u1 queued 40 min (the gate land u1 waits for)'), so a starved critical job is seen against the rest."""
    by = {}
    tasks = ((p.get("pueue") or {}).get("tasks") or {})
    for tid, t in sorted(tasks.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
        if task_rc(t) is not None or _st(t) not in ("Queued", "Running"): continue
        lab = own_label(cfg, t); state = job_state({n: p}, f"{n}:{tid}")[0]; m = _mins(t)
        s = f"{n}:{tid} {lab or (str(t.get('label') or 'no label') + ' (not this project)')} {state}" + (f" {m} min" if m is not None and state != "running" else "")
        if lab and lab[:5] == "unit:" and lab[5:] in lands and t.get("group") in GATES: s += f" (the gate land {lab[5:]} waits for)"
        by.setdefault(t.get("group") or "default", []).append(s)
    return "; ".join(f"{g}: " + ", ".join(x[:most]) + (f", +{len(x) - most} more" if len(x) > most else "") for g, x in by.items()) or "nothing queued or running"

def land_jobs(cfg, hs, unit):
    """The land's unfinished jobs: its gate (gates lane, label unit:<unit>) and the step jobs that gate queued, on any host."""
    refs = []
    for hn, p in hs.items():
        for tid, t in ((p.get("pueue") or {}).get("tasks") or {}).items():
            if own_label(cfg, t) != f"unit:{unit}" or t.get("group") not in GATES or task_rc(t) is not None: continue
            refs.append(f"{hn}:{tid}"); tok = wrap_token(t)
            refs += [r for r in (wrapped_jobs(cfg, tok) if tok else []) if job_state(hs, r)[0] in ("queued", "admission", "running")]
    return list(dict.fromkeys(refs))

def land_hosts(cfg):
    """The hosts janus land --host takes (landhost.land_host's rule): they meet [landing] needs and are local or have git."""
    from janus.actions.jobs import host_meets, parse_needs
    ld = cfg.c.get("landing", {}); needs = parse_needs([ld["needs"]]) if ld.get("needs") else {}; fs = cfg.jget(FACTS)
    return [n for n, h in cfg.hosts.items() if host_meets(cfg, n, fs.get(n), needs)[0] and (h.get("local") or "git" in (fs.get(n) or {}).get("features", []))]

def lane_free(p):
    """A host whose gates lane would start a gate now: it answers, admits, the group runs and has a free slot."""
    t = p.get("pueue")
    if not p.get("ok") or not t or host_block(p) or int(p.get("wait_gates", 0) or 0): return False
    g = (t.get("groups") or {}).get("gates")
    busy = [x for x in (t.get("tasks") or {}).values() if x.get("group") == "gates" and task_rc(x) is None]
    return bool(g) and g.get("status") == "Running" and len(busy) < g.get("parallel_tasks", 1)

def land_stalls(cfg, hs):
    """The stalled lands, as data: unit, record, minutes, waiting jobs, {host: why it is blocked}, idle landing hosts, starved."""
    ld = cfg.c.get("landing", {}); bound, starve = ld.get("stall_min", 180), ld.get("starve_min", 20); out = []; capable = None
    for r in live_lands(cfg):
        unit = r["unit"]; mins = int((now() - (r.get("t") or now())) / 60)
        waits = [j for j in land_jobs(cfg, hs, unit) if job_state(hs, j)[0] in ("queued", "admission")]; on = {}
        for j in waits:
            hn = j.split(":")[0]; why = host_block(hs.get(hn) or {})
            if why: on.setdefault(hn, why)
        if capable is None: capable = land_hosts(cfg)
        idle = [n for n in capable if n not in on and lane_free(hs.get(n) or {})]
        starved = bool(on and idle) and mins >= starve
        if starved or mins > bound: out.append({"rec": r, "unit": unit, "mins": mins, "waits": waits, "on": on, "idle": idle, "starved": starved, "bound": bound})
    return out

def recover_refusal(cfg, s):
    """Why the watcher must not restart this stalled land itself; None when it is provably safe: the land is starved (not
    merely slow), it still waits for its gate (its record exists: nothing is squashed or pushed yet), its worktree here holds
    the merge as a commit with no open change, the recorded process is that land, and the watcher has not restarted it lately.
    A restart then loses gate time only: land --restart merges the landing branch again and gates the result."""
    r = s["rec"]; wt = r.get("wt") or ""
    if cfg.c.get("landing", {}).get("auto_recover") is False: return "[landing] auto_recover is false"
    if not s["starved"]: return "it is over the time bound but not starved beside a free landing host: a slow gate that still runs would lose its progress"
    if not Path(wt).is_dir(): return f"its worktree {wt or '(not recorded)'} is not on this machine"
    rc, o, e = sh(["git", "-C", wt, "status", "--porcelain", "--untracked-files=no"])
    if rc: return f"git cannot read {wt}"
    if o.strip(): return f"{wt} has uncommitted changes (commit them first)"
    if sh(["git", "-C", wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"])[0] == 0: return f"{wt} has a merge in progress"
    if not land_proc(r.get("pid"), s["unit"]): return f"process {r.get('pid')} is not a janus land {s['unit']} on this machine"
    prev = cfg.jget("land_recover.json").get(s["unit"])
    if prev and now() - prev.get("t", 0) < RECOVER_EVERY: return f"the watcher already restarted it at {ts(prev['t'])} (at most once in {RECOVER_EVERY // 3600} h): a second stall needs you"
    return None

def spawn_land(cfg, unit, host):
    """Start `janus land <unit> --host <host> --restart` detached from the watcher (its output: runs/land-<unit>.recover.log)."""
    log = cfg.f("runs") / f"land-{unit}.recover.log"
    with log.open("a") as f:
        f.write(f"{ts()} watcher: janus land {unit} --host {host} --restart\n"); f.flush()
        return subprocess.Popen([sys.executable, str(JANUS_SELF), "land", unit, "--host", host, "--restart"], cwd=str(cfg.root), stdin=subprocess.DEVNULL,
                                stdout=f, stderr=subprocess.STDOUT, start_new_session=True, env={**os.environ, "JANUS_ROOT": str(cfg.root)}).pid

def tend_stalled_lands(cfg, hs, units, log):
    """The watcher's recovery: each starved land that is provably safe to restart (recover_refusal) is re-run on the idle
    landing host by the one command the finding names. Recorded in land_recover.json, so check reports it."""
    done = []
    for s in land_stalls(cfg, hs):
        if cfg.run.dry or recover_refusal(cfg, s): continue
        u, to = s["unit"], s["idle"][0]; why = "; ".join(f"{h}: {w}" for h, w in s["on"].items())
        pid = spawn_land(cfg, u, to)
        ev = cfg.jget("land_recover.json"); ev[u] = {"t": int(now()), "from": why, "to": to, "pid": pid, "mins": s["mins"]}; cfg.jput("land_recover.json", ev)
        log(f"land {u} starved {s['mins']} min ({why}) while {to} is free: restarted there (pid {pid})"); done.append(u)
    return done

def stop_land(cfg, unit):
    """janus land <unit> --restart: end a land of this unit that waits for its gate on this machine, and cancel its gate jobs."""
    from janus.actions.jobs import cancel_job
    from janus.probe import probe_all
    f = land_file(cfg, unit)
    try: r = json.loads(f.read_text())
    except (OSError, ValueError): r = {}
    pid = r.get("pid")
    if pid and int(pid) != os.getpid() and alive(pid):
        if not land_proc(pid, unit): raise Err(f"--restart: process {pid} recorded in {f} is not a janus land {unit}: nothing stopped")
        if cfg.run.dry: say(f"+ stop the running land {unit} (pid {pid}) and cancel its gate jobs"); return
        os.kill(int(pid), signal.SIGTERM); t0 = now()
        while alive(pid) and now() - t0 < 10: time.sleep(0.2)
        if alive(pid): os.kill(int(pid), signal.SIGKILL)
        say(f"stopped the running land {unit} (pid {pid}, at its gate since {ts(r.get('t'))})")
    if cfg.run.dry: return
    for ref in land_jobs(cfg, probe_all(cfg), unit):
        try:
            with cfg.writer(): say(f"cancelled its gate job {ref}: " + "; ".join(cancel_job(cfg, ref)))
        except Err as e: say(f"note: gate job {ref} not cancelled ({e}): janus cancel {ref}")
    f.unlink(missing_ok=True); (cfg.f("runs") / f"land-{unit}.pid").unlink(missing_ok=True)

def stall_findings(cfg, hs, units=()):
    F = []; lands = [r["unit"] for r in live_lands(cfg)]
    for s in land_stalls(cfg, hs):
        u, r = s["unit"], s["rec"]; why = recover_refusal(cfg, s); hosts = list(s["on"]) or [h for h in [r.get("host")] if h in hs]
        if s["on"]: what = f"its gate job(s) {', '.join(s['waits'])} wait on " + "; ".join(f"{h} ({w})" for h, w in s["on"].items()) + (f" while {', '.join(s['idle'])} has a free landing lane" if s["idle"] else "; no other landing host is free now")
        else: what = "no host it waits on is paused" + (f"; {', '.join(s['idle'])} has a free landing lane" if s["idle"] else "")
        q = "; ".join(f"{h} holds {queue_waits(cfg, h, hs[h], lands)}" for h in hosts)
        cmd = f"janus land {u} --host {s['idle'][0]} --restart" if s["idle"] else f"janus land {u} --restart"
        F.append((f"land:{u}:stalled", f"land {u} STALLED: at its final gate for {s['mins']} min (bound {s['bound']}, [landing] stall_min; pid {r.get('pid')}); {what}" + (f"; {q}" if q else "")
                  + f": {cmd} (it stops this land and its gate jobs, merges again and gates" + (" there" if s["idle"] else "") + "); "
                  + (f"the watcher does not do it itself: {why}" if why else "the watcher does it on its next pass (the merge is a commit in the worktree: only gate time is lost)")))
    for u, e in cfg.jget("land_recover.json").items():
        if now() - e.get("t", 0) >= RECOVER_SHOW: continue
        log = cfg.f("runs") / f"land-{u}.recover.log"
        try: last = log.read_text().strip().splitlines()[-1][:200]
        except (OSError, IndexError): last = ""
        F.append((f"land:{u}:recovered", f"the watcher restarted land {u} at {ts(e['t'])}: it was starved {e.get('mins')} min ({e.get('from')}) while {e.get('to')} was free; "
                  f"janus land {u} --host {e.get('to')} --restart is {'running' if alive(e.get('pid')) else 'over'} (pid {e.get('pid')}; {log}" + (f", last line: {last}" if last else "") + "): confirm it landed (janus status), then ack"))
    return F

def pending_release(vf=None):
    """(source checkout, [commit subjects], [changed files]) of the janus commits the running release lacks; None when there is
    none or it cannot be known (a working tree has no VERSION; install writes: version, 'from <source>', git describe)."""
    vf = Path(vf) if vf else JANUS_SELF.parent / "VERSION"
    try: L = vf.read_text().splitlines(); src = L[1].removeprefix("from ").strip(); rev = L[2].strip().removesuffix("-dirty")
    except (OSError, IndexError): return None
    if not rev or not Path(src).is_dir(): return None
    rc, subj, _ = sh(["git", "-C", src, "log", "--format=%s", f"{rev}..HEAD"])
    if rc or not subj.strip(): return None
    return src, subj.strip().splitlines(), sh(["git", "-C", src, "diff", "--name-only", rev, "HEAD"])[1].split()

def install_findings(cfg, F, vf=None):
    """The reinstall deadlock: a stalled land runs the installed release, and commits that change the code it runs wait to be
    installed 'until the land ends'. A running janus keeps its own release folder (install builds releases side by side and
    switches `current` for new commands only), so the answer is to install now and restart the land on the new release."""
    blocked = [k for k, _ in F if k.startswith("land:") and k.endswith(":stalled")]
    pr = pending_release(vf) if blocked else None
    if not pr: return []
    src, subj, files = pr; hit = [f for f in files if f in LAND_CODE]
    if not hit: return []
    return [("install:blocked-by-blocker", f"{len(subj)} janus commit(s) in {src} are not installed ({'; '.join(subj[:4])}{', ...' if len(subj) > 4 else ''}) and change the code the stalled land runs "
             f"({', '.join(hit)}); {', '.join(blocked)} runs the installed release, so waiting for it to end before installing is a deadlock. A running janus keeps its own release "
             f"folder: install now ({src}/install --release-only), then " + "; ".join(f"janus land {k.split(':')[1]} --restart" for k in blocked) + " (the new release lands it)")]

def note_collected(cfg, unit, by):
    """A unit was collected or closed: its result is owed to the owner in chat until janus report --presented <unit>."""
    P = cfg.jget("present.json"); e = P.get(unit) or {}
    if e.get("presented"): return
    P[unit] = {"t": e.get("t") or int(now()), "by": e.get("by") or by}; cfg.jput("present.json", P)

def mark_presented(cfg, unit, summary):
    if len((summary or "").split()) < 6: raise Err("--summary \"<what you told the owner>\": the substance (what the unit proved or built, what is left, what you ask of them), not a tick")
    P = {u: e for u, e in cfg.jget("present.json").items() if not e.get("presented") or now() - e["presented"] < 7 * 86400}
    P[unit] = {**(P.get(unit) or {"t": int(now())}), "presented": int(now()), "summary": summary[:600]}; cfg.jput("present.json", P)

def present_findings(cfg, units=()):
    return [(f"present:{u}", f"{u} was {'collected' if e.get('by') == 'collect' else 'closed'} at {ts(e.get('t'))} and its result was never presented to the owner: tell the owner in chat now "
             f"what it proved or built, what is left and what you ask of them (out/{u}/REPORT.md), then janus report --presented {u} --summary \"<what you told them>\"")
            for u, e in cfg.jget("present.json").items() if not e.get("presented")]

def present_line(unit):
    """What collect and close end with: the result is presented to the owner now, not only written to files."""
    return (f"PRESENT NOW: tell the owner in chat what {unit} proved or built, what is left and what you ask of them (out/{unit}/REPORT.md), "
            f"then janus report --presented {unit} --summary \"<what you told them>\" (present:{unit} stays until then)")

def lock_findings(cfg):
    """watcher:lock: the watcher's last pass held the writer lock over [watch] lock_warn_s (default 120): the orchestrator's
    short writers (ack, remember, intent, launch, board answers through janus) waited behind it. The line names the slow steps."""
    w = cfg.jget("watch_lock.json"); bound = cfg.c.get("watch", {}).get("lock_warn_s", 120)
    if not w or now() - w.get("t", 0) > 1800 or w.get("held", 0) <= bound: return []
    return [("watcher:lock", f"the watcher's pass at {ts(w['t'])} held the writer lock {w['held']} s (bound {bound}, [watch] lock_warn_s); slowest: "
             + (", ".join(f"{n} {s} s" for n, s in (w.get("slow") or {}).items()) or "no step over 5 s")
             + ": every janus writer (ack, remember, intent, launch) waited that long; log it with janus issue and the step names (.janus/watch.log, .janus/lock.log hold the history)")]
