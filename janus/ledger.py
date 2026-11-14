"""The API ledger (janus owns it) and the secret store (section 7, Step 2).

parse/read/spend/committed/append, reserve_budget/ledger_done (one row per run under Locked; idempotent), run_cost,
settle_run, read_secret/model_ok. Every row is written once under the fcntl lock (section 9: keep the lock and per-run
idempotency). unreconciled() stays with its callers until the findings split (it reads budgets/park helpers)."""
from janus.context import NUM, Locked, Err, atomic_write, say, now
from janus.platform import secret_get
import json, os, re, time
from pathlib import Path

# One TSV per provider ([providers.<p>] ledger), one row per event:  ts  unit  running|<cost>  <budget>|done rc=<x>  <run id>.
# Every run id has exactly one `running` row (its budget, written when the budget is reserved) and at most one `done` row
# (its cost; the whole budget when the run never reported one). Every write takes the ledger's lock (<ledger>.lock, an fcntl
# lock: the lock zsh's `zsystem flock` takes too) and is idempotent: a second running or done row for a run id is never
# written, so a retry, a re-run or a looping launcher cannot duplicate rows (janus-fixes7: a launcher edited while it ran
# looped and appended ~49,000 done rows; the cap check summed them all). Spend is computed per run id: its done cost, or its
# budget while it runs. A malformed ledger refuses every new run until `janus ledger repair`.

def ledger_path(cfg, pv): return cfg.root / pv["ledger"]

def ledger_parse(text):
    """(runs, problems, notes). runs: {run id: {unit, running: [line numbers], done: [...], budget, cost}}. problems make the
    ledger malformed (a launch is refused); notes are only reported."""
    runs, bad, notes = {}, [], []
    for i, l in enumerate(text.splitlines(), 1):
        if not l.strip(): continue
        f = l.split("\t")
        if len(f) != 5 or not f[1].strip() or not f[4].strip():
            bad.append(f"line {i}: {len(f)} field(s), want 5 (ts, unit, running|cost, budget|done rc=, run id): {l[:80]!r}"); continue
        r = runs.setdefault(f[4], {"unit": f[1], "running": [], "done": [], "budget": None, "cost": None, "ts": f[0]})
        if r["unit"] != f[1]: bad.append(f"line {i}: run id {f[4]} belongs to {r['unit']}, not {f[1]}"); continue
        if f[2] == "running":
            if not NUM.match(f[3].strip()): bad.append(f"line {i}: running row without a numeric budget: {f[3][:20]!r}"); continue
            r["running"].append(i)
            if r["budget"] is None: r["budget"] = float(f[3])
        elif NUM.match(f[2].strip()) and f[3].startswith("done"):
            r["done"].append(i)
            if r["cost"] is None: r["cost"] = float(f[2]); r["how"] = f[3]
        else: bad.append(f"line {i}: neither a running row nor a done row: {l[:80]!r}")
    for k, r in runs.items():
        if len(r["running"]) > 1: bad.append(f"run {k} ({r['unit']}): {len(r['running'])} running rows (lines {', '.join(map(str, r['running'][:5]))}{'...' if len(r['running']) > 5 else ''})")
        if len(r["done"]) > 1: bad.append(f"run {k} ({r['unit']}): {len(r['done'])} done rows (lines {', '.join(map(str, r['done'][:5]))}{'...' if len(r['done']) > 5 else ''})")
        if r["done"] and not r["running"]: notes.append(f"run {k} ({r['unit']}): a done row without a running row (counted)")
    return runs, bad, notes

def ledger_read(cfg, pv):
    try: return ledger_parse(ledger_path(cfg, pv).read_text())
    except FileNotFoundError: return {}, [], []

def spend(runs):
    """Committed dollars: per run id, its done cost, or its budget while it runs."""
    return sum(r["cost"] if r["done"] else (r["budget"] or 0.0) for r in runs.values())

def committed(cfg, pv): return spend(ledger_read(cfg, pv)[0])

def ledger_append(cfg, pv, row):
    """One row, appended with a single write to an O_APPEND descriptor (the caller holds the lock)."""
    p = ledger_path(cfg, pv); p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try: os.write(fd, ("\t".join(map(str, row)) + "\n").encode())
    finally: os.close(fd)

def tstamp(): return time.strftime("%Y-%m-%d %H:%M:%S")

OVERSHOOT_PCT = 20   # [budget] overshoot_pct: runs overshoot their --max-budget-usd (observed: 21 runs over, worst 18 %)

