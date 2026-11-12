"""A unit's derived state, its report and the unit queries every layer uses (section 7, Step 2).

unit_state (from the live source it runs in), verdict (a resumed report only counts a verdict written after the resume
started; PENDING is not a verdict), report_path, herdr_agents, ws_id (the workspace agents run in); get_unit/unit_any/
all_units (open units, the latest record of any unit), unit_runs/unit_spent, api_out, the WAITING line of its report
(waiting_line_jobs), agent_ended, and whether its tree is its own (own_tree, main_checkout)."""
from janus.beads import Beads, unit_record
from janus.context import Err, sh
from janus.ledger import ledger_read
from janus.platform import herdr
import json, re
from pathlib import Path

def unit_state(u, ps, agents):
    """working | idle | blocked | queued | done | failed | gone, from the live source the unit runs in."""
    via = u.get("via")
    if via == "herdr":
        a = agents.get(u["name"])
        return a.get("agent_status", "unknown") if a else "gone"
    p = ps.get(u.get("host"), {})
    if not p.get("ok"): return "unknown"
    if via == "tmux": return "working" if f"janus-{u['name']}" in p.get("tmux", []) else "gone"
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

def main_checkout(cwd):
    """True when cwd is the main working tree of a repository that has linked worktrees (agents get a worktree, H9)."""
    rc, o, e = sh(["git", "-C", str(cwd), "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"])
    if rc: return False
    top, gd, cd = o.split("\n")[:3]
    if Path(top).resolve() != Path(cwd).resolve() or Path(cwd, gd).resolve() != Path(cwd, cd).resolve(): return False
    rc, o, e = sh(["git", "-C", str(cwd), "worktree", "list", "--porcelain"])
    return rc == 0 and o.count("\nworktree ") + o.startswith("worktree ") > 1

def get_unit(cfg, name):
    bd = Beads(cfg); u = next((u for u in bd.units() if u["name"] == name), None)
    if not u: raise Err(f"no open unit {name}")
    return bd, u

def own_tree(cfg, u, units):
    """The unit has a worktree of its own: not the project root, not shared with another open unit."""
    c = Path(u["cwd"]).resolve()
    return c != cfg.root.resolve() and not any(Path(x.get("cwd", "/")).resolve() == c for x in units if x["id"] != u["id"])

ENDED = ("done", "failed", "gone", "idle")

WAIT_RX = re.compile(r"^[\s*>-]*WAITING\b\**:?\s*(.*)$", re.M)

JOBREF = re.compile(r"(?<![\w/.-])([A-Za-z0-9][\w.-]*):(\d+)\b")

def report_text(cfg, u):
    try: return report_path(cfg, u).read_text()
    except OSError: return ""

def waiting_line_jobs(cfg, u):
    """The jobs (<host>:<task>) named on the latest WAITING line of the unit's report that it has not been resumed for yet.
    A resumed unit's report counts only once written after the unit started."""
    try:
        if u.get("resume") and report_path(cfg, u).stat().st_mtime < u.get("started", 0): return []
    except OSError: return []
    m = WAIT_RX.findall(report_text(cfg, u))
    if not m: return []
    done = set(u.get("handled_jobs", []))
    return [j for j in dict.fromkeys(f"{h}:{t}" for h, t in JOBREF.findall(m[-1]) if h in cfg.hosts) if j not in done]

def unit_runs(u): return list(dict.fromkeys([*u.get("runs", []), *([u["run"]] if u.get("run") else [])]))

def unit_spent(cfg, u, wrapup=False):
    """Dollars the unit's runs have used (ledger: each run's done cost, its budget while it runs), over all its resumes; the
    wrap-up run is outside the unit's budget and counted only with wrapup=True."""
    pv = cfg.prov.get(cfg.models.get(u.get("model"), {}).get("provider"), {})
    if "ledger" not in pv: return 0.0
    runs = ledger_read(cfg, pv)[0]
    return sum((r["cost"] or 0.0) if r["done"] else (r["budget"] or 0.0) for k in unit_runs(u)
               if (r := runs.get(k)) and (wrapup or k != u.get("wrapup")))

def api_out(cfg, u):
    try: return json.loads((cfg.p("out", "out") / u["name"] / "api.json").read_text())
    except Exception: return {}

def all_units(cfg):
    """Every unit ever recorded (open and closed): {name: meta}; the latest record of a name wins."""
    if cfg.run.all_units is None:
        try:
            r = Beads(cfg).run("list", "--label", "unit", "--all", "-n", "0")
            r = r.get("issues", r) if isinstance(r, dict) else r
        except Err: r = []
        out = {}
        for i in sorted(r, key=lambda i: i.get("created_at", "")):
            t = i.get("title", "")
            if not t.startswith("unit:"): continue
            out[t[5:]] = {**unit_record(i), "id": i["id"], "name": t[5:], "status": i.get("status")}
        cfg.run.all_units = out
    return cfg.run.all_units

def agent_ended(u):
    """The unit's agent is gone: an API run ended, a tmux session or herdr agent is gone (an idle herdr agent is alive)."""
    return u.get("state") == "gone" if u.get("via") == "herdr" else u.get("state") in ENDED

def unit_any(cfg, name):
    """An open unit, else the latest record of a closed one."""
    try: u = next((u for u in Beads(cfg).units() if u["name"] == name), None)
    except Err: u = None
    return u or all_units(cfg).get(name)

__all__ = ["unit_state", "verdict", "herdr_agents", "ws_id", "report_path", "main_checkout", "get_unit", "own_tree", "ENDED", "WAIT_RX", "JOBREF", "report_text", "waiting_line_jobs", "unit_runs", "unit_spent", "api_out", "all_units", "agent_ended", "unit_any"]
