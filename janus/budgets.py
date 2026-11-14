"""Budgets from history, spend so far and each run's outcome.

Queries only: a unit's default budget, its spend so far (ledger, else its transcript priced at learned rates), each run's
outcome."""
import calendar, json, re, time
from pathlib import Path
from janus.context import Err, hrun, sh
from janus.ledger import RECONCILED, budget_of, committed, ledger_read, reserved, run_time
from janus.transcripts import remote_slug_sh, transcript_of
from janus.usage import usage_scan
import janus.usage as usage_mod
from janus.units import Beads, all_units, api_out, report_text, unit_runs, unit_spent, verdict

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
                   "wrapup_usd": 0.5, "resume_min_usd": 0.1, "finish_reserve": True,
                   "token_weights": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}}

def bset(cfg, k): return cfg.c.get("budget", {}).get(k, BUDGET_DEFAULTS[k])

def run_capped(cfg, u, run):
    """Whether ledger run `run` of unit u ended at its budget cap (finding 10): that run's own api-<run>.json; the shared
    api.json only when `run` is the unit's latest run, so an earlier run of a unit whose LAST run was capped is not counted
    capped too (api.json carries only the latest run's subtype)."""
    od = cfg.p("out", "out") / u["name"]; p = od / f"api-{run}.json"
    if not p.is_file():
        if u.get("run") != run: return False
        p = od / "api.json"
    try: return json.loads(p.read_text()).get("subtype") == "error_max_budget_usd"
    except (OSError, ValueError): return False

RESUME_RUN = re.compile(r"\.r\d+\.\d+$")   # a resume's run id is <unit>.r<k>.<time>; any other run id is a unit's first run

def cost_history(cfg, provider):
    """{(kind, tier): [what each past unit cost]} of a provider, oldest first. One sample per UNIT: its first run and every
    resume of it added up (budget-follow item 5: a unit's budget pays for all its runs, and each resume pays to load the whole
    context again, so the median of single runs taught a budget the next unit could not finish on). A name launched again is
    a unit of its own. A unit counts when it ended with a verdict, when a run of it ended at its budget cap (the budget was too
    small, so it raises it), or when it cost at least [budget] min_usd (a real unit whose report was tidied still counts,
    finding 9); only a tiny unit without a verdict is dropped (the 23 tiny runs that skewed the median to $0.09, item 16c)."""
    pv = cfg.prov[provider]
    if "ledger" not in pv: return {}
    runs = ledger_read(cfg, pv)[0]; us = all_units(cfg); byrun = {u.get("run"): u for u in us.values() if u.get("run")}
    groups, last = [], {}   # [unit meta, cost, capped] per unit, in ledger order; last: the open group of each unit name
    for k, r in runs.items():
        if not r["done"] or r["cost"] is None: continue
        u = byrun.get(k) or us.get(r["unit"])
        if not u or not u.get("kind"): continue
        g = last.get(r["unit"]) if RESUME_RUN.search(k) else None
        if g is None: g = last[r["unit"]] = [u, 0.0, False]; groups.append(g)
        g[1] += r["cost"]; g[2] = g[2] or run_capped(cfg, u, k)
    out = {}
    for u, cost, capped in groups:
        if not (verdict(cfg, {**u, "resume": False}) or capped or cost >= bset(cfg, "min_usd")): continue
        out.setdefault((u["kind"], u.get("tier") or "normal"), []).append(round(cost, 6))
    return out