def reserved(cfg, budget):
    """What a run's budget holds in the ledger while it runs: the budget plus [budget] overshoot_pct (default 20 %)."""
    return round(float(budget) * (1 + cfg.c.get("budget", {}).get("overshoot_pct", OVERSHOOT_PCT) / 100), 2)

def budget_of(cfg, held):
    """The plain budget a `running` row's held amount stands for. The row holds the reservation (`reserved`), which is a cap
    margin, not money: a unit's spend must never be read from it (item 31: a unit launched with --budget 1.5 read 'spent $1.80
    of $1.5' while its run had reported $0.10)."""
    return round(float(held) / (1 + cfg.c.get("budget", {}).get("overshoot_pct", OVERSHOOT_PCT) / 100), 2)

def reserve_budget(cfg, mc, name, budget, run=None):
    """Under the ledger's lock: refuse when the ledger is malformed or the reservation (the budget plus the overshoot margin)
    would pass the cap, else write the run's one running row holding the reservation. Idempotent: a run id that already has its
    running row is returned as it is. Returns the run id."""
    pv = cfg.prov[mc["provider"]]; run = run or f"{name}.{os.getpid()}.{int(now())}"; held = reserved(cfg, budget)
    if cfg.run.dry: return run
    with Locked(ledger_path(cfg, pv)):
        runs, bad, _ = ledger_read(cfg, pv)
        if bad: raise Err(f"refused: the {mc['provider']} ledger {pv['ledger']} is malformed ({bad[0]}; {len(bad)} problem(s)): run janus ledger check, then janus ledger repair")
        r = runs.get(run)
        if r and r["done"]: raise Err(f"refused: run {run} is already finished (done row at line {r['done'][0]}); a re-run gets a new run id")
        if r and r["running"]: return run
        if "cap_usd" in pv:
            c = spend(runs)
            if c + held > pv["cap_usd"]: raise Err(f"refused: ${c:.2f} committed + ${held} (${budget} and its overshoot margin) would pass the ${pv['cap_usd']} cap of {mc['provider']}")
        ledger_append(cfg, pv, [tstamp(), name, "running", held, run])
    return run

def ledger_done(cfg, pv, run, name, cost, rc):
    """Write the run's one done row (idempotent: nothing when it already has one). Returns True when written."""
    if cfg.run.dry: return False
    with Locked(ledger_path(cfg, pv)):
        r = ledger_read(cfg, pv)[0].get(run)
        if r and r["done"]: return False
        ledger_append(cfg, pv, [tstamp(), name, cost, RECONCILED if rc == "unknown" else f"done rc={rc}", run])
    return True

def run_json(path):
    """A run's json output as a dict ({} when it is missing, cut short or not an object)."""
    try: d = json.loads(Path(path).read_text())
    except Exception: return {}
    return d if isinstance(d, dict) else {}

def run_time(run):
    """The start time a run id ends with (<unit>.<pid>.<time>, <unit>.r<k>.<time>); 0 when it has none."""
    t = str(run).rsplit(".", 1)[-1]; return int(t) if t.isdigit() else 0

def session_before(od, run, sid):
    """The cost the agent's SESSION had reported before run `run`: the total in the json of the latest earlier run of the same
    session (out/<unit>/api-<run>.json); 0.0 when there is none."""
    best = (-1, 0.0)
    for f in Path(od).glob("api-*.json"):
        k = f.name[4:-5]; d = run_json(f); c = d.get("total_cost_usd")
        if k == run or run_time(k) >= run_time(run) or (d.get("session_id") or d.get("thread_id")) != sid: continue
        if isinstance(c, (int, float)) and run_time(k) > best[0]: best = (run_time(k), float(c))
    return best[1]

def run_cost(path, budget, run=None):
    """(cost, reported) of an API run from its json output, else its whole budget (a run stopped before it wrote its cost is
    charged everything, so the cap never undercounts). The agent CLI reports the cost of the whole SESSION: a resumed run's
    total_cost_usd holds every run before it too. With `run` (the run id) the run is charged only its own part: the total minus
    what the session had reported before it (session_before). A total below the earlier one is a session that did not carry
    its total over: the run's own cost. A total EQUAL to the earlier one is the earlier run's json, still lying here while
    this run wrote nothing: not reported."""
    d = run_json(path); c = d.get("total_cost_usd")
    if not isinstance(c, (int, float)): return float(budget or 0), False
    sid = d.get("session_id") or d.get("thread_id")
    prior = session_before(Path(path).parent, run, sid) if run and sid else 0.0
    if prior and abs(c - prior) < 5e-7: return float(budget or 0), False
    return (round(float(c) - prior, 6) if 0 < prior < c else float(c)), True

