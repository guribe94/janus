"""Reservations, back-pressure and learned history: the one capacity rule per host (section 7, Step 2).

CAP_DEFAULTS/capv/cap_nums, cap_lib (renders templates.CAP_LIB for runs/cap.sh), class_mb/class_secs, record_history,
host_cap/next_free/build_room. build_room's refusal names when room is expected (section 9: keep that)."""
from janus.context import fl, ts, now, sh
from janus.templates import CAP_LIB
import re, shlex
from pathlib import Path

# "Launch as much as we can, but no more." Admission is by reservations, never by a momentary reading: each running job holds
# cores_per_job cores and the memory its job class needed before (the peak resident memory of past runs of that class on that
# host, plus margin_pct), each agent with a reservation file holds the agent class's figure; a job is admitted while the sum
# fits cores and ram_gb - services_gb. Back-pressure pauses admission (memory PSI avg10 over psi_pause, or load over
# load_pause x cores for hold_s); a running job is never killed for it. One shell library per host (runs/cap.sh) holds the
# rule: every waiting job sources it each poll, and every probe carries it inline, so janus check, launch and fill read the
# same numbers the admission uses. Settings: per host, else [capacity], else the default below.

CAP_DEFAULTS = {"cores": 4, "cores_per_job": 1, "ram_gb": 4, "services_gb": 0, "job_gb": 2, "agent_gb": 1.0, "psi_pause": 10,
                "load_pause": 1.25, "hold_s": 60, "history_n": 5, "margin_pct": 15, "min_disk_gb": 10, "discover_h": 24,
                "age_weight": 1, "expect_s": 600, "broad_min": 30, "overrun_x": 3}
# imp-sched item 3 (CPU): a job reserves, and is capped at, its class's learned cores (CPU seconds / run time of its last runs, 1 to
# cores_per_job); waiting jobs go shortest expected run time first, minus age_weight x seconds waited (aging); expect_s is the
# expected run time of a class without history; broad_min: a class whose median run is longer needs --broad; overrun_x: check
# flags a job running longer than that many times its expected run time.

FACTS = "hosts-facts.json"   # the discovered hosts (probe.host_facts); cores and ram_gb default to what was found there
_facts_cache = {}

def facts(cfg):
    """{host: facts} as last discovered (read once per change of the cache file)."""
    p = cfg.f(FACTS)
    try: k = (str(p), p.stat().st_mtime)
    except OSError: return {}
    if k not in _facts_cache: _facts_cache.clear(); _facts_cache[k] = cfg.jget(FACTS)
    return _facts_cache[k]

def capv(cfg, h, k):
    """A capacity setting of host h: janus.toml [hosts.X], else (cores, ram_gb) what discovery found on it, else [capacity],
    else the default. No core or memory count is assumed once a host was discovered."""
    if k in h: return h[k]
    if cfg and k in ("cores", "ram_gb"):
        f = next((facts(cfg).get(n) for n, x in cfg.hosts.items() if x is h), None) or {}
        v = f.get("cores") if k == "cores" else f.get("mem_mb") and round(f["mem_mb"] / 1024, 1)
        if v: return v
    return (cfg.capc if cfg else {}).get(k, CAP_DEFAULTS[k])