def budget_for(cfg, mc, kind, tier):
    """(budget USD, why) for a new API unit of this kind and tier."""
    pv = cfg.prov[mc["provider"]]; d = pv.get("budget_per_unit", 15)
    c = cost_history(cfg, mc["provider"]).get((kind, tier), [])
    if len(c) < bset(cfg, "min_runs"): return d, f"budget_per_unit; {len(c)} past {kind}/{tier} unit(s), {bset(cfg, 'min_runs')} needed to learn"
    s = sorted(c); med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    b = max(bset(cfg, "min_usd"), -(-med * (100 + bset(cfg, "margin_pct")) / 100 * 2 // 1) / 2)   # rounded up to $0.50
    if bset(cfg, "max_usd"): b = min(b, bset(cfg, "max_usd"))
    return b, f"median ${med:.2f} of {len(c)} past {kind}/{tier} unit(s), their resumes included, + {bset(cfg, 'margin_pct')}%"

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

def session_file(cfg, u, sid=None):
    """The transcript of a local unit's agent session: ~/.claude/projects/*/<session id>.jsonl, else the one its brief names."""
    sid = sid or api_out(cfg, u).get("session_id")
    f = next(iter(sorted((Path.home() / ".claude/projects").glob(f"*/{sid}.jsonl"))), None) if sid else None
    return f or transcript_of(cfg, u)

def session_usage(cfg, u, since=0, sid=None):
    """usage_scan of the unit's agent session ({ctx, model, use}), or None when it cannot be read: here from its transcript; for
    a unit on another host janus/usage.py runs there (one ssh, one JSON line back)."""
    h = cfg.hosts.get(u.get("host"), {})
    if h.get("local"):
        f = session_file(cfg, u, sid)
        try: return usage_scan(f, since) if f else None
        except OSError: return None
    sid = sid or api_out(cfg, u).get("session_id")
    if sid and not re.fullmatch(r"[A-Za-z0-9-]{3,64}", str(sid)): return None
    if not sid and not u.get("rcwd"): return None
    # a first run has no session id here until it ends: the newest transcript of its own tree there (item 6)
    pick = f'f="$HOME/.claude/projects/$s/{sid}.jsonl"' if sid else 'f=$(ls -t "$HOME/.claude/projects/$s"/*.jsonl 2>/dev/null | head -1)'
    rc, o, e = hrun(h, f"s={remote_slug_sh(u)}\n{h.get('agent_env', '')}\n{pick}\npython3 - \"$f\" {int(since)}",
                    inp=Path(usage_mod.__file__).read_text(), timeout=40)
    try: d = json.loads(o.strip().splitlines()[-1]) if rc == 0 and o.strip() else None
    except ValueError: d = None
    return d if isinstance(d, dict) and "ctx" in d else None

def model_rate(cfg, model):
    """$ per weighted token of a model (learned_rates; an unknown model gets the mean), None when nothing is learned yet."""
    rates = learned_rates(cfg)
    return rates.get(model) or (sum(rates.values()) / len(rates) if rates else None)

def reload_usd(cfg, use):
    """What a resume pays before it does anything: the session's context (use: session_usage) written to the cache again (the
    cache is cold after a wait) at its model's learned rate. 0.0 when it is not known."""
    r = model_rate(cfg, (use or {}).get("model"))
    return round(use["ctx"] * bset(cfg, "token_weights")["cache_write"] * r, 4) if use and use.get("ctx") and r else 0.0

# Budget-follow item 6: the agent CLI stops a run at its --max-budget-usd without a word to the agent. So every API run is told
# its budget (budget_note, in its first prompt and in every resume message), and the watcher measures a running run's spend
# from its session's transcript (run_spend: here, or on the unit's host) and orders it once, at [budget] warn_pct, to checkpoint.

def budget_note(cfg, budget, spent=None, total=None, reload=0.0):
    """What a run is told about its money: this run's budget, what its unit has used, and the checkpoint rule."""
    used = ""
    if total: used = f" (your unit has used ${spent or 0:.2f} of its ${total:g}" + (f"; about ${reload:.2f} of this run goes to loading your context again" if reload else "") + ")"
    return (f"Budget: this run stops without warning at ${budget:.2f}{used}. Commit and write your state in the report at each checkpoint; "
            f"at the board's budget checkpoint (about {bset(cfg, 'warn_pct')} %), commit, record your state, then park on your jobs or finish.")

def run_spend(cfg, u):
    """(dollars the unit's open run has used so far, that run's budget): its agent session's messages since the run started,
    priced at the learned rates. (None, budget) when it cannot be told; (None, None) when the unit has no open run."""
    pv = cfg.prov.get(cfg.models.get(u.get("model"), {}).get("provider"), {})
    r = ledger_read(cfg, pv)[0].get(u.get("run")) if "ledger" in pv and u.get("run") else None
    if not r or r["done"]: return None, None
    b = budget_of(cfg, r["budget"] or 0.0); use = session_usage(cfg, u, since=run_time(u["run"]) or u.get("started", 0)); w = bset(cfg, "token_weights"); tot = 0.0
    for mdl, us in ((use or {}).get("use") or {}).items():
        rate = model_rate(cfg, mdl)
        if rate is None: return None, b
        tot += token_cost(us, w, rate)
    return (round(tot, 4) if use else None), b

OUTCOMES = (("verdict", "finished with a verdict"), ("capped-partial", "capped, partial work"), ("capped-nothing", "capped, nothing written"),
            ("open", "open unit, no verdict yet"), ("no-verdict", "ended without a verdict"), ("unknown", "unknown (no done row)"),
            ("reconciled", "agent gone, no done row: reconciled, budget counted as the upper bound"))

# The finish reserve (budget-follow item 4): an open unit that has not finished still needs the rest of its budget (a parked or
# capped unit is resumed on it). New work is admitted against the cap MINUS that reserve, so the cap is never spent down by new
# units while open ones can no longer finish; a plain resume draws on its own unit's reserve and is exempt. [budget]
# finish_reserve = false turns it off.

def unit_need(cfg, u):
    """Dollars an open API unit still needs to finish: its budget minus what its runs used (a still-open run counts at its
    budget, so a unit in its first run needs nothing more); 0 once it has a final verdict. A unit stopped by its budget and
    recorded resumable (item 7) needs the new budget it was recorded with, whatever its wrap-up wrote."""
    if cfg.models.get(u.get("model"), {}).get("kind") != "api" or not u.get("budget"): return 0.0
    if u.get("resumable"): return max(0.0, round(u["budget"] - unit_spent(cfg, u), 2), float(u["resumable"].get("need") or 0))   # item 7
    return 0.0 if verdict(cfg, u) else max(0.0, round(u["budget"] - unit_spent(cfg, u), 2))

def finish_reserve(cfg, provider, units=None, but=()):
    """(dollars, {unit: dollars}): what the provider's open units still need to finish, without the units named in but. Each
    need is counted as the ledger will hold it when the unit is resumed (the rest of its budget plus the overshoot margin), so
    every open unit can be resumed, all at once if need be."""
    if not bset(cfg, "finish_reserve"): return 0.0, {}
    if units is None:
        try: units = Beads(cfg).units()
        except Err: units = []
    need = {u["name"]: n for u in units if u["name"] not in but and cfg.models.get(u.get("model"), {}).get("provider") == provider and (n := reserved(cfg, unit_need(cfg, u)))}
    return round(sum(need.values()), 2), need

def names_some(need, n=4):
    L = sorted(need, key=lambda k: -need[k])
    return ", ".join(f"{k} ${need[k]:.2f}" for k in L[:n]) + (f", {len(L) - n} more" if len(L) > n else "")

def finish_refusal(cfg, mc, name, budget, units=None, but=()):
    """Why a new $budget run of the provider is refused for the finish reserve (None: it fits, or no cap is set)."""
    pv = cfg.prov.get(mc.get("provider"), {})
    if not pv.get("cap_usd") or "ledger" not in pv or budget is None: return None
    keep, need = finish_reserve(cfg, mc["provider"], units, (name, *but)); c = committed(cfg, pv); held = reserved(cfg, budget)
    if not keep or c + keep + held <= pv["cap_usd"]: return None
    return (f"refused: ${c:.2f} committed + ${keep:.2f} kept for {len(need)} open unit(s) to finish ({names_some(need)}) + ${held:.2f} (${budget:g} and its "
            f"overshoot margin) would pass the ${pv['cap_usd']:g} cap of {mc['provider']}: let them finish or close one (janus close <unit>), or ask the "
            f"owner to raise {mc['provider']}'s cap_usd")


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