def keep_run_json(cfg, od, run):
    """Keep the run's own json (out/<unit>/api-<run>.json, a copy of api.json) when it is closed here: the next run of the same
    session is charged against it (run_cost). A local run writes its own copy; a remote unit's first run has none."""
    src, dst = Path(od) / "api.json", Path(od) / f"api-{run}.json"
    if cfg.run.dry or not run or dst.exists(): return
    try: atomic_write(dst, src.read_text())
    except OSError: pass

def settle_run(cfg, u, rc):
    """Close an API unit's ledger run (its done row) when it ended without writing one: stopped, killed or closed."""
    mc = cfg.models.get(u.get("model"), {}); pv = cfg.prov.get(mc.get("provider"), {})
    if mc.get("kind") != "api" or not u.get("run") or "ledger" not in pv: return
    od = cfg.p("out", "out") / u["name"]
    cost, reported = run_cost(od / "api.json", u.get("budget") or pv.get("budget_per_unit", 0), u["run"])
    if reported: keep_run_json(cfg, od, u["run"])
    if ledger_done(cfg, pv, u["run"], u["name"], cost, rc): say(f"ledger: run {u['run']} closed at ${cost:.2f} ({rc})")

def recost_plan(cfg, pv):
    """[(run id, unit, charged, own cost)]: done rows that charge a resumed run the whole SESSION total its json reports (rows
    written before run_cost charged each run its own part). Per unit, its runs in time order: a row is corrected only when it
    holds exactly its json's total and an earlier run of the same session reported a smaller one; a run without a kept json
    passes its row on as the total so far (a remote unit's first run). Rows already corrected no longer equal their json's
    total, so the correction is done once."""
    runs = ledger_read(cfg, pv)[0]; by = {}; out = []
    for k, r in runs.items(): by.setdefault(r["unit"], []).append(k)
    for un, ks in by.items():
        if not any(re.search(r"\.r\d+\.\d+$", k) for k in ks): continue   # no resumed run: nothing to read
        od = cfg.p("out", "out") / un; prev, psid = None, None
        for k in sorted(ks, key=run_time):
            r = runs[k]; d = run_json(od / f"api-{k}.json"); c = d.get("total_cost_usd"); sid = d.get("session_id") or d.get("thread_id")
            if not isinstance(c, (int, float)):
                if r["done"] and prev is None and r.get("how") != RECONCILED: prev = r["cost"]
                continue
            if psid and sid != psid: prev = None   # another session: its total starts again
            if r["done"] and prev and abs(r["cost"] - c) < 6e-5 and prev < c - 5e-5: out.append((k, un, r["cost"], round(c - prev, 6)))
            prev, psid = float(c), sid
    return out

def row_time(s):
    """A ledger row's time stamp as epoch seconds (0 when it cannot be read)."""
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try: return time.mktime(time.strptime(str(s).strip(), f))
        except ValueError: pass
    return 0

def left_cost(cfg, runs, k):
    """(cost, file name) of what run `k` itself reported in the json it left, else (None, None): its kept json
    (out/<unit>/api-<run>.json), else the unit's api.json when that is this run's (the run is the unit's latest in the ledger and
    the file was written after the run started). Row 222: a run whose agent was gone was booked at its whole budget while its
    api.json, read only for a unit still open, held the cost."""
    r = runs[k]; od = cfg.p("out", "out") / r["unit"]
    cost, rep = run_cost(od / f"api-{k}.json", r["budget"], k)
    if rep: return cost, f"api-{k}.json"
    p = od / "api.json"
    try: mine = [x for x in runs if runs[x]["unit"] == r["unit"]][-1] == k and p.stat().st_mtime >= row_time(r["ts"])
    except OSError: mine = False
    cost, rep = run_cost(p, r["budget"], k) if mine else (None, False)
    return (cost, "api.json") if rep else (None, None)

def reconciled_plan(cfg, pv):
    """[(run id, unit, charged, reported cost, file)]: runs closed `done unknown` at their budget (the upper bound) whose own json
    reports what they cost. A run that left nothing keeps the bound."""
    runs = ledger_read(cfg, pv)[0]; out = []
    for k, r in runs.items():
        if not r["done"] or r.get("how") != RECONCILED: continue
        cost, src = left_cost(cfg, runs, k)
        if src and abs(cost - r["cost"]) >= 5e-7: out.append((k, r["unit"], r["cost"], cost, src))
    return out

