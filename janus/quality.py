"""The quality subsystem: the settings behind the rules every brief carries, the pre-landing review a cheap model runs over a
unit's diff, and the block `janus land` applies to its findings.

The brief header (launch.brief_header) and the brief lint (launch.lint_brief) live in launch.py, where the header is built; the
gate test-signal lives in gates.py; the report.json the review and the report parsers read lives in units.py. This module holds
what those share: the `[quality]` settings and the review/land-block pair (janus quality review, janus land)."""
import json, re, subprocess
from pathlib import Path
from janus.context import Err, atomic_write, clean_env, say, sh, ts
from janus.gates import landing_branch
from janus.ledger import ledger_done, model_ok, read_secret, reserve_budget
from janus.units import unit_any

# [quality] settings. Nothing here runs unless the project asks: the brief lint when brief_lint is set, the pre-landing review
# when review_model is set. rules_file is a pointer the header names (relative to the project root), not content.
QUALITY_DEFAULTS = {"rules_file": "QUALITY.md", "brief_lint": False, "review_model": None, "review_budget_usd": 0.5,
                    "review_max_chars": 60000, "review_timeout_s": 600, "signal_runs": 5, "signal_min_min": 20, "soak_every_s": 20}

def qset(cfg, k): return cfg.c.get("quality", {}).get(k, QUALITY_DEFAULTS[k])

REVIEW_PROMPT = """You review one unit's diff before it lands. Judge exactly two things:
1. slop: filler, needless or dead code, comments that restate the code, speculative abstractions nobody asked for, duplicated logic.
2. test value: tests with no real failure, duplicates of a check another test already makes, assertions that cannot fail.
Reply with JSON only, no prose: {{"findings": [{{"kind": "slop" or "test", "where": "<file:line>", "why": "<one line>"}}]}}.
An empty findings list means the diff is clean.

Unit: {unit}
Diff:
{diff}
"""

def unit_diff(cfg, u, max_chars=None):
    """(head sha, diff text) of a unit's tree against the landing branch's merge-base: committed, staged and unstaged tracked
    changes, plus the names of untracked files (their content is not diffed). A remote unit is read on its host."""
    cwd = u.get("cwd")
    if not cwd or not Path(cwd).is_dir(): raise Err(f"no tree for unit {u.get('name')}: nothing to review")
    br = landing_branch(cfg, Path(cwd))
    mb = sh(["git", "-C", str(cwd), "merge-base", "HEAD", br])[1].strip()
    head = sh(["git", "-C", str(cwd), "rev-parse", "HEAD"])[1].strip()
    diff = sh(["git", "-C", str(cwd), "diff", mb] if mb else ["git", "-C", str(cwd), "diff", "HEAD"], timeout=120)[1]
    unt = [l[3:].strip() for l in sh(["git", "-C", str(cwd), "status", "--porcelain", "-uall"])[1].splitlines() if l[:2] == "??"]
    if unt: diff += "\n# untracked files (not diffed here): " + ", ".join(unt[:50])
    n = max_chars if max_chars is not None else qset(cfg, "review_max_chars")
    return head, diff[:n] + (f"\n... (diff cut at {n} chars)" if len(diff) > n else "")

def parse_review(text):
    """The findings the model returned: [{kind, where, why}]. A non-JSON answer becomes one finding naming that, so an
    unreadable review blocks the landing instead of passing silently."""
    t = text
    try:
        d = json.loads(t)
        if isinstance(d, dict) and isinstance(d.get("result"), str): t = d["result"]
    except ValueError: pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try: d = json.loads(m.group(0))
        except ValueError: d = None
        if isinstance(d, dict) and isinstance(d.get("findings"), list):
            return [{"kind": str(f.get("kind", "review")), "where": str(f.get("where", "-")), "why": str(f.get("why", ""))[:300]}
                    for f in d["findings"] if isinstance(f, dict)]
    return [{"kind": "review", "where": "-", "why": "the model's answer was not JSON: " + re.sub(r"\s+", " ", t)[:200]}]

