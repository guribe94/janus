"""Findings about work in flight: ready backlog that would fit, the project's own git state, the watcher's health."""
from janus.actions.backlog import brief_dir, fill_candidates, fill_plan
from janus.backlog import backlog_cfg, backlog_issues, item_progress, norm_item
from janus.units import all_units
from janus.context import Err, sh
from janus.ledger import route_unusable
from janus.memory import long_texts
from janus.platform import watcher_loaded, watcher_service

def git_findings(cfg):
    """git:dirty, git:unpushed, git:noupstream of the project's own repository (janus's own state under .janus is not counted)."""
    F = []
    rc, o, e = sh(["git", "status", "--porcelain", "-uall"], cwd=cfg.root)
    if rc == 0:
        dirty = [l for l in o.splitlines() if not l[3:].strip().strip('"').startswith(".janus/")]
        if dirty: F.append(("git:dirty", f"{len(dirty)} uncommitted path(s) in {cfg.root.name}"))
        rc, o, e = sh(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=cfg.root)
        if rc == 0 and o.strip() != "0": F.append(("git:unpushed", f"{o.strip()} unpushed commit(s)"))
        elif rc: F.append(("git:noupstream", "git has no upstream"))
    return F

def backlog_findings(cfg, hs, units):
    """backlog:import (the checklist is not in beads), idle-with-backlog (ready briefed units that fit now), idle-with-backlog:nobrief
    (ready items without a usable brief, by id: the orchestrator writes them), backlog:held (briefed units that do not fit now) and
    backlog:blocked (ready items bd ready does not list: a beads dependency). The selection is fill's own (fill_candidates)."""
    b = backlog_cfg(cfg)
    if not b.get("file"): return []
    try: iss = backlog_issues(cfg)
    except Err: return []
    if not iss: return [("backlog:import", f"the checklist {b['file']} is not in beads: janus backlog import")]
    F = []; missing, blocked, held = [], [], []
    try: cands = fill_candidates(cfg, units, missing=missing, blocked=blocked, held=held)
    except Err: return F
    # an item with no brief waiting, no open unit and at least one unit closed DONE: finished work the item still waits to be closed on
    prog = item_progress(cfg, list(all_units(cfg).values()), iss)
    done = [i for i in missing if (p := prog.get(norm_item(i))) and p["done"] and not p["open"]]
    for i in done:
        p = prog[norm_item(i)]
        F.append((f"backlog:done:{i}", f"all {p['closed']} closed unit(s) of {i} ended and {len(p['done'])} landed DONE (newest {p['done'][0]}); no unit is open and no brief "
                  f"waits for it: close it (janus backlog set {i} status=DONE --unit {p['done'][0]}) or brief what is left"))
    missing = [i for i in missing if i not in done]
    cfg.run.briefless = missing; cfg.run.held = held   # held: named by status only, never a dispatch finding
    if missing: F.append(("idle-with-backlog:nobrief", f"{len(missing)} ready backlog item(s) have no usable brief: {', '.join(missing[:12])}{'...' if len(missing) > 12 else ''}; "
                          f"write {brief_dir(cfg)}/<unit>.md with an ITEM: <id> line (janus fill launches it), or janus backlog set <id> hold=<reason>"))
    if blocked: F.append(("backlog:blocked", f"{len(blocked)} ready backlog item(s) wait on a beads dependency (bd ready does not list them): {', '.join(blocked[:12])}"))
    if not cands: return F
    pinned = []; plan, stops = fill_plan(cfg, hs, units, cands=cands, pinned=pinned)
    for name, item, h in pinned:   # its brief or [placement] names a host that is down: not moved for it (a pin is usually hardware)
        F.append((f"backlog:pinned:{name}", f"{name} [{item}] is pinned to {h}, which is down: it waits for {h}; to run it elsewhere change its brief's TEST-HOST or [placement], or bring {h} back"))
    if plan:
        per = {}
        for x in plan: per[x[3]] = per.get(x[3], 0) + 1
        F.append(("idle-with-backlog", f"{len(plan)} ready backlog unit(s) with briefs fit now ({', '.join(f'{k}: {v}' for k, v in per.items())}): "
                  + ", ".join(x[0] for x in plan[:6]) + ("..." if len(plan) > 6 else "") + "; janus fill"))
    held = [x[4] for x in cands if x[4] not in {p[0] for p in plan} | {p[0] for p in pinned}]
    if held: F.append(("backlog:held", f"{len(held)} ready backlog unit(s) with briefs do not launch now ({', '.join(held[:6])}): " + "; ".join(stops)[:300]))
    return F

ORCH_WAIT = 3   # watcher passes an unregistered orchestrator is tolerated

def orchestrator_findings(cfg):
    """handoff:long and memory:long (memory.long_texts); orchestrator:kind: the registered orchestrator's kind differs from janus.toml [orchestrator] kind; orchestrator:unregistered:
    the watcher has not seen the orchestrator it started (or the recorded pane's agent) for ORCH_WAIT passes and starts none."""
    F = []
    w = cfg.jget("orch_wait.json")
    if w.get("passes", 0) >= ORCH_WAIT:
        F.append(("orchestrator:unregistered", f"no orchestrator seen for {w['passes']} watcher passes ({w.get('why')}); the watcher starts no other while it "
                  "may be starting: look at its pane, then run janus register in it, or close the pane (herdr pane close) so the watcher starts a new one"))
    for k, m, why in route_unusable(cfg):
        F.append((f"routing:{k}", f"[routing] {k} starts with {m}, which cannot launch ({why}), and no fallback is allowed: fill and launch without "
                  f"--model or MODEL: launch nothing for {k}; put an enabled model first in [routing] {k}, or set fallback = true"))
    F += long_texts(cfg)   # handoff:long, memory:long: what every session start loads whole
    sk = cfg.f("session_kind").read_text().strip() if cfg.f("session_kind").exists() else ""
    if sk and sk != cfg.c.get("orchestrator", {}).get("kind", "claude"):
        F.append(("orchestrator:kind", f"the registered orchestrator is {sk}, but janus.toml [orchestrator] kind is {cfg.c.get('orchestrator', {}).get('kind')}: set it to {sk}"))
    return F

def session_findings(cfg):
    """watcher (not loaded) and the watcher's failing steps."""
    F = []
    if not watcher_loaded(): F.append(("watcher", f"watcher {watcher_service()[1]} ({watcher_service()[0]}) is not running: run install"))
    return F + watch_step_findings(cfg)

STEP_FAILS = 3   # passes in a row before a failing watcher step is a finding (one failure is retried by the next pass)

def watch_step_findings(cfg):
    """watcher:step:<name>: a step of the watcher's pass failed STEP_FAILS passes in a row (watch.watch_step records it and
    carries on with the other steps); it clears when the step works again."""
    return [(f"watcher:step:{n}", f"the watcher's step {n} failed {x['n']} passes in a row ({x.get('err', '?')}); its other steps still run and it is tried "
             f"again every pass. What that step does is not happening: find the cause in {cfg.f('watch.log')}")
            for n, x in sorted(cfg.jget("watch_steps.json").items()) if x.get("n", 0) >= STEP_FAILS]
