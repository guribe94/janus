"""Budgets from history, spend so far, and the spend-by-outcome policy (waste pause, report checkpoints).

Queries only: a unit's default budget, its spend so far (ledger, else its transcript priced at learned rates), each run's
outcome, and whether launch and fill are paused by the waste rule."""
import calendar, json, time
from pathlib import Path
from janus.context import now, sh
from janus.findings import live_acks
from janus.ledger import RECONCILED, ledger_read
from janus.transcripts import transcript_of
from janus.units import all_units, api_out, report_text, unit_runs, verdict

# A unit's default budget is the median cost of the past finished runs of its kind and tier (ledger done rows joined to the
# units that ran them), plus [budget] margin_pct; with fewer than min_runs runs, the provider's budget_per_unit. A running
# API unit's spend so far is estimated from its Claude Code transcript (tokens per model, priced at the rate learned from
# finished runs' api.json), so check can say when a unit nears its budget with its report not updated.

# Every ledger run gets one outcome: "verdict" (its unit ended with a final verdict), "capped-partial" / "capped-nothing" (it
# ended at its budget cap and its unit has no final verdict: with or without work written: report lines, a patch), "open"
# (its unit is still open, no verdict yet), "no-verdict" (ended, not capped, unit closed without a verdict), "unknown" (no done
# row: its end was never recorded). check raises waste:<unit> for an open unit whose run ended at its cap without a verdict,
# report-due at [report] checkpoints, and waste-pause when capped-without-verdict spend passes [report] waste_pause (default
# 25 %) of the spend of the last recent_h hours: launch and fill are refused until the orchestrator acks waste-pause.

BUDGET_DEFAULTS = {"margin_pct": 30, "min_runs": 3, "min_usd": 0.5, "max_usd": None, "warn_pct": 80, "report_stale_min": 10,
                   "wrapup_usd": 0.5, "resume_min_usd": 0.1,
                   "token_weights": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}}

def bset(cfg, k): return cfg.c.get("budget", {}).get(k, BUDGET_DEFAULTS[k])

def cost_history(cfg, provider):
    """{(kind, tier): [cost of each finished run]} of a provider, oldest first. Item 16c: a run under [budget] min_usd, or one
    whose unit ended without a verdict, is left out -- 23 tiny capped runs skewed the median to $0.09 and every real unit started
    at a $0.50 cap (the waste cause)."""
    pv = cfg.prov[provider]
    if "ledger" not in pv: return {}
    runs = ledger_read(cfg, pv)[0]; us = all_units(cfg); byrun = {u.get("run"): u for u in us.values() if u.get("run")}
    out = {}
    for k, r in runs.items():
        if not r["done"] or r["cost"] is None or r["cost"] < bset(cfg, "min_usd"): continue
        u = byrun.get(k) or us.get(r["unit"])
        if not u or not u.get("kind") or not verdict(cfg, {**u, "resume": False}): continue
        out.setdefault((u["kind"], u.get("tier") or "normal"), []).append(r["cost"])
    return out

