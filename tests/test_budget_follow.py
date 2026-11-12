"""Budget follow-through: each run pays its own cost, a resume gets a budget it can use, open units keep what they need to
finish, capped units come first, and the unattended loop carries on by itself. Every test names the failure it catches.
Standard library only; no network, no API spend."""
import argparse, io, contextlib, json, os, re, subprocess, sys, time, unittest
from pathlib import Path
from unittest import mock

from test_janus import host_probe, janus, project
from janusload import JANUS_PY

def ns(**k): return argparse.Namespace(**k)

def said(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): rc = fn(*a)
    return rc, buf.getvalue()

def costs(d): return [x.split("\t")[2] for x in (d / "out/ledger.tsv").read_text().splitlines() if x.split("\t")[2] != "running"]

def session_json(d, unit, run, total, sid="S", **more):
    """A run's own json as the agent CLI writes it: total_cost_usd is the SESSION total, not the run's."""
    od = d / "out" / unit; od.mkdir(parents=True, exist_ok=True)
    (od / f"api-{run}.json").write_text(json.dumps({"total_cost_usd": total, "session_id": sid, **more}))

class Item1RunCost(unittest.TestCase):
    def test_a_resumed_run_is_charged_its_own_cost_not_the_session_total(self):
        """Catches (item 1): the agent CLI reports the SESSION's cost so far on every resume (1.9, then 3.3); the ledger charged
        each resume the whole total again (1.9 + 3.3 = 5.2 for a unit that cost 3.3), so the cap filled with money never spent."""
        d, cfg = project()
        fake = d / "fake.py"; cnt = d / "count"
        fake.write_text(f"import json\np = {str(cnt)!r}\ntry: n = int(open(p).read())\nexcept Exception: n = 0\nopen(p, 'w').write(str(n + 1))\n"
                        "print(json.dumps({'total_cost_usd': [1.9, 3.3][n], 'session_id': 'S'}))\n")
        t = (d / "janus.toml").read_text().replace('enabled = true\ncap_usd', f'enabled = true\ncmd = ["{sys.executable}", "{fake}", "{{prompt}}"]\n'
                                                   f'resume_cmd = ["{sys.executable}", "{fake}", "{{session}}", "{{prompt}}"]\ncap_usd', 1)
        (d / "janus.toml").write_text(t); k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n"); k.chmod(0o600)
        (d / "briefs/u.md").write_text("do nothing\n"); (d / "p.md").write_text("continue\n")
        env = {x: v for x, v in os.environ.items() if x not in ("ANTHROPIC_API_KEY", "JANUS_ROOT", "JANUS_UNIT", "JANUS_ROLE", "JANUS_REPORT")}
        base = [sys.executable, str(JANUS_PY), "api-run", "u", "--model", "m", "--budget", "4", "--brief", str(d / "briefs/u.md"), "--cwd", str(d)]
        for extra in (["--run", "u.1.100"], ["--run", "u.r2.200", "--resume-session", "S", "--prompt-file", str(d / "p.md")]):
            r = subprocess.run(base + extra, cwd=d, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([round(float(c), 4) for c in costs(d)], [1.9, 1.4], "the resume was charged the session total, not its own cost")
        u = {"name": "u", "model": "m", "runs": ["u.1.100", "u.r2.200"], "run": "u.r2.200", "budget": 4}
        self.assertAlmostEqual(janus.unit_spent(janus.Cfg(d), u), 3.3, places=4)

    def test_a_remote_resume_closed_from_its_saved_json_pays_its_own_cost(self):
        """Catches (item 1): a remote unit's runs are closed here from the saved api.json (stop, close, resume, collect); the
        second run's row took the session total again."""
        d, cfg = project(); pv = cfg.prov["api"]; mc = cfg.models["m"]; od = d / "out/u"; od.mkdir()
        u = {"name": "u", "model": "m", "budget": 4, "run": janus.reserve_budget(cfg, mc, "u", 2, "u.1.100")}
        (od / "api.json").write_text(json.dumps({"total_cost_usd": 1.9, "session_id": "S"}))
        said(janus.settle_run, cfg, u, "stopped")
        u["run"] = janus.reserve_budget(cfg, mc, "u", 2, "u.r2.200")
        (od / "api.json").write_text(json.dumps({"total_cost_usd": 3.3, "session_id": "S"}))
        said(janus.settle_run, cfg, u, "stopped")
        self.assertEqual([round(float(c), 4) for c in costs(d)], [1.9, 1.4])
        # the json of the run before, still here while the new run has written nothing: never read as a run that cost $0
        u["run"] = janus.reserve_budget(cfg, mc, "u", 2, "u.r3.300")
        said(janus.settle_run, cfg, u, "stopped")
        self.assertEqual(round(float(costs(d)[-1]), 4), 4.0, "a stale json was read as this run's cost")

    def test_ledger_recost_corrects_old_rows_once_and_shows_them_first(self):
        """Catches (item 1): rows written before the fix keep charging each resume the session total; nothing corrected them."""
        d, cfg = project(); pv = cfg.prov["api"]; mc = cfg.models["m"]
        for run, tot in (("u.1.100", 0.9), ("u.r2.200", 1.3), ("u.r3.300", 2.2)):
            janus.reserve_budget(cfg, mc, "u", 2, run); janus.ledger_done(cfg, pv, run, "u", tot, 0); session_json(d, "u", run, tot)
        janus.reserve_budget(cfg, mc, "v", 2, "v.1.100"); janus.ledger_done(cfg, pv, "v.1.100", "v", 0.7, 0); session_json(d, "v", "v.1.100", 0.7, sid="T")
        self.assertAlmostEqual(janus.committed(cfg, pv), 5.1)
        self.assertIn("ledger:api:recost", [k for k, _ in janus.ledger_findings(cfg, [])])
        before = (d / "out/ledger.tsv").read_text()
        cfg.run.dry = True; rc, out = said(janus.cmd_ledger, cfg, ns(action="recost", provider=None)); cfg.run.dry = False
        self.assertIn("u.r2.200", out); self.assertIn("$2.20 charged twice", out)
        self.assertEqual((d / "out/ledger.tsv").read_text(), before, "the dry run changed the ledger")
        with mock.patch.object(janus, "refuse_agent", lambda *a, **k: None): rc, out = said(janus.cmd_ledger, cfg, ns(action="recost", provider=None))
        self.assertAlmostEqual(janus.committed(cfg, pv), 2.9)                       # 0.9 + 0.4 + 0.9 + 0.7
        self.assertEqual(len(list((d / "out").glob("ledger.tsv.bak-*"))), 1)
        self.assertEqual(janus.ledger_read(cfg, pv)[1], [])
        self.assertNotIn("ledger:api:recost", [k for k, _ in janus.ledger_findings(cfg, [])])
        with mock.patch.object(janus, "refuse_agent", lambda *a, **k: None): rc, out = said(janus.cmd_ledger, cfg, ns(action="recost", provider=None))
        self.assertIn("nothing to correct", out); self.assertAlmostEqual(janus.committed(cfg, pv), 2.9)

def transcript(home, cwd, sid, ctx, model="test-model", n=1, first=None):
    """A session transcript under a fake home: n assistant messages, the last one with a context of ctx tokens (first: the
    text of its first prompt)."""
    d = Path(home) / ".claude/projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(cwd)); d.mkdir(parents=True, exist_ok=True)
    L = [json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {"id": f"m{i}", "model": model, "usage": {
        "input_tokens": 0, "output_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 1000}}}) for i in range(n)]
    if first: L.insert(0, json.dumps({"type": "user", "message": {"content": first}}))
    (d / f"{sid}.jsonl").write_text("\n".join(L) + "\n")

def rates(d, per_token=4e-6, model="test-model"):
    """A finished run's json the rate is learned from: per_token dollars for one input token of this model."""
    od = d / "out/old"; od.mkdir(parents=True, exist_ok=True)
    (od / "api.json").write_text(json.dumps({"total_cost_usd": per_token * 1e6, "modelUsage": {model: {"inputTokens": 1000000, "costUSD": per_token * 1e6}}}))

class FakeHome:
    """HOME is a temp folder while the test runs: transcripts are read from there, never from the real one."""
    def __init__(self, d): self.h = d / "home"; self.h.mkdir(exist_ok=True)
    def __enter__(self): self.old = os.environ.get("HOME"); os.environ["HOME"] = str(self.h); return self.h
    def __exit__(self, *a):
        if self.old is None: os.environ.pop("HOME", None)
        else: os.environ["HOME"] = self.old

def api_unit(d, name="u1", budget=4.0, host="here", **more):
    b = d / f"briefs/{name}.md"; b.write_text(f"# Brief: {name}\n")
    return {"id": "t-1", "name": name, "host": host, "via": "pueue", "model": "m", "task": 5, "budget": budget, "brief": str(b), "rbrief": str(b),
            "rcwd": str(d), "cwd": str(d), "started": 0, "kind": "implement", "tier": "normal", **more}

def resume_stubs(got):
    """resume_unit with nothing started: the queued task, the board and the task log are faked; the ledger is real."""
    return [mock.patch.object(janus, "session_of", lambda cfg, u: "sid"), mock.patch.object(janus, "X", lambda cfg, h, cmd, **k: got.setdefault("cmds", []).append(cmd) or (0, "7", "")),
            mock.patch.object(janus, "confirm_started", lambda cfg, h, tid: ("running", "")), mock.patch.object(janus, "save_task_log", lambda *a, **k: None),
            mock.patch.object(janus, "board", lambda *a, **k: (0, "", ""))]

def run_resume(cfg, u, msg="go", **k):
    got = {}
    with contextlib.ExitStack() as st:
        for m in resume_stubs(got): st.enter_context(m)
        got["line"] = janus.resume_unit(cfg, u, msg, **k)
    return got

def held(d, cfg): return {k: janus.budget_of(cfg, r["budget"]) for k, r in janus.ledger_read(cfg, cfg.prov["api"])[0].items() if not r["done"]}

class Item2ResumeFloor(unittest.TestCase):
    """The agent CLI stops a resumed run when THAT RUN's spend reaches --max-budget-usd (not the session's), and the first
    request of a resume pays for loading the whole context again. A cap under that cost buys nothing."""
    def setUp(self):
        self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]; rates(self.d)
        self.u = api_unit(self.d, run=janus.reserve_budget(self.cfg, self.mc, "u1", 4, "u1.1.100"), runs=["u1.1.100"])
        janus.ledger_done(self.cfg, self.pv, "u1.1.100", "u1", 3.6, 0)                   # $0.40 of its $4 left

    def test_a_resume_that_cannot_pay_for_its_context_is_refused_not_wasted(self):
        """Catches (item 2): a unit with $0.40 left was resumed with a $0.40 cap; loading its 150k-token context cost $0.75, so
        the run stopped at its first turn having done nothing, and the money was gone."""
        with FakeHome(self.d) as home:
            transcript(home, self.d, "sid", 150000)                                      # 150000 x 1.25 x $4e-6 = $0.75 to reload
            with self.assertRaises(janus.Err) as e: run_resume(self.cfg, self.u)
        self.assertIn("$0.75", str(e.exception)); self.assertIn("--budget", str(e.exception))
        self.assertEqual(held(self.d, self.cfg), {}, "a run was reserved for a resume that could do nothing")

    def test_a_resume_with_enough_left_gets_what_is_left(self):
        """Catches (item 2): the floor must not shrink or refuse a resume that can pay for its context."""
        with FakeHome(self.d) as home:
            transcript(home, self.d, "sid", 50000)                                       # $0.25 to reload, $0.40 left
            got = run_resume(self.cfg, self.u)
        self.assertEqual(list(held(self.d, self.cfg).values()), [0.4])

    def test_the_wrap_up_run_gets_its_budget_on_top_of_the_reload(self):
        """Catches (item 2): the wrap-up resume of a capped run got a flat $0.50; with a $0.75 context it stopped at turn 1 and
        the report was never written (seen live: three such runs of one unit, $0.50 each, nothing done)."""
        with FakeHome(self.d) as home:
            transcript(home, self.d, "sid", 150000)
            run_resume(self.cfg, self.u, wrapup=True)
        self.assertEqual(list(held(self.d, self.cfg).values()), [1.25])                 # $0.75 reload + [budget] wrapup_usd 0.5

    def test_a_remote_units_context_is_read_on_its_host(self):
        """Catches (item 2): a remote unit's transcript is not here, so its resume had no floor at all."""
        self.cfg.hosts["far"] = {"ssh": "far", "root": "/srv/p", "_rd": "/srv/p/runs"}; u = {**self.u, "host": "far", "rcwd": "/srv/p/w/u1"}
        (self.d / "out/u1").mkdir(exist_ok=True); (self.d / "out/u1/api.json").write_text(json.dumps({"session_id": "sid-0001"}))
        calls = []
        def hrun(h, script, **k): calls.append((script, k.get("inp"))); return 0, json.dumps({"ctx": 150000, "model": "test-model", "use": {}}) + "\n", ""
        with mock.patch.object(janus, "hrun", hrun): use = janus.session_usage(self.cfg, u)
        self.assertEqual(use["ctx"], 150000); self.assertAlmostEqual(janus.reload_usd(self.cfg, use), 0.75)
        self.assertIn("python3 -", calls[0][0]); self.assertIn("sid-0001.jsonl", calls[0][0]); self.assertIn("def usage_scan", calls[0][1])

class Item3RemoteSettle(unittest.TestCase):
    """A remote unit's run that ended on its host is closed in the ledger when its output is brought here, not at collect."""
    def setUp(self):
        self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]
        self.cfg.hosts["far"] = {"ssh": "far", "root": "/srv/p", "_rd": "/srv/p/runs"}
        self.u = api_unit(self.d, host="far", state="done", run=janus.reserve_budget(self.cfg, self.mc, "u1", 2, "u1.1.100"), runs=["u1.1.100"])
        self.bd = type("B", (), {"save": lambda s, u: None})()

    def saved(self, text):
        def save(cfg, u):
            od = self.d / "out/u1"; od.mkdir(exist_ok=True); (od / "api.json").write_text(text)
            (od / "REPORT.md").write_text("VERDICT: PENDING\nstep 1 done, exit 0\nVERDICT: DONE - exit 0\n"); return True
        return [mock.patch.object(janus, "save_output", save), mock.patch.object(janus, "remote_patch", lambda *a, **k: None)]

    def test_check_closes_an_ended_remote_run_at_its_own_cost(self):
        """Catches (item 3): a remote unit that finished for $0.60 kept its whole reservation ($2.40) in the ledger until someone
        ran janus collect; with several such units the cap read as reached and launches were refused."""
        with contextlib.ExitStack() as st:
            for m in self.saved(json.dumps({"total_cost_usd": 0.6, "session_id": "S"})): st.enter_context(m)
            janus.sync_remote_outputs(self.cfg, [self.u], self.bd)
        self.assertAlmostEqual(janus.committed(self.cfg, self.pv), 0.6)
        self.assertEqual(janus.verdict(self.cfg, self.u), "VERDICT: DONE - exit 0")

    def test_the_watcher_closes_it_too(self):
        """Catches (item 3): the watcher saved the ended remote unit's output and left its run open at the reservation."""
        with contextlib.ExitStack() as st:
            for m in self.saved(json.dumps({"total_cost_usd": 0.6, "session_id": "S"})): st.enter_context(m)
            janus.refresh_remote(self.cfg, self.u, self.bd, lambda m: None)
        self.assertAlmostEqual(janus.committed(self.cfg, self.pv), 0.6)

    def test_a_run_that_reported_no_cost_stays_held(self):
        """Catches (item 3): closing a run whose json is empty (killed) at $0 would undercount the cap."""
        with contextlib.ExitStack() as st:
            for m in self.saved(""): st.enter_context(m)
            janus.sync_remote_outputs(self.cfg, [self.u], self.bd)
        self.assertAlmostEqual(janus.committed(self.cfg, self.pv), 2.4)

    def test_reconcile_charges_the_cost_the_run_reported(self):
        """Catches (item 3): janus ledger reconcile charged a gone run its whole reservation although its saved json says what it
        cost."""
        od = self.d / "out/u1"; od.mkdir(); (od / "api.json").write_text(json.dumps({"total_cost_usd": 0.6, "session_id": "S"}))
        beads = type("B", (), {"__init__": lambda s, cfg: None, "units": lambda s: [dict(self.u)]})
        with mock.patch.object(janus, "Beads", beads), mock.patch.object(janus, "probe_all", lambda cfg: {}), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "unit_state", lambda u, hs, ag: "gone"), mock.patch.object(janus, "all_units", lambda cfg: {"u1": self.u}), \
             mock.patch.object(janus, "refuse_agent", lambda *a, **k: None):
            rc, out = said(janus.cmd_ledger, self.cfg, ns(action="reconcile", provider=None))
        self.assertAlmostEqual(janus.committed(self.cfg, self.pv), 0.6, msg=out)

class FakeBeads:
    """No beads database in a temp project: the open units are the list given."""
    units_ = []
    def __init__(self, cfg): pass
    def units(self): return [dict(u) for u in FakeBeads.units_]
    def create(self, title, *a): return title
    def run(self, *a): raise janus.Err("no beads")
    def save(self, u): pass
    def comment(self, *a): pass

class Item4FinishReserve(unittest.TestCase):
    """New work is admitted against the cap MINUS what every open unit still needs to finish."""
    def setUp(self):
        self.d, self.cfg = project('\n[backlog]\nfile = "S.md"\n[routing]\nnormal = ["m"]\ncritical = ["m"]\n'); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]
        self.units = []                                   # two open $4 units between runs: a used $1.00, b used $0.50
        for n, c in (("a", 1.0), ("b", 0.5)):
            run = janus.reserve_budget(self.cfg, self.mc, n, 4, f"{n}.1.100"); janus.ledger_done(self.cfg, self.pv, run, n, c, 0)
            self.units.append(api_unit(self.d, n, run=run, runs=[run], state="parked"))
        FakeBeads.units_ = self.units; self.real = janus.Beads; janus.Beads = FakeBeads
    def tearDown(self): janus.Beads = self.real

    def launch(self, name, budget):
        (self.d / f"briefs/{name}.md").write_text(f"# Brief: {name}\n")
        hs = {"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "100", "load": "0"}}
        a = ns(unit=name, kind="implement", tier="normal", host="here", model="m", brief=None, cwd=str(self.d / "tree-c"), budget=budget, test_host=None,
               allow_main=True, of=None, report=None, resume_of=None, item=None, probes=hs)
        self.cfg.run.dry = True
        try:
            with mock.patch.object(janus, "herdr", lambda *a, **k: None), mock.patch.object(janus, "read_secret", lambda pv: ("K", "x")), \
                 mock.patch.object(janus, "choose", lambda *a, **k: ("here", "here", "test")): return said(janus.cmd_launch, self.cfg, a)
        finally: self.cfg.run.dry = False

    def test_a_new_unit_is_refused_when_the_open_units_could_no_longer_finish(self):
        """Catches (item 4): with $1.50 spent of a $10 cap a $3 unit was launched although the two open units still needed $6.50
        of their budgets: the cap was then spent down by new work and no open unit could finish."""
        with self.assertRaises(janus.Err) as e: self.launch("c", 3)       # 1.50 + 7.80 kept + 3.60 > 10
        self.assertIn("$7.80 kept for 2 open unit(s) to finish", str(e.exception)); self.assertIn("ask the owner", str(e.exception))
        self.launch("c", 0.5)                                             # 1.50 + 7.80 + 0.60 fits: launched as before
        (self.d / "out/b").mkdir(); (self.d / "out/b/REPORT.md").write_text("VERDICT: DONE - exit 0\n")
        self.launch("c", 3)                                               # b finished: it needs nothing more; 1.50 + 3.60 + 3.60 fits

    def test_an_open_unit_resumes_on_what_is_kept_for_it(self):
        """Catches (item 4): the reserve must never refuse the resume it is kept for."""
        self.pv["cap_usd"] = 9.5                                          # 1.50 + 7.80 kept: no room for new work
        with self.assertRaises(janus.Err): self.launch("c", 1)
        with self.assertRaises(janus.Err) as e: run_resume(self.cfg, self.units[0], budget=3.5)   # a NEW budget would take b's money
        self.assertIn("$4.20 kept for 1 open unit(s) to finish (b $4.20)", str(e.exception))
        run_resume(self.cfg, self.units[0])                               # its own $3.00 is there
        self.assertEqual(list(held(self.d, self.cfg).values()), [3.0])

    def test_fill_plans_only_what_leaves_the_open_units_their_money(self):
        """Catches (item 4): fill launched every ready unit the bare cap allowed and left nothing for the open units' resumes."""
        (self.d / "S.md").write_text("| # | item | status |\n|---|---|---|\n| X1 | one | READY |\n| X2 | two | READY |\n| X3 | three | READY |\n")
        for n, it in (("x1-a", "X1"), ("x2-a", "X2"), ("x3-a", "X3")): (self.d / f"briefs/{n}.md").write_text(f"# {n}\nITEM: {it}\n")
        self.cfg.hosts["here"].update(ram_gb=64, cores=16); hs = {"here": host_probe(name="here")}
        self.pv["cap_usd"] = 12; plan, stops = janus.fill_plan(self.cfg, hs, [])
        self.assertEqual(len(plan), 3, stops)                             # no open unit: all three fit the cap
        plan, stops = janus.fill_plan(self.cfg, hs, self.units)           # $2.70 is free after the $7.80 kept: two $1.20 holds fit, not three
        self.assertEqual(len(plan), 2, stops); self.assertIn("$7.80 kept for 2 open unit(s) to finish", " ".join(map(str, stops)))

    def test_a_finding_asks_the_owner_before_the_cap_is_reached(self):
        """Catches (item 4): nothing was said until committed spend passed 80 % of the cap; by then the open units' remaining
        budgets no longer fitted."""
        F = dict(janus.reserve_findings(self.cfg, self.units))            # $1.50 committed is 15 % of the cap; $0.70 is free
        self.assertIn("ask the owner", F.get("spend:api:reserve", ""), F)
        self.pv["cap_usd"] = 9.0                                          # the open units need more than is left
        self.assertIn("$0.30 short", dict(janus.reserve_findings(self.cfg, self.units)).get("spend:api:short", ""))
        self.pv["cap_usd"] = 12
        self.assertEqual(janus.reserve_findings(self.cfg, self.units), [])
        for k in ("spend:api:reserve", "spend:api:short"): janus.finding(k, "x")   # both keys are of a registered family

class Item5UnitHistory(unittest.TestCase):
    """A new unit's budget is learned from what past UNITS cost to finish: every run of each, the resumes included."""
    def setUp(self):
        self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]; self.pv["cap_usd"] = 100; self.au = {}; self.cfg.run.all_units = self.au
    def unit(self, name, costs, verdict="VERDICT: DONE - exit 0\n", t0=100):
        ids = [f"{name}.{t0}.{t0}"] + [f"{name}.r{k}.{t0 + k}" for k in range(2, len(costs) + 1)]
        for run, c in zip(ids, costs): janus.reserve_budget(self.cfg, self.mc, name, 2, run); janus.ledger_done(self.cfg, self.pv, run, name, c, 0)
        (self.d / "out" / name).mkdir(exist_ok=True); (self.d / "out" / name / "REPORT.md").write_text(verdict)
        self.au[name] = {"name": name, "kind": "implement", "tier": "normal", "model": "m", "run": ids[-1], "runs": ids, "status": "closed"}

    def test_a_resumed_units_runs_are_one_sample(self):
        """Catches (item 5): three units that each needed $3.50 over three runs (a $2.00 run, then resumes of $1.00 and $0.50,
        each paying to load the context again) taught a budget of $1.50 -- the median of the nine RUNS -- so the next unit was
        capped and resumed the same way."""
        for n in ("u1", "u2", "u3"): self.unit(n, (2.0, 1.0, 0.5))
        self.assertEqual(janus.cost_history(self.cfg, "api"), {("implement", "normal"): [3.5, 3.5, 3.5]})
        b, why = janus.budget_for(self.cfg, self.mc, "implement", "normal")
        self.assertEqual(b, 5.0, why); self.assertIn("median $3.50 of 3 past implement/normal unit(s)", why); self.assertIn("resume", why)

    def test_a_name_launched_again_is_a_unit_of_its_own(self):
        """Catches (item 5): summing by name alone would add an earlier unit of the same name to the later one."""
        self.unit("u1", (0.6,)); self.unit("u1", (1.0, 0.5), t0=500)
        self.assertEqual(janus.cost_history(self.cfg, "api"), {("implement", "normal"): [0.6, 1.5]})

