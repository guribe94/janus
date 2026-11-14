"""One probe per host, parallel and over one ssh; and the readings derived from it (section 7, Step 2).

probe/probe_all (the PROBE script, margins, room, host_cap), probe_extra (old queues, other schedulers, direct builds),
pueue(), margins/room/cap_line. Unreachable hosts degrade to a finding with the units they held (section 9: keep)."""
from janus.context import hrun, qp, fl, sh, now, FIXTURE_MARK, SSH
from janus.cap import cap_lib, capv, host_cap, FACTS
from janus.platform import HOST_FACTS, parse_facts
from janus.templates import API_PROBE, PROBE, DIRECT_PROBE
import concurrent.futures as cf, json, shlex

# Remote lane enforcement (issue 22): processes on a Linux host whose environment carries JANUS_UNIT (an agent and what it starts),
# whose command is a build or test, and that do not run under pueued (a lane job): printed as direct=<pid> <pgid> <unit> <secs> <cmd>.
# Only the topmost such process of a tree is printed; a call of the unit's lane runner (<root>/run/<unit>.lane) is the lane.
def probe_extra(h):
    """Running janus jobs still waiting in their admission (per lane), the old scripts' queue, other job schedulers on the host
    (one scheduler per host, janus-fixes8), and on remote hosts builds or tests an agent runs outside the lanes (issue 22)."""
    rd = h["_rd"]
    # a .wait/.gwait file holds '<pid> <since>'; its job waits while that pid lives (row 231: the pid is read by the shell's own read)
    # old_rule: waiting jobs whose script sources neither runs/cap.sh nor runs/admit.sh (queued before the measured rule; audit H5)
    w = (f'g=0; j=0; o=0; ol=; for f in {qp(rd)}/*.gwait {qp(rd)}/*.wait; do [ -e "$f" ] && read p _ < "$f" && kill -0 "$p" 2>/dev/null || continue; '
         f'b=${{f##*/}}; echo "waitjob=${{b%.*}}"; case $f in *.gwait) g=$((g+1));; *) j=$((j+1));; esac; s=${{f%.*}}.sh; grep -qE "cap.sh|admit.sh" "$s" 2>/dev/null || {{ o=$((o+1)); ol="$ol ${{s##*/}}"; }}; done; '
         f'echo "wait_gates=$g"; echo "wait_jobs=$j"; echo "old_rule=$o"; echo "old_rule_jobs=$ol"; '
         # row 214: seconds since each running job last showed progress (its own mark, written by the job script or the gate wrapper)
         f'nowt=$(date +%s); for f in {qp(rd)}/*.live; do [ -e "$f" ] || continue; b=${{f##*/}}; echo "live=${{b%.live}} $((nowt - $(cat "$f" 2>/dev/null || echo $nowt)))"; done')
    # schedulers (janus.toml, per host): extended regexes for the command lines of other job-running processes (an old queue); each
    # running one prints sched=<pid> <command>. legacy_lanes (the Mac): pid-file folders of an old lane; a live pid is one too.
    for rx in h.get("schedulers", []):
        w += f"\nps -A -o pid=,command= | grep -E {shlex.quote(rx)} | grep -v -e grep -e pueued -e 'cap_report' -e {FIXTURE_MARK} | head -5 | sed 's/^ */sched=/'"
    for s in h.get("legacy_lanes", "").split():
        w += f'\no=$(cat {shlex.quote(s)}/pid 2>/dev/null); [ -n "$o" ] && kill -0 "$o" 2>/dev/null && echo "sched=$o old lane {s}"'
    if not h.get("local"): w += "\n" + DIRECT_PROBE % {"rx": shlex.quote(BUILD_ERE)}
    # board:stale-sync: minutes the host's oldest board post has waited in its outbox (an older shipped board prints nothing)
    if not h.get("local") and h.get("root"): w += f'\nb={qp(h["root"])}/bin/board.sh; [ -x "$b" ] && m=$("$b" outbox 2>/dev/null) && echo "outbox_min=$m"; true'
    w += "\n" + API_PROBE   # every host: Claude/Anthropic API processes janus did not start (spend safety)
    # legacy_probe (janus.toml, per host): shell that prints legacy_wait=<n> and legacy_run=<n> for a project's older job queue
    # while it drains, so a jammed old queue shows in janus check
    return w + "\n" + h.get("legacy_probe", "")