def budget_for(cfg, mc, kind, tier):
    """(budget USD, why) for a new API unit of this kind and tier."""
    pv = cfg.prov[mc["provider"]]; d = pv.get("budget_per_unit", 15)
    c = cost_history(cfg, mc["provider"]).get((kind, tier), [])
    if len(c) < bset(cfg, "min_runs"): return d, f"budget_per_unit; {len(c)} past {kind}/{tier} run(s), {bset(cfg, 'min_runs')} needed to learn"
    s = sorted(c); med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    b = max(bset(cfg, "min_usd"), -(-med * (100 + bset(cfg, "margin_pct")) / 100 * 2 // 1) / 2)   # rounded up to $0.50
    if bset(cfg, "max_usd"): b = min(b, bset(cfg, "max_usd"))
    return b, f"median ${med:.2f} of {len(c)} past {kind}/{tier} run(s) + {bset(cfg, 'margin_pct')}%"

def token_cost(usage, w, rate):
    """Dollars for a usage dict (api.json usage or transcript message usage) at rate $ per weighted token."""
    return rate * (w["input"] * usage.get("input_tokens", usage.get("inputTokens", 0)) + w["output"] * usage.get("output_tokens", usage.get("outputTokens", 0))
                   + w["cache_write"] * usage.get("cache_creation_input_tokens", usage.get("cacheCreationInputTokens", 0))
                   + w["cache_read"] * usage.get("cache_read_input_tokens", usage.get("cacheReadInputTokens", 0)))

def learned_rates(cfg):
    """{model id: $ per weighted token}, from the modelUsage of the newest finished api.json files (cost / weighted tokens)."""
    if cfg.run.rates is not None: return cfg.run.rates
    w = bset(cfg, "token_weights"); acc = {}
    fs = sorted(cfg.p("out", "out").glob("*/api.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:60]
    for f in fs:
        try: mu = json.loads(f.read_text()).get("modelUsage") or {}
        except Exception: continue
        for m, x in mu.items():
            wt = token_cost(x, w, 1.0)
            if wt > 0 and isinstance(x.get("costUSD"), (int, float)): a = acc.setdefault(m, [0.0, 0.0]); a[0] += x["costUSD"]; a[1] += wt
    cfg.run.rates = {m: c / wt for m, (c, wt) in acc.items() if wt}
    return cfg.run.rates

def unit_spend(cfg, u):
    """(dollars so far, how known) of an API unit: its api.json cost when finished, else its transcript priced at the learned
    rate; (None, why) when it cannot be told (remote, no transcript, no learned rate)."""
    try:
        c = json.loads((cfg.p("out", "out") / u["name"] / "api.json").read_text()).get("total_cost_usd")
        if isinstance(c, (int, float)): return float(c), "api.json"
    except Exception: pass
    if not cfg.hosts.get(u.get("host"), {}).get("local"): return None, "remote"
    f = transcript_of(cfg, u)
    if not f: return None, "no transcript"
    return transcript_cost(cfg, f)

def transcript_cost(cfg, f, since=0):
    """(dollars, "transcript") of a Claude Code transcript's assistant messages (those after since, a time), priced at the
    learned rate; (None, why) without a learned rate."""
    rates = learned_rates(cfg); w = bset(cfg, "token_weights"); seen = {}
    for l in open(f, errors="replace"):
        if '"usage"' not in l: continue
        try: d = json.loads(l)
        except Exception: continue
        m = d.get("message") or {}
        if since:
            try:
                if calendar.timegm(time.strptime(d.get("timestamp", "")[:19], "%Y-%m-%dT%H:%M:%S")) < since: continue   # UTC
            except ValueError: pass
        if d.get("type") == "assistant" and isinstance(m, dict) and m.get("usage"): seen[m.get("id") or len(seen)] = (m.get("model"), m["usage"])
    if not seen: return 0.0, "transcript"
    tot = 0.0
    for mdl, us in seen.values():
        r = rates.get(mdl) or (sum(rates.values()) / len(rates) if rates else None)
        if r is None: return None, "no learned rate (no finished api.json yet)"
        tot += token_cost(us, w, r)
    return tot, "transcript"

OUTCOMES = (("verdict", "finished with a verdict"), ("capped-partial", "capped, partial work"), ("capped-nothing", "capped, nothing written"),
            ("open", "open unit, no verdict yet"), ("no-verdict", "ended without a verdict"), ("unknown", "unknown (no done row)"),
            ("reconciled", "agent gone, no done row: reconciled, budget counted as the upper bound"))

REPORT_DEFAULTS = {"every_h": 4, "at": [], "waste_pause": 25, "recent_h": 24}

def rset(cfg, k): return cfg.c.get("report", {}).get(k, REPORT_DEFAULTS[k])

def work_written(cfg, u, since=0):
    """The unit left work: report lines beyond its VERDICT line, a non-empty patch of its changes, or in its worktree a commit
    made since the run started (since) or uncommitted changes."""
    t = [l for l in report_text(cfg, u).splitlines() if l.strip() and not l.startswith("VERDICT:") and not l.startswith("WAITING")]
    od = cfg.p("out", "out") / u["name"]
    if len(t) >= 2 or any((od / f).is_file() and (od / f).stat().st_size > 0 for f in ("changes.patch", "backup.patch")): return True
    c = Path(u.get("cwd") or "/nonexistent")
    if not c.is_dir() or c.resolve() == cfg.root.resolve(): return False
    if since and sh(["git", "-C", str(c), "log", f"--since={int(since)}", "--format=%H", "-1"])[1].strip(): return True
    return bool(sh(["git", "-C", str(c), "status", "--porcelain", "--untracked-files=no"])[1].strip())

def run_outcomes(cfg):
    """[(provider, run id, unit name, dollars, outcome, time)] for every ledger run."""
    us = all_units(cfg); open_ = {u["name"] for u in us.values() if u.get("status") in ("open", "in_progress")}
    byrun = {k: u for u in us.values() for k in unit_runs(u)}; out = []
    for pn, pv in cfg.prov.items():
        if "ledger" not in pv: continue
        for k, r in ledger_read(cfg, pv)[0].items():
            u = byrun.get(k) or us.get(r["unit"]) or {"name": r["unit"]}
            try: t = time.mktime(time.strptime(r["ts"][:19], "%Y-%m-%d %H:%M:%S"))
            except (ValueError, TypeError): t = 0
            cost = r["cost"] if r["done"] else (r["budget"] or 0.0)
            od = cfg.p("out", "out") / u["name"]
            try: aj = json.loads((od / f"api-{k}.json").read_text())
            except Exception: aj = api_out(cfg, u) if k == (u.get("run") or k) else {}
            v = verdict(cfg, {**u, "resume": False}) if u.get("name") else None
            if r["done"] and r.get("how") == RECONCILED: o = "reconciled"
            elif v: o = "verdict"
            elif aj.get("subtype") == "error_max_budget_usd": o = "capped-partial" if work_written(cfg, u, t) else "capped-nothing"
            elif not r["done"]: o = "open" if u["name"] in open_ else "unknown"
            elif u["name"] in open_: o = "open"
            else: o = "no-verdict"
            out.append((pn, k, u["name"], float(cost or 0), o, t))
    return out

def waste_state(cfg, outs=None):
    """(capped-without-verdict dollars, all dollars, share %) of the runs of the last [report] recent_h hours."""
    outs = run_outcomes(cfg) if outs is None else outs; cut = now() - rset(cfg, "recent_h") * 3600
    rec = [x for x in outs if x[5] >= cut]; tot = sum(x[3] for x in rec); w = sum(x[3] for x in rec if x[4].startswith("capped"))
    return w, tot, (100 * w / tot if tot else 0.0)

def waste_paused(cfg):
    """Why launch and fill are paused (wasted spend over [report] waste_pause % of recent spend). Item 7: the reason is stated
    even while the ack is live, so the finding exists, run_check's ack filter hides it, and the ack lasts its TTL; waste_block is
    the ack-aware refusal launch and fill use."""
    w, tot, pct = waste_state(cfg)
    if pct <= rset(cfg, "waste_pause"): return None
    return (f"${w:.2f} of ${tot:.2f} ({pct:.0f} %) of the last {rset(cfg, 'recent_h')} h went to runs that ended at their budget cap without a "
            f"verdict (limit {rset(cfg, 'waste_pause')} %): find the cause (janus report), then janus ack waste-pause \"<cause and fix>\"")

def waste_block(cfg):
    """Why launch and fill are refused now: waste_paused, unless the orchestrator acked waste-pause (the ack lasts its TTL)."""
    return None if "waste-pause" in live_acks(cfg) else waste_paused(cfg)

def last_checkpoint(cfg):
    """The latest report checkpoint passed: [report] at (local HH:MM times) and every every_h hours from midnight."""
    t = now(); lt = time.localtime(t); mid = time.mktime((*lt[:3], 0, 0, 0, 0, 0, -1)); cps = []
    for x in rset(cfg, "at"):
        try: h, m = map(int, str(x).split(":")); c = mid + h * 3600 + m * 60; cps.append(c if c <= t else c - 86400)
        except ValueError: pass
    if rset(cfg, "every_h"): e = rset(cfg, "every_h") * 3600; cps.append(mid + ((t - mid) // e) * e)
    return max(cps) if cps else None
