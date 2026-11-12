"""A unit's derived state, its report and the unit queries every layer uses (section 7, Step 2).

unit_state (from the live source it runs in), verdict (report.json's verdict first, else the prose; a resumed report only counts
a verdict written after the resume started; PENDING is not a verdict), report_path/report_json/report_summary, herdr_agents,
ws_id (the workspace agents run in); get_unit/unit_any/all_units (open units, the latest record of any unit), unit_runs/unit_spent,
api_out, the WAITING line of its report (waiting_line_jobs), agent_ended, and whether its tree is its own (own_tree,
main_checkout)."""
from janus.beads import Beads, unit_record
from janus.context import Err, sh
from janus.ledger import budget_of, ledger_read
from janus.platform import herdr
from janus.project import unit_tmux
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
    if via == "tmux": return "working" if unit_tmux(u) in p.get("tmux", []) else "gone"
    if via == "pueue":
        t = ((p.get("pueue") or {}).get("tasks") or {}).get(str(u.get("task")))
        if not t: return "gone"
        s = t.get("status"); k = s if isinstance(s, str) else next(iter(s), "")
        if k == "Done": return "done" if s["Done"].get("result") == "Success" or s["Done"] == "Success" else "failed"
        return {"Queued": "queued", "Running": "working"}.get(k, k.lower())
    return "unknown"

def verdict(cfg, u):
    """The unit's final VERDICT line: report.json's verdict when it states one (preferred over the prose), else the last
    'VERDICT:' line of its report (a resumed unit: its original report, report_path). A resumed report still holds the earlier
    run's verdict, so only a verdict written after the resume started counts, and a line 1 that is still PENDING means not
    finished. A report whose LAST VERDICT line is PENDING or WAITING is not finished (item 11, finding 2): whatever earlier
    DONE lines or a stale report.json say, it is parked, not done."""
    p = report_path(cfg, u); j = report_json(cfg, u)
    try: text = p.read_text()
    except OSError: text = ""
    t = text.splitlines()
    if u.get("resume"):
        try: mt = p.stat().st_mtime
        except OSError: mt = 0
        jp = p.with_name("report.json")
        if jp.exists(): mt = max(mt, jp.stat().st_mtime)
        if mt < u.get("started", 0) or (t and "PENDING" in t[0]): return None
    # finding 1 (high): the LAST of the report's VERDICT: lines and its park lines decides. A park line after the last VERDICT
    # line means parked, never finished, whatever earlier DONE lines (or a stale report.json) say. Finding 5: a park line counts
    # only when it names a job janus knows (a host in cfg.hosts) -- a prose line, or a park line whose ids janus cannot read
    # (`mac:9` for an unknown host, a bare `9`), is not a park, so the closing VERDICT line still stands.
    # Finding 1 (fix-install6): ONE reader for both readers -- the park jobs come from park_jobs, which reads the ids on the park
    # line or on the line(s) after it (WAIT_RX's `\s*` spans the line break), so a unit parked with its ids on the next line is
    # judged parked here exactly as waiting_line_jobs yields them.
    vi = next((i for i in range(len(t) - 1, -1, -1) if t[i].startswith("VERDICT:")), None)
    pi, _ = park_jobs(cfg, text)
    if pi is not None and (vi is None or pi > vi): return None
    last = t[vi] if vi is not None else None                                  # the report's last VERDICT line decides
    if last is not None and NONFINAL.search(last): return None                # ends parked/pending: not finished
    v, present = verdict_line(j)
    if present: return v
    return last

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

def report_json(cfg, u):
    """The unit's structured report, report.json next to its REPORT.md (verdict, files_changed, jobs with exit codes,
    tests_added with the failure each catches, left), or {} when it is absent or not a JSON object. janus prefers it over
    parsing the prose and falls back to REPORT.md cleanly (row: structured reports)."""
    try: d = json.loads(report_path(cfg, u).with_name("report.json").read_text())
    except (OSError, ValueError): return {}
    return d if isinstance(d, dict) else {}

def verdict_line(j):
    """('VERDICT: <V>' | None, present): the verdict a report.json states, and whether the file stated one at all. PENDING and
    WAITING are never a final verdict (item 11)."""
    if not j or "verdict" not in j: return None, False
    v = str(j.get("verdict") or "").strip().upper()
    return (f"VERDICT: {v}" if v and v not in NONFINAL_WORDS else None), True

NONFINAL = re.compile(r"VERDICT:\s*(PENDING|WAITING)\b")   # a report that ends WAITING is parked, not finished (item 11)
NONFINAL_WORDS = ("PENDING", "WAITING")

RESULT_RX = re.compile(r"^\W*(result|summary|outcome|conclusion)s?\b\W*", re.I)

