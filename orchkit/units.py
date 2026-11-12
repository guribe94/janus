"""A unit's derived state and report (section 7, Step 2).

unit_state (from the live source it runs in), verdict (a resumed report only counts a verdict written after the resume
started; PENDING is not a verdict), report_path, herdr_agents, ws_id (the workspace agents run in)."""
from orchkit.context import herdr
import argparse, calendar, concurrent.futures as cf, hashlib, json, os, re, shlex, signal, subprocess, sys, tempfile, time, tomllib
from pathlib import Path

def unit_state(u, ps, agents):
    """working | idle | blocked | queued | done | failed | gone, from the live source the unit runs in."""
    via = u.get("via")
    if via == "herdr":
        a = agents.get(u["name"])
        return a.get("agent_status", "unknown") if a else "gone"
    p = ps.get(u.get("host"), {})
    if not p.get("ok"): return "unknown"
    if via == "tmux": return "working" if f"orch-{u['name']}" in p.get("tmux", []) else "gone"
    if via == "pueue":
        t = ((p.get("pueue") or {}).get("tasks") or {}).get(str(u.get("task")))
        if not t: return "gone"
        s = t.get("status"); k = s if isinstance(s, str) else next(iter(s), "")
        if k == "Done": return "done" if s["Done"].get("result") == "Success" or s["Done"] == "Success" else "failed"
        return {"Queued": "queued", "Running": "working"}.get(k, k.lower())
    return "unknown"

def verdict(cfg, u):
    """The unit's final VERDICT line, from the report it writes (a resumed unit: its original report, report_path). A resumed
    report still holds the earlier run's verdict, so only a verdict written after the resume started counts, and a line 1
    that is still PENDING means not finished."""
    p = report_path(cfg, u)
    try: t = p.read_text().splitlines()
    except OSError: return None
    if u.get("resume") and (p.stat().st_mtime < u.get("started", 0) or (t and "PENDING" in t[0])): return None
    return next((l for l in reversed(t) if l.startswith("VERDICT:") and "PENDING" not in l), None)   # PENDING is not a verdict

def herdr_agents(cfg):
    r = herdr("agent", "list")
    return {a.get("name") or a["pane_id"]: a for a in (r or {}).get("agents", [])} if r else {}

def ws_id(cfg, create=False):
    lab = cfg.c.get("herdr", {}).get("workspace", cfg.name)
    r = herdr("workspace", "list") or {}
    for w in r.get("workspaces", []):
        if w.get("label") == lab: return w["workspace_id"]
    if create and not cfg.run.dry:
        r = herdr("workspace", "create", "--label", lab, "--no-focus") or {}
        return r.get("workspace", {}).get("workspace_id")

def report_path(cfg, u): return Path(u["report"]) if u.get("report") else cfg.p("out", "out") / u["name"] / "REPORT.md"

__all__ = ["unit_state", "verdict", "herdr_agents", "ws_id", "report_path"]