def cap_nums(cfg, h):
    """The host's capacity in integers: cores, cores per job, memory for reservations (MB), job default/cap (MB), agent (MB)."""
    return {"cores": int(capv(cfg, h, "cores")), "cpj": max(1, int(-(-capv(cfg, h, "cores_per_job") // 1))),
            "memcap": int((capv(cfg, h, "ram_gb") - capv(cfg, h, "services_gb")) * 1024), "capmb": int(capv(cfg, h, "job_gb") * 1024),
            "agmb": int(capv(cfg, h, "agent_gb") * 1024), "hn": int(capv(cfg, h, "history_n")), "margin": int(capv(cfg, h, "margin_pct"))}

def cap_lib(cfg, hn, h):
    """runs/cap.sh for host h: the rule above with the host's numbers, its [capacity.classes], and how its jobs are seen
    (systemd scopes janus-<jid>.scope; podman containers by container.name_prefixes; the Mac: process trees)."""
    n = cap_nums(cfg, h); pod = h.get("runner") == "podman"
    cpus = '  v=$(awk \'/^usage_usec/{print int($2/1000000)}\' "%s/cpu.stat" 2>/dev/null)'
    if pod:
        pre = h.get("container", {}).get("name_prefixes", ["janus-run-"])
        jobids = "podman ps --format '{{.Names}}' | awk '" + " ".join(
            f"/^{re.escape(x)}/ {{" + ("sub(/^janus-run-/, \"\"); " if x == "janus-run-" else "") + "print; next}" for x in pre) + "'"
        sample = ('  i=$(podman ps -q --no-trunc --filter name=janus-run-$2 2>/dev/null | head -1)\n'
                  '  [ -n "$i" ] && v=$(awk \'/^anon /{print int($2/1048576)}\' "$US/libpod-$i.scope/memory.stat" 2>/dev/null)')
        cpusample = '  i=$(podman ps -q --no-trunc --filter name=janus-run-$1 2>/dev/null | head -1)\n  [ -n "$i" ] &&' + cpus[1:] % "$US/libpod-$i.scope"
    elif h.get("local"):
        jobids, sample, cpusample = "true", '  v=$(tree_mb "$1")', "  :"
    else:
        jobids = "ls -d $AS/janus-*.scope | sed 's#.*/janus-##; s#[.]scope$##'"
        sample = ('  v=$(awk \'/^anon /{print int($2/1048576)}\' "$AS/janus-$2.scope/memory.stat" 2>/dev/null)\n'
                  '  [ -n "$v" ] || v=$(tree_mb "$1")')
        cpusample = cpus % "$AS/janus-$1.scope"
    cls = "".join(f"  printf '%s\\n' \"$c\" | grep -Eq {shlex.quote(rx)} && {{ echo {shlex.quote(k)}; return; }}\n"
                  for k, rx in (cfg.capc.get("classes", {}) if cfg else {}).items())
    return CAP_LIB % {**n, "name": hn, "psip": capv(cfg, h, "psi_pause"), "loadx": capv(cfg, h, "load_pause"), "hold": int(capv(cfg, h, "hold_s")),
                      "disk": int(capv(cfg, h, "min_disk_gb")), "jobids": jobids, "sample": sample, "classes": cls,
                      "cpusample": cpusample, "agew": capv(cfg, h, "age_weight"), "exps": int(capv(cfg, h, "expect_s"))}

def class_mb(hist, cls, d, cap, hn=5, margin=15):
    """The reservation of a job class from history lines (time, class, peak MB, secs, rc): the same rule as cap_mb in runs/cap.sh."""
    ag = cls == "agent"
    pk = sorted([int(f[2]) for f in (l.split("\t") for l in hist) if len(f) >= 3 and f[1] == cls and f[2].isdigit() and int(f[2]) > 0][-(hn * 3 if ag else hn):])
    if not pk: return d
    v = max(128 if ag else 256, int((pk[(len(pk) - 1) // 2] if ag else pk[-1]) * (100 + margin) / 100))
    return min(v, cap) if cap > 0 else v

def class_secs(hist, cls, shape=None):
    """Median run time (s) of a job class from history lines, None without history. shape (a command signature, column 8 of a
    history line) limits the runs to COMPARABLE jobs (item 32): one class lumps a 1-minute `cargo test` together with a
    20-minute one, so the class median flagged the long job as stuck; with a shape, only jobs of the same command count."""
    rows = [f for f in (l.split("\t") for l in hist)
            if len(f) >= 4 and f[1] == cls and f[3].isdigit() and (shape is None or (len(f) >= 8 and f[7] == shape))]
    s = sorted(int(f[3]) for f in rows)
    return s[len(s) // 2] if s else None

def class_cores(hist, cls, cpj, hn=5):
    """The cores a job class reserves (and is capped at): the same rule as cap_cores in runs/cap.sh."""
    r = [int(f[5]) / int(f[3]) for f in (l.split("\t") for l in hist) if len(f) >= 6 and f[1] == cls and f[3].isdigit() and int(f[3]) > 0 and f[5].isdigit() and int(f[5]) > 0][-hn:]
    return max(1, min(cpj, int(max(r) + 0.5))) if r else cpj

def class_usage(hist, cpj, hn=5):
    """Per job class, from history lines: runs, expected (median) run time, learned cores, median CPU seconds, queue wait median
    and 95th percentile (the columns cap_record writes: time, class, peak MB, secs, rc, CPU s, wait s)."""
    by = {}
    for f in (l.split("\t") for l in hist):
        if len(f) >= 5 and f[1] != "agent" and f[3].isdigit(): by.setdefault(f[1], []).append(f)
    med = lambda v, q=0.5: sorted(v)[min(len(v) - 1, int(len(v) * q))] if v else None
    out = []
    for c, fs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        cpu = [int(f[5]) for f in fs if len(f) >= 7 and f[5].isdigit()]; wait = [int(f[6]) for f in fs if len(f) >= 7 and f[6].isdigit()]
        out.append({"class": c, "runs": len(fs), "expect_s": class_secs(hist, c), "cores": class_cores(hist, c, cpj, hn),
                    "cpu_s": med(cpu), "wait_p50": med(wait), "wait_p95": med(wait, 0.95)})
    return out

def tree_rss_mb(pid):
    """Resident memory (MB) of process pid and its descendants (one ps call)."""
    rc, o, e = sh(["ps", "-A", "-o", "pid=,ppid=,rss="], timeout=10)
    pp, rs = {}, {}
    for l in o.splitlines():
        f = l.split()
        if len(f) == 3 and f[0].isdigit(): pp[int(f[0])] = int(f[1]); rs[int(f[0])] = int(f[2])
    q, ch = {pid}, True
    while ch:
        ch = False
        for x, y in pp.items():
            if y in q and x not in q: q.add(x); ch = True
    return sum(rs.get(x, 0) for x in q) // 1024

def record_history(rd, cls, peak, secs, rc):
    """One line of <runs>/history.tsv (the format cap_record writes)."""
    with open(Path(rd) / "history.tsv", "a") as f: f.write(f"{int(now())}\t{cls}\t{int(peak)}\t{int(secs)}\t{rc}\n")

def host_cap(cfg, n, p, units):
    """The host's capacity now, from its probe (the numbers the admission sees) and the open units:
    slots: build jobs that fit when nothing runs; free: further jobs that fit now; agents: open units whose builds run here."""
    h = cfg.hosts[n]; c = cap_nums(cfg, h); hist = p.get("hist", [])
    # a typical job here: the largest reservation of the classes run lately (job_gb when nothing is recorded yet)
    recent = {l.split("\t")[1] for l in hist[-50:] if l.count("\t") >= 2}
    jmb = max([class_mb(hist, k, c["capmb"], c["capmb"], c["hn"], c["margin"]) for k in recent] or [c["capmb"]])
    amb = class_mb(hist, "agent", c["agmb"], 0, c["hn"], c["margin"])
    rj, rc, rm, ra, ram = (int(fl(p.get(k))) for k in ("cap_rj", "cap_rc", "cap_rm", "cap_ra", "cap_ram"))
    t = list(((p.get("pueue") or {}).get("tasks") or {}).values())
    qd = sum(1 for x in t if x.get("group") in ("jobs", "gates") and "Queued" in str(x.get("status")))
    qd += int(fl(p.get("wait_jobs"))) + int(fl(p.get("wait_gates")))
    here = [u for u in units if (u.get("build") or u.get("host")) == n and u.get("state") not in ("done", "failed", "gone", "idle")]
    on = [u for u in units if u.get("host") == n and u.get("state") not in ("done", "failed", "gone", "idle", "parked")]
    agents_mb = max(ram, len(on) * amb)   # every open agent on the host holds the agent figure (reservation files of api agents at least)
    slots = max(1, min(c["cores"] // c["cpj"], (c["memcap"] - agents_mb) // max(jmb, 1)))
    pause = p.get("cap_pause", "")
    free = 0 if pause else max(0, min((c["cores"] - rc) // c["cpj"], (c["memcap"] - rm - agents_mb) // max(jmb, 1)))
    if rj == 0 and not pause: free = max(free, 1)
    return {**c, "jmb": jmb, "amb": amb, "rj": rj, "rc": rc, "rm": rm, "ra": ra, "agents_mb": agents_mb, "queued": qd, "slots": slots,
            "free": free, "pause": pause, "agents": [u["name"] for u in here], "on": len(on), "hist": hist, "res": p.get("res", [])}

def next_free(hc):
    """When a build reservation is expected to free: the earliest (start + median run time of its class) of the running jobs."""
    ends = []
    for r in hc["res"]:
        f = r.split()
        if len(f) >= 5 and f[3].isdigit():
            s = class_secs(hc["hist"], f[4])
            if s: ends.append(int(f[3]) + s)
    return min(ends) if ends else None

def build_room(cfg, n, hc, kind="implement"):
    """(ok, why) for one more agent whose builds and tests run on host n (janus-fixes8 item 3): it needs a free build reservation
    there (agents for the host < its build slots), or a bounded queue position: at most one queued job per running job, and
    no more agents than twice the slots. A paused host takes none. Refusals say when to expect room."""
    if kind in cfg.capc.get("no_build_kinds", []): return True, "no builds"
    a = len(hc["agents"]); s = hc["slots"]
    if hc["pause"]: return False, f"{n} admission paused ({hc['pause']}); retry when it clears (running jobs are not killed)"
    if a < s: return True, f"build reservation {a + 1} of {s} on {n}"
    used = max(hc["queued"], a - s)   # queue positions in use: jobs queued now, or agents past the slots (each may queue one)
    if a < 2 * s and used < hc["rj"]: return True, f"queue position on {n} ({used} of {hc['rj']} in use: one queued job per running job)"
    nf = next_free(hc)
    when = (f"a build slot should free about {ts(nf)}" if nf and nf > now() else "a build slot frees when a running job ends") + \
           (f", or when one of {', '.join(hc['agents'][:4])} closes" if hc["agents"] else "")
    return False, (f"{n} build capacity full: {a} agent(s) for {s} build slot(s) + {s} queue position(s); {hc['rj']} running, "
                   f"{hc['queued']} queued; {when}")

__all__ = ["CAP_DEFAULTS", "FACTS", "facts", "capv", "cap_nums", "cap_lib", "class_mb", "class_secs", "class_cores", "class_usage", "tree_rss_mb", "record_history", "host_cap", "next_free", "build_room"]
