"""janus soak: exercise janus's read-only paths on a live project for `--minutes` and print one pass/fail line per step.

Dry by default: every step only reads what the orchestrator's own cycle reads, so it is safe on a running project. `--live`
additionally runs one watcher pass (the only step that writes). Steps: the watcher's wake decision, a check, the peers file,
the reports index, a brief header (with the session memory) in a dry run, a transcript read, each provider's ledger, and the
decision log. A step that raises, or a malformed ledger, prints FAIL and the command exits 1."""
import argparse, time
from janus.beads import Beads
from janus.check import run_check
from janus.context import Err, session_alive
from janus.findings.units import peer_rows
from janus.ledger import ledger_read
from janus.memory import memory_read
from janus.quality import qset
from janus.transcripts import read_transcript
from janus.units import all_units, unit_state

STEPS = ("watcher", "check", "peers", "index", "memory", "transcript", "ledger", "decisions")

def _local(cfg): return next((n for n, h in cfg.hosts.items() if h.get("local")), "local")

def step_watcher(cfg, live):
    """The watcher's wake decision, read here: its pane, the registered session, the open problems. --live runs one pass."""
    if live:
        from janus.watch import cmd_watch
        cmd_watch(cfg, argparse.Namespace(alert=False))
        return True, "ran one watcher pass"
    pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    return True, (f"pane {pane or 'none'}, registered session {'alive' if session_alive(cfg) else 'gone/unknown'}, "
                  f"{len(cfg.jget('open.json'))} open problem(s)")

def step_peers(cfg, hs, units):
    return True, f"{len(peer_rows(cfg, units, hs))} peer row(s)"

def step_index(cfg):
    from janus.actions.swarm import write_index
    return True, f"{write_index(cfg)} report(s) indexed"   # read-only run: write_index writes nothing

def step_memory(cfg):
    """The session memory as an agent sees it: render the brief header (no file is written)."""
    from janus.actions.launch import brief_header
    hn = _local(cfg); txt = brief_header(cfg, hn, cfg.hosts[hn], "soak", str(cfg.root), True,
                                         report=str(cfg.p("out", "out") / "soak/REPORT.md"))
    n = len(memory_read(cfg))
    return (bool(txt.strip()), "brief header empty" if not txt.strip() else f"brief header ok, {n} memory entry(ies)")

def step_transcript(cfg, units):
    u = next((x for x in units if x.get("name")), None) or next(iter(all_units(cfg).values()), None)
    if not u: return True, "no unit to read"
    rc, txt = read_transcript(cfg, u, "", 5)
    first = next((l for l in txt.splitlines() if l.strip()), "")[:80]
    return True, f"{u.get('name')}: exit {rc}" + (f", {first}" if first else "")   # a missing transcript is not a failure

def step_ledger(cfg):
    n = 0; bad = 0; who = []
    for name, pv in cfg.prov.items():
        if "ledger" not in pv: continue
        runs, b, _ = ledger_read(cfg, pv); n += len(runs); bad += len(b); who.append(name)
    return (bad == 0), (f"{n} run(s) in {', '.join(who)}" if who else "no ledger configured") + (f"; {bad} problem(s)" if bad else "")

def step_decisions(cfg):
    """The decision log: the beads decision records and the memory's Decisions entries, read as check reads them."""
    n = 0
    try: n += len(Beads(cfg).issues("janus-log"))
    except Err: pass
    return True, f"{n + sum(1 for e in memory_read(cfg) if e[0] == 'Decisions')} decision record(s)"

def cmd_soak(cfg, a):
    """Read-only by default; --live runs one watcher pass. Repeats every [quality] soak_every_s seconds until --minutes."""
    live = bool(getattr(a, "live", False))
    if not live: cfg.run.ro = True   # dry by default: nothing is written and no agent runs
    mins = float(getattr(a, "minutes", 1.0) or 0); every = float(qset(cfg, "soak_every_s"))
    L = []; ok_all = True; passes = 0; t0 = time.time()
    while True:
        passes += 1
        hs, units, F = {}, [], []
        try:
            F, _live, hs, units = run_check(cfg, cycle=False, writer=False)
            check = (True, f"{len(F)} finding(s), {len(units)} unit(s)")
        except Exception as e:
            check = (False, f"{type(e).__name__}: {e}")
        for u in units: u["state"] = unit_state(u, hs, {})
        results = [("watcher", lambda: step_watcher(cfg, live)), ("check", lambda: check),
                   ("peers", lambda: step_peers(cfg, hs, units)), ("index", lambda: step_index(cfg)),
                   ("memory", lambda: step_memory(cfg)), ("transcript", lambda: step_transcript(cfg, units)),
                   ("ledger", lambda: step_ledger(cfg)), ("decisions", lambda: step_decisions(cfg))]
        for name, fn in results:
            try: ok, detail = fn()
            except Exception as e: ok, detail = False, f"{type(e).__name__}: {e}"
            ok_all = ok_all and ok; L.append(f"soak {name}: {'PASS' if ok else 'FAIL'} - {detail}")
        left = mins * 60 - (time.time() - t0)
        if left <= 0: break
        time.sleep(min(every, left))
    L.append(f"soak: {passes} pass(es), {'live' if live else 'read-only'} ({mins:g} min)")
    return (0 if ok_all else 1), L

__all__ = ["cmd_soak", "STEPS"]