BUILD_ERE = "(^|[ /])cargo(-[a-z]+)? (test|nextest|build|clippy|bench)( |$)|(^|[ /])pytest( |$)|(^|[ /])(npm|pnpm) (run )?test( |$)|xcodebuild.* test"

def pueue(h):
    c = h.get("pueue_config"); pre = "" if h.get("local") else f'PATH="$HOME/.local/bin:{h.get("bin", "")}:$PATH" '
    return pre + "pueue" + (f" -c {shlex.quote(c)}" if c else "")

LIST_KEYS = ("res", "cls", "sched", "direct", "waitjob", "apiproc", "apiinfo", "live")   # apiproc/apiinfo: API processes janus did not start, and whose they are   # probe lines that repeat: kept as lists (waitjob: jobs waiting for admission)

def probe(name, h, ro=True, cfg=None):
    """One ssh (or local sh) per host: memory, load, PSI, disk, waiting jobs, the capacity library's report (reservations, pause,
    history), other schedulers, direct builds, pueue status and tmux sessions. ro: the overload tracker is only read."""
    lib = cap_lib(cfg, name, h) if cfg else ""
    script = PROBE % {"bin": h.get("bin", ""), "root": qp(h.get("root", "/")), "pueue": pueue(h), "extra": probe_extra(h),
                                "rd": qp(h["_rd"]), "ro": "1" if ro else "", "cap": lib or "cap_report() { :; }"}
    rc, o, e = hrun(h, script, timeout=25)
    # Row 234: ssh's own failure (255) may be the shared master connection's (mux_client_request_session: send fds failed) while
    # the host answers a fresh connection: ask once more without the master before calling the host unreachable.
    if rc == 255 and not h.get("local"): rc, o, e = sh([*SSH, "-o", "ControlPath=none", h["ssh"], script], timeout=25)
    if rc: return {"name": name, "ok": False, "err": (e or o).strip().splitlines()[-1][:80] if (e or o).strip() else f"rc {rc}"}
    head, _, rest = o.partition("--pueue\n"); pj, _, tm = rest.partition("--tmux\n")
    p = {"name": name, "ok": True, "tmux": tm.split(), **{k: [] for k in LIST_KEYS}}
    for l in head.splitlines():
        k, _, v = l.partition("=")
        if k in LIST_KEYS: p[k].append(v)
        else: p[k] = v
    try: p["pueue"] = json.loads(pj) if pj.strip() else None
    except Exception: p["pueue"] = None
    return p

def discover(name, h):
    """What host h is, found rather than assumed (imp-sched item 1): parse_facts of the read-only HOST_FACTS script, plus the
    features and devices janus.toml declares for it ([hosts.X] features, devices: what cannot be probed). None: unreachable."""
    rc, o, e = hrun(h, HOST_FACTS % {"bin": h.get("bin", ""), "root": qp(h.get("root", "$HOME")), "agent_env": h.get("agent_env", "") or ":"}, timeout=30)
    if rc or "os=" not in o: return None
    f = parse_facts(o)
    f["features"] = sorted(set(f["features"]) | set(h.get("features", []))); f["devices"] = sorted(set(f["devices"]) | set(h.get("devices", [])))
    return {**f, "at": int(now())}

def host_facts(cfg, refresh=False):
    """{host: facts} from the cache (.janus/hosts-facts.json); a host never discovered, or older than [capacity] discover_h
    (default 24) when refresh is "stale", or every host when refresh is True, is discovered now (in parallel). A read-only run
    keeps what it found in memory only. An unreachable host keeps its last facts."""
    old = cfg.jget(FACTS); ttl = capv(cfg, {}, "discover_h") * 3600
    todo = [n for n in cfg.hosts if refresh is True or n not in old or (refresh == "stale" and now() - old[n].get("at", 0) > ttl)]
    if todo:
        with cf.ThreadPoolExecutor(8) as ex: new = dict(zip(todo, ex.map(lambda n: discover(n, cfg.hosts[n]), todo)))
        old.update({n: f for n, f in new.items() if f}); cfg.jput(FACTS, old)
    return {n: old[n] for n in cfg.hosts if n in old}

