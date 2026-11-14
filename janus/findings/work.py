"""Findings about work in flight: ready backlog that would fit, the project's own git state, the watcher's health."""
from janus.actions.backlog import fill_candidates, fill_plan
from janus.backlog import backlog_cfg, backlog_issues, item_actors, item_holds, unit_items
from janus.context import Err, sh
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
    """backlog:import (the checklist is not in beads), idle-with-backlog (ready briefed units that fit now), backlog:held."""
    b = backlog_cfg(cfg)
    if not b.get("file"): return []
    try: iss = backlog_issues(cfg)
    except Err: return []
    if not iss: return [("backlog:import", f"the checklist {b['file']} is not in beads: janus backlog import")]
    F = []
    cov = unit_items(cfg, units); act = item_actors(cfg); hold = item_holds(cfg)
    todo = [r for r, i in iss.items() if i.get("status") in ("open", "in_progress") and "backlog-ready" in (i.get("labels") or [])
            and r[8:] not in cov and act.get(r[8:], "agent") == "agent" and r[8:] not in hold]
    if not todo: return F
    plan, stops = fill_plan(cfg, hs, units)
    if plan:
        per = {}
        for x in plan: per[x[3]] = per.get(x[3], 0) + 1
        F.append(("idle-with-backlog", f"{len(plan)} ready backlog unit(s) with briefs fit now ({', '.join(f'{k}: {v}' for k, v in per.items())}): "
                  + ", ".join(x[0] for x in plan[:6]) + ("..." if len(plan) > 6 else "") + "; janus fill"))
    held = [x[4] for x in fill_candidates(cfg, units) if x[4] not in {p[0] for p in plan}]
    if held: F.append(("backlog:held", f"{len(held)} ready backlog unit(s) with briefs do not launch now ({', '.join(held[:6])}): " + "; ".join(stops)[:300]))
    return F

ORCH_WAIT = 3   # watcher passes an unregistered orchestrator is tolerated

def orchestrator_findings(cfg):
    """orchestrator:kind: the registered orchestrator's kind differs from janus.toml [orchestrator] kind; orchestrator:unregistered:
    the watcher has not seen the orchestrator it started (or the recorded pane's agent) for ORCH_WAIT passes and starts none."""
    F = []
    w = cfg.jget("orch_wait.json")
    if w.get("passes", 0) >= ORCH_WAIT:
        F.append(("orchestrator:unregistered", f"no orchestrator seen for {w['passes']} watcher passes ({w.get('why')}); the watcher starts no other while it "
                  "may be starting: look at its pane, then run janus register in it, or close the pane (herdr pane close) so the watcher starts a new one"))
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