def report_summary(text, j=None):
    """(verdict, one-line result) of a unit's report: report.json (j) wins over the prose; its verdict, and a
    'Result:'/'Summary:' line, else the report.json 'left', else the last content line before the final verdict."""
    L = [l.rstrip() for l in text.splitlines()]
    vi = next((i for i in range(len(L) - 1, -1, -1) if L[i].startswith("VERDICT:") and not NONFINAL.search(L[i])), None)
    v = (str(j.get("verdict") or "").strip() if isinstance(j, dict) and j.get("verdict") else "") or (L[vi][8:].strip() if vi is not None else "none")
    res = next((RESULT_RX.sub("", l) for l in L if RESULT_RX.match(l) and len(RESULT_RX.sub("", l)) > 3), None)
    if not res and isinstance(j, dict) and j.get("left"): res = str(j["left"])
    if not res:
        body = [l for l in L[1:vi if vi and vi > 0 else len(L)] if l.strip() and not l.lstrip().startswith(("#", "VERDICT:", "|---", "```"))]
        res = body[-1] if body else ""
    return (v[:24] or "none"), re.sub(r"\s+", " ", res.strip(" -*"))[:140]

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

def park_jobs(cfg, text):
    """(line index, jobs) of the report's last park line that names a job janus knows (a host in cfg.hosts): finding 1 -- the ONE
    reader verdict and waiting_line_jobs share. The ids may sit on the park line itself or on the line(s) after it (WAIT_RX's
    `\\s*` spans the line break), so a unit parked as `WAITING` with `here:9` on the next line is read the same either way.
    Finding 5: a prose line, or a park line whose ids janus cannot read (`mac:9` for an unknown host, a bare `9`), is not a park.
    Finding B residue 1: words after the park word (`WAITING on jobs:`) do not stop the reader -- the ids on the following lines
    are read too. Finding B residue 2: a bullet/numbered LIST of ids parks on ALL of them. A blank line is skipped, a `Jobs:`-like
    header is skipped, a `VERDICT:` line or a prose line ends the list. (None, []) when there is no such park line."""
    out = (None, [])
    for m in WAIT_RX.finditer(text):
        jobs = [f"{h}:{t}" for h, t in JOBREF.findall(m.group(1)) if h in cfg.hosts]
        # finding B residue 1: words after the park word (`WAITING on jobs:`) do not hide the ids on the following lines; read
        # them. Finding B residue 2: a bullet/numbered LIST of ids parks on ALL of them, so keep collecting the following lines
        # while each names a job (a blank line is skipped, a `Jobs:`-like header is skipped, a `VERDICT:` line or a prose line
        # ends the list).
        for line in text[m.end():].splitlines():
            st = line.strip()
            if not st: continue
            if st.startswith("VERDICT:"): break
            found = [f"{h}:{t}" for h, t in JOBREF.findall(line) if h in cfg.hosts]
            if found: jobs.extend(found); continue
            if st.endswith(":"): continue               # a header like `Jobs:` before the list
            break
        # finding A: the line index is a splitlines index, exactly like verdict's `text.splitlines()` (a form feed, U+2028 or
        # \x1c-\x1e counts as a line break in splitlines but not in `count("\n")`, so the two indexes must use one model).
        if jobs: out = (len(text[:m.start()].splitlines()), list(dict.fromkeys(jobs)))
    return out

def report_text(cfg, u):
    try: return report_path(cfg, u).read_text()
    except OSError: return ""

def waiting_line_jobs(cfg, u):
    """The jobs (<host>:<task>) named on the latest park line of the unit's report that it has not been resumed for yet. A resumed
    unit's report counts only once written after the unit started. Finding 1: the same reader verdict uses (park_jobs), so the two
    never disagree about whether the unit is parked."""
    try:
        if u.get("resume") and report_path(cfg, u).stat().st_mtime < u.get("started", 0): return []
    except OSError: return []
    _, jobs = park_jobs(cfg, report_text(cfg, u))
    done = set(u.get("handled_jobs", []))
    return [j for j in dict.fromkeys(jobs) if j not in done]

def unit_runs(u): return list(dict.fromkeys([*u.get("runs", []), *([u["run"]] if u.get("run") else [])]))

def unit_spent(cfg, u, wrapup=False):
    """Dollars the unit's runs have used (ledger: each run's done cost, its budget while it runs), over all its resumes; the
    wrap-up run is outside the unit's budget and counted only with wrapup=True. A still-open run counts at its BUDGET, never at
    the ledger's reservation (budget + [budget] overshoot_pct: a cap margin, not money, item 31)."""
    pv = cfg.prov.get(cfg.models.get(u.get("model"), {}).get("provider"), {})
    if "ledger" not in pv: return 0.0
    runs = ledger_read(cfg, pv)[0]
    return sum((r["cost"] or 0.0) if r["done"] else budget_of(cfg, r["budget"] or 0.0) for k in unit_runs(u)
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

__all__ = ["unit_state", "verdict", "verdict_line", "herdr_agents", "ws_id", "report_path", "report_json", "report_summary", "RESULT_RX", "main_checkout", "get_unit", "own_tree", "ENDED", "WAIT_RX", "JOBREF", "report_text", "waiting_line_jobs", "unit_runs", "unit_spent", "api_out", "all_units", "agent_ended", "unit_any"]
