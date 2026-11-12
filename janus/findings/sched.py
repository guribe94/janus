"""Findings about scheduled jobs (imp-sched item 3): overrun:<host>:<job>, a running job far over its expected run time."""
from janus.cap import capv, class_secs
from janus.context import fl, now

def overrun_findings(cfg, hs):
    """A job (a reservation in the host's probe) running longer than [capacity] overrun_x times its class's expected run time
    (the median of its recorded runs on that host), and at least 10 min over it. Jobs of a class without history are not judged."""
    F = []
    for n, p in hs.items():
        if not p.get("ok"): continue
        x = capv(cfg, cfg.hosts[n], "overrun_x")
        for r in p.get("res", []):
            f = r.split()   # pid cores MB start class file
            if len(f) < 6 or not f[3].isdigit() or f[4].startswith("agent"): continue
            e = class_secs(p.get("hist", []), f[4]); ran = now() - int(f[3])
            if e and ran > max(x * e, e + 600):
                job = f[5].removesuffix(".res")
                F.append((f"overrun:{n}:{job}", f"{n} job {job} ({f[4]}) has run {int(ran // 60)} min, {fl(ran / e):.0f}x its expected "
                          f"{max(1, e // 60)} min (median of its class there): look at its log; janus cancel it if it is stuck"))
    return F