class Item6Checkpoint(unittest.TestCase):
    """Every API run is told its budget and what the unit has used, and is ordered to checkpoint before the cap stops it."""
    def setUp(self):
        self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]; rates(self.d, 4e-5)
        self.cfg.hosts["far"] = {"ssh": "far", "root": "/srv/p", "_rd": "/srv/p/runs"}
        self.u = api_unit(self.d, state="working", run=janus.reserve_budget(self.cfg, self.mc, "u1", 4, "u1.1.100"), runs=["u1.1.100"])
        self.posts = []; self.log = []
    def tend(self, u):
        def board(cfg, *a, **k): self.posts.append(a); return 0, "", ""
        with mock.patch.object(janus, "board", board), mock.patch.object(janus.Beads, "comment", lambda *a: None): janus.tend_checkpoint(self.cfg, {}, [u], self.log.append)
        return [a for a in self.posts if a[0] == "direct"]

    def test_the_first_prompt_states_the_budget_and_the_checkpoint_rule(self):
        """Catches (item 6): the agent was never told its budget, so a run stopped at its cap mid-step with uncommitted work and
        a report that did not say where it was."""
        got = {}
        def argv(pv, mc, budget, prompt, resume=None): got["prompt"] = prompt; return ["true"]
        a = ns(unit="u1", run=self.u["run"], model="m", budget=4.0, brief=self.u["brief"], cwd=str(self.d), report=None, prompt_file=None, resume_session=None)
        with mock.patch.object(janus, "api_argv", argv), mock.patch.object(janus, "read_secret", lambda pv: ("K", "x")), \
             mock.patch.object(janus, "board", lambda *a, **k: (0, "", "")): said(janus.cmd_api_run, self.cfg, a)
        for want in ("stopped without warning once it has spent $4.00", "commit your work", "budget checkpoint"): self.assertIn(want, got["prompt"])
        with mock.patch.object(janus, "board", lambda *a, **k: (0, "", "")): p = janus.agent_prompt(self.cfg, "u1", "/b.md", "far", "/r.md", budget=2.5)
        self.assertIn("$2.50", p)                                         # the prompt a remote unit is launched with

    def test_a_resumed_run_is_told_this_runs_budget_and_what_the_unit_used(self):
        """Catches (item 6): a resumed agent was not told what this run may spend, nor that part of it pays for its context."""
        janus.ledger_done(self.cfg, self.pv, "u1.1.100", "u1", 1.0, 0)
        with FakeHome(self.d) as home:
            transcript(home, self.d, "sid", 5000); run_resume(self.cfg, self.u)
        text = (self.d / "out/u1/resume-2.md").read_text()
        self.assertIn("stopped without warning once it has spent $3.00", text); self.assertIn("used $1.00 of its $4", text)
        self.assertIn("loading your context again", text)

    def test_a_run_near_its_budget_is_ordered_to_checkpoint_once(self):
        """Catches (item 6): nothing told a running agent that its run was at 85 % of its budget; it was stopped at the cap."""
        with FakeHome(self.d) as home:
            transcript(home, self.d, "S", 36000, n=5, first=f"Read {self.u['brief']}")           # $1.70 of $4: nothing yet
            self.assertEqual(self.tend(self.u), [])
            transcript(home, self.d, "S", 36000, n=10, first=f"Read {self.u['brief']}")          # $3.40 of $4: 85 %
            d = self.tend(self.u)
            self.assertEqual(len(d), 1, self.log); self.assertEqual(d[0][1], "u1")
            for want in ("$3.40 of its $4", "commit", "report", "park"): self.assertIn(want, d[0][2])
            self.assertEqual(len(self.tend(self.u)), 1, "the same run was ordered twice")

    def test_a_remote_run_is_measured_on_its_host(self):
        """Catches (item 6): a remote unit's spend could not be told here ('remote'), so it was never warned."""
        u = {**self.u, "host": "far", "rcwd": "/srv/p/w/u1"}; calls = []
        use = {"ctx": 36000, "model": "test-model", "use": {"test-model": {"input_tokens": 0, "output_tokens": 10000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 400000}}}
        def hrun(h, script, **k): calls.append(script); return 0, json.dumps(use) + "\n", ""
        with mock.patch.object(janus, "hrun", hrun): d = self.tend(u)
        self.assertEqual(len(d), 1, self.log); self.assertIn("$3.60 of its $4", d[0][2])   # (50000 + 40000) weighted tokens at $4e-5
        self.assertIn(" 100", calls[0].splitlines()[-1])                  # only this run's messages (since its start)
        self.assertIn("ls -t", calls[0])                                  # a first run has no session id here yet: the tree's newest transcript

if __name__ == "__main__": unittest.main()