def probe_all(cfg):
    with cf.ThreadPoolExecutor(8) as ex:   # check refreshes the discovery (beside the probes) once it is older than discover_h
        fut = ex.submit(host_facts, cfg, "stale"); ps = list(ex.map(lambda kv: probe(*kv, ro=cfg.run.ro, cfg=cfg), cfg.hosts.items())); fut.result()
    d = {p["name"]: p for p in ps}
    un = cfg.jget("hosts.json")
    for n, p in d.items():
        if p["ok"]: un.pop(n, None)
        else: un.setdefault(n, now())
    cfg.jput("hosts.json", un)
    return d

def margins(h, p, cleanup=False, cfg=None):
    """Why the host is over its disk or memory-pressure margins. A cleanup unit is exempt from the disk floor (it is what frees
    it); memory pressure still counts. Load is judged by back-pressure (cap_paused: sustained, not one reading)."""
    why = []
    if p.get("cap_psi") and fl(p["cap_psi"]) > capv(cfg, h, "psi_pause"): why.append(f"memory pressure {p['cap_psi']}")
    elif not p.get("cap_psi") and p.get("psi") and fl(p["psi"]) >= 5: why.append(f"memory pressure {p['psi']}")
    if not cleanup and p.get("disk_gb") and fl(p["disk_gb"]) < capv(cfg, h, "min_disk_gb"): why.append(f"disk {p['disk_gb']}G")
    return why

def room(cfg, name, p, units, cleanup=False):
    """(number of agents that fit now, why not), by reservations: the host's memory for reservations (ram_gb - services_gb)
    minus its jobs' and agents' reservations, over the agent figure (learned: class agent in the host's history, else
    agent_gb); none while admission is paused (back-pressure), and the owner's caps."""
    h = cfg.hosts[name]
    if not p["ok"]: return 0, "unreachable"
    why = margins(h, p, cleanup, cfg); hc = host_cap(cfg, name, p, units)
    n = (hc["memcap"] - hc["rm"] - hc["agents_mb"]) // max(hc["amb"], 1)
    if n < 1: why.append(f"memory reserved: jobs {hc['rm']} MB + {hc['on']} agent(s) {hc['agents_mb']} MB of {hc['memcap']} MB (RAM - services)")
    if hc["pause"] and not cleanup: why.append(f"admission paused: {hc['pause']}")
    caps = cfg.caps
    if caps.get("host", {}).get(name) is not None:
        n = min(n, caps["host"][name] - sum(1 for u in units if u.get("host") == name))
    if caps.get("total") is not None: n = min(n, caps["total"] - len(units))
    return (0, "; ".join(why) or "caps") if why or n < 1 else (int(n), "")

def cap_line(cfg, name, p, units):
    if not p["ok"]: return f"{name} UNREACHABLE ({p['err']})"
    n, why = room(cfg, name, p, units); hc = host_cap(cfg, name, p, units)
    av = f"{fl(p.get('avail_mb')) / 1024:.1f}G" if p.get("avail_mb") else "unknown"
    return (f"{name} room={n} build: {hc['rj']} running ({hc['rc']}/{hc['cores']} cores, {hc['rm']}/{hc['memcap']} MB reserved), "
            f"{hc['queued']} queued, {len(hc['agents'])} agent(s) for {hc['slots']} slot(s), free {hc['free']} x {hc['jmb']} MB; "
            f"avail={av} load={p.get('load') or 'unknown'} psi={p.get('cap_psi') or p.get('psi') or '-'} disk={p.get('disk_gb')}G"
            + (f" PAUSED: {hc['pause']}" if hc["pause"] else "") + (f" FULL: {why}" if n < 1 else ""))

__all__ = ["probe_extra", "BUILD_ERE", "pueue", "LIST_KEYS", "probe", "discover", "host_facts", "probe_all", "margins", "room", "cap_line"]
