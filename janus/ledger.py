"""The API ledger (janus owns it) and the secret store (section 7, Step 2).

parse/read/spend/committed/append, reserve_budget/ledger_done (one row per run under Locked; idempotent), run_cost,
settle_run, read_secret/model_ok. Every row is written once under the fcntl lock (section 9: keep the lock and per-run
idempotency). unreconciled() stays with its callers until the findings split (it reads budgets/park helpers)."""
from janus.context import NUM, Locked, Err, say, now
from janus.platform import secret_get
import json, os, time
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

def reserve_budget(cfg, mc, name, budget, run=None):
    """Under the ledger's lock: refuse when the ledger is malformed or the budget would pass the cap, else write the run's one
    running row. Idempotent: a run id that already has its running row is returned as it is. Returns the run id."""
    pv = cfg.prov[mc["provider"]]; run = run or f"{name}.{os.getpid()}.{int(now())}"
    if cfg.run.dry: return run
    with Locked(ledger_path(cfg, pv)):
        runs, bad, _ = ledger_read(cfg, pv)
        if bad: raise Err(f"refused: the {mc['provider']} ledger {pv['ledger']} is malformed ({bad[0]}; {len(bad)} problem(s)): run janus ledger check, then janus ledger repair")
        r = runs.get(run)
        if r and r["done"]: raise Err(f"refused: run {run} is already finished (done row at line {r['done'][0]}); a re-run gets a new run id")
        if r and r["running"]: return run
        if "cap_usd" in pv:
            c = spend(runs)
            if c + budget > pv["cap_usd"]: raise Err(f"refused: ${c:.2f} committed + ${budget} would pass the ${pv['cap_usd']} cap of {mc['provider']}")
        ledger_append(cfg, pv, [tstamp(), name, "running", budget, run])
    return run

def ledger_done(cfg, pv, run, name, cost, rc):
    """Write the run's one done row (idempotent: nothing when it already has one). Returns True when written."""
    if cfg.run.dry: return False
    with Locked(ledger_path(cfg, pv)):
        r = ledger_read(cfg, pv)[0].get(run)
        if r and r["done"]: return False
        ledger_append(cfg, pv, [tstamp(), name, cost, RECONCILED if rc == "unknown" else f"done rc={rc}", run])
    return True

def run_cost(path, budget):
    """The cost an API run reported in its json output, else its whole budget (a run stopped before it wrote its cost is
    charged everything, so the cap never undercounts)."""
    try: c = json.loads(Path(path).read_text()).get("total_cost_usd")
    except Exception: c = None
    return (float(c), True) if isinstance(c, (int, float)) else (float(budget or 0), False)

def settle_run(cfg, u, rc):
    """Close an API unit's ledger run (its done row) when it ended without writing one: stopped, killed or closed."""
    mc = cfg.models.get(u.get("model"), {}); pv = cfg.prov.get(mc.get("provider"), {})
    if mc.get("kind") != "api" or not u.get("run") or "ledger" not in pv: return
    cost, _ = run_cost(cfg.p("out", "out") / u["name"] / "api.json", u.get("budget") or pv.get("budget_per_unit", 0))
    if ledger_done(cfg, pv, u["run"], u["name"], cost, rc): say(f"ledger: run {u['run']} closed at ${cost:.2f} ({rc})")

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
    if "ledger" in pv:
        runs, bad, _ = ledger_read(cfg, pv)
        if bad: return False, f"{c['provider']} ledger malformed ({len(bad)} problem(s); janus ledger check)"
        if "cap_usd" in pv:
            left = pv["cap_usd"] - spend(runs)
            if left < pv.get("budget_per_unit", 0): return False, f"{c['provider']} budget left ${left:.0f}"
    return True, ""

RECONCILED = "done unknown"   # a reconciled run: its agent is gone with no done row; its budget is counted as the upper bound

__all__ = ["ledger_path", "ledger_parse", "ledger_read", "spend", "committed", "ledger_append", "tstamp", "reserve_budget", "ledger_done", "run_cost", "settle_run", "read_secret", "model_ok", "RECONCILED"]