def recost(cfg, pv, plan, found=()):
    """Rewrite the cost of the done rows in plan (recost_plan) under the ledger's lock, after a backup <ledger>.bak-<time>; the
    file is replaced by write-then-rename. `found` (reconciled_plan) rows get their reported cost and are marked
    `done rc=reconciled`: no longer counted at the bound. Returns the backup path."""
    p = ledger_path(cfg, pv); new = {k: own for k, _, _, own in plan}; mark = {k: c for k, _, _, c, _ in found}
    with Locked(p):
        raw = p.read_text(); keep = []
        for l in raw.splitlines():
            f = l.split("\t")
            if len(f) == 5 and f[4] in new and f[2] != "running" and f[3].startswith("done"): f[2] = str(new.pop(f[4])); l = "\t".join(f)
            elif len(f) == 5 and f[4] in mark and f[3] == RECONCILED: f[2], f[3] = str(mark.pop(f[4])), "done rc=reconciled"; l = "\t".join(f)
            keep.append(l)
        bak = p.with_name(p.name + time.strftime(".bak-%Y%m%d-%H%M%S")); bak.write_text(raw)
        atomic_write(p, "".join(x + "\n" for x in keep))
    return bak

def read_secret(pv):
    """(variable, value) of a provider's API key from the configured secret store, never printed or written anywhere:
    key_keychain (an item of the OS secret store: the macOS keychain, else secret-tool or pass; janus/platform.py), else key_file (lines VAR=value; refused
    unless only its owner can read it, mode 600)."""
    var = pv.get("key_env", "ANTHROPIC_API_KEY"); tried = []
    if pv.get("key_keychain"):
        r = secret_get(pv["key_keychain"])   # the OS secret store (keychain, secret-tool, pass) lives in janus/platform.py
        if r is not None:
            if r[0] == 0 and r[1].strip(): return var, r[1].strip()
            tried.append(f"secret store item {pv['key_keychain']} (not found)")
    if pv.get("key_file"):
        p = Path(os.path.expanduser(pv["key_file"]))
        try: st = p.stat()
        except OSError: tried.append(f"{p} (missing)")
        else:
            if st.st_mode & 0o077: raise Err(f"{p} can be read by others (mode {oct(st.st_mode & 0o777)}): chmod 600 {p}")
            for l in p.read_text().splitlines():
                k, _, v = l.strip().removeprefix("export ").partition("=")
                if k.strip() == var and v.strip(): return var, v.strip().strip("'\"")
            tried.append(f"{p} (no {var}=)")
    raise Err(f"no API key: " + ("; ".join(tried) or "set key_keychain (macOS) or key_file (mode 600) in the provider's janus.toml table"))

def model_ok(cfg, m):
    c = cfg.models.get(m)
    if not c: return False, "unknown model"
    pv = cfg.prov.get(c["provider"], {})
    if not pv.get("enabled"): return False, f"provider {c['provider']} disabled"
    r = cfg.route; tool = c.get("tool") or (c["kind"] if c.get("kind") in ("claude", "codex", "opencode") else {"anthropic": "claude", "openai": "codex"}.get(c.get("vendor"), c.get("kind")))
    auth = pv.get("auth") or ("api_key" if c.get("kind") == "api" else "subscription")
    if r.get("allow_tools") is not None and tool not in r["allow_tools"]: return False, f"tool {tool} is not in [routing] allow_tools"
    if r.get("allow_auth") is not None and auth not in r["allow_auth"]: return False, f"auth {auth} (provider {c['provider']}) is not in [routing] allow_auth"
    if "ledger" in pv:
        runs, bad, _ = ledger_read(cfg, pv)
        if bad: return False, f"{c['provider']} ledger malformed ({len(bad)} problem(s); janus ledger check)"
        if "cap_usd" in pv:
            left = pv["cap_usd"] - spend(runs)
            if left < pv.get("budget_per_unit", 0): return False, f"{c['provider']} budget left ${left:.0f}"
    return True, ""

RECONCILED = "done unknown"   # a reconciled run: its agent is gone with no done row; its budget is counted as the upper bound

__all__ = ["OVERSHOOT_PCT", "reserved", "budget_of", "ledger_path", "ledger_parse", "ledger_read", "spend", "committed", "ledger_append", "tstamp", "reserve_budget", "ledger_done", "run_json", "run_time", "session_before", "run_cost", "keep_run_json", "settle_run", "recost_plan", "reconciled_plan", "left_cost", "row_time", "recost", "read_secret", "model_ok", "RECONCILED"]