def review_unit(cfg, unit, override=None):
    """janus quality review <unit>: a cheap model ([quality] review_model) reads the unit's diff and writes out/<unit>/review.json
    (findings block its landing). --override <reason> records that the findings are accepted; no model run then."""
    u = unit_any(cfg, unit)
    if not u or not u.get("cwd"): raise Err(f"no unit {unit}")
    rj = cfg.p("out", "out") / unit / "review.json"
    if override:
        try: d = json.loads(rj.read_text())
        except (OSError, ValueError): d = {"unit": unit, "findings": []}
        d["override"] = str(override); d["overridden_at"] = ts()
        if cfg.run.dry: say(f"+ write {rj} with override: {override}"); return 0, []
        atomic_write(rj, json.dumps(d, indent=1) + "\n")
        return 0, [f"{unit}: review findings accepted with a reason: {override}"]
    model = qset(cfg, "review_model")
    if not model: raise Err("set [quality] review_model in janus.toml (a cheap api model) to review before landing")
    mc = cfg.models.get(model) or {}
    if mc.get("kind") != "api": raise Err(f"review_model {model} is not an api model (janus runs it itself; kind = \"api\")")
    pv = cfg.prov.get(mc["provider"], {})
    ok, why = model_ok(cfg, model)
    if not ok: raise Err(f"review_model {model}: {why}")
    head, diff = unit_diff(cfg, u)
    if not diff.strip(): return 0, [f"{unit}: nothing to review (no change against the landing branch)"]
    prompt = REVIEW_PROMPT.format(unit=unit, diff=diff)
    budget = float(qset(cfg, "review_budget_usd"))
    if cfg.run.dry:
        say(f"+ bd-less review run: {model}, budget ${budget:g}, {len(diff)} diff chars -> {rj}"); return 0, []
    from janus.actions.launch import api_argv   # lazy: launch imports this module for qset
    var, key = read_secret(pv)
    run = reserve_budget(cfg, mc, unit, budget)
    env = clean_env({var: key, "JANUS_ROLE": "agent", "JANUS_UNIT": unit})
    try:
        p = subprocess.run(api_argv(pv, mc, budget, prompt), cwd=str(cfg.root), env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=float(qset(cfg, "review_timeout_s")))
    except subprocess.TimeoutExpired:
        ledger_done(cfg, pv, run, unit, budget, "timeout")
        raise Err(f"review of {unit} timed out after {qset(cfg, 'review_timeout_s')}s (its budget was charged)")
    finally:
        del key, env
    cost, reported = run_cost_text(p.stdout or p.stderr, budget)
    ledger_done(cfg, pv, run, unit, cost, p.returncode)
    if p.returncode: raise Err(f"review of {unit} failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-200:]}")
    findings = parse_review(p.stdout)
    d = {"unit": unit, "model": model, "at": ts(), "head": head, "cost": round(cost, 4), "findings": findings, "override": None}
    atomic_write(rj, json.dumps(d, indent=1) + "\n")
    L = [f"{unit}: review by {model} (${cost:.4f}): " + (f"{len(findings)} finding(s)" if findings else "clean")]
    for f in findings: L.append(f"  [{f['kind']}] {f['where']}: {f['why']}")
    return (1 if findings else 0), L

def run_cost_text(text, budget):
    """(cost, reported) from a model CLI's stdout/stderr JSON (total_cost_usd), else the whole budget like run_cost."""
    try:
        d = json.loads(text)
        c = d.get("total_cost_usd") if isinstance(d, dict) else None
    except ValueError: c = None
    return (float(c), True) if isinstance(c, (int, float)) else (float(budget or 0), False)

def land_review(cfg, u):
    """Why janus land must not land the unit yet, or None. Only when [quality] review_model is set: a missing, stale (its tree
    moved) or open review blocks the landing until it passes or is overridden with a reason."""
    if not qset(cfg, "review_model"): return None
    name = u.get("name") or ""
    rj = cfg.p("out", "out") / name / "review.json"
    try: d = json.loads(rj.read_text())
    except (OSError, ValueError):
        return f"{name} has no pre-landing review ([quality] review_model {qset(cfg, 'review_model')} is set): run janus quality review {name}"
    cwd = u.get("cwd")
    head = sh(["git", "-C", str(cwd), "rev-parse", "HEAD"])[1].strip() if cwd and Path(cwd).is_dir() else ""
    if head and d.get("head") != head:
        return f"{name}'s review is stale (its tree moved since it was reviewed): run janus quality review {name}"
    f = d.get("findings") or []
    if f and not d.get("override"):
        return (f"{name}'s pre-landing review found {len(f)} issue(s): " + "; ".join(f"[{x.get('kind')}] {x.get('where')}: {x.get('why')}" for x in f[:4])
                + (f" (+{len(f) - 4} more)" if len(f) > 4 else "") + f" — fix them, or janus quality review {name} --override '<reason>'")
    return None

def cmd_quality(cfg, a):
    """janus quality lint <brief|unit>: the brief lint alone (read-only). janus quality review <unit> [--override '<reason>']:
    the pre-landing review, or the record that its findings are accepted."""
    if a.action != "lint": return review_unit(cfg, a.arg, override=a.override)
    from janus.actions.launch import lint_brief   # lazy: launch imports this module for qset
    if not a.arg: raise Err("janus quality lint <brief path or unit name>")
    bp = Path(a.arg)
    if not bp.is_file(): bp = cfg.p("briefs", "briefs") / (a.arg if a.arg.endswith(".md") else a.arg + ".md")
    if not bp.is_file(): raise Err(f"brief {a.arg} not found")
    cwd = cfg.p("worktrees", "worktrees") / bp.stem
    P = lint_brief(cfg, bp, cwd=cwd if cwd.is_dir() else None)
    return (1 if P else 0), ([f"brief {bp}: {x}" for x in P] or [f"brief {bp}: clean"])

__all__ = ["QUALITY_DEFAULTS", "qset", "unit_diff", "parse_review", "review_unit", "land_review", "cmd_quality", "run_cost_text"]
