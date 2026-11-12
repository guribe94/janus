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

class Item9ResumeFigures(unittest.TestCase):
    """What a resumed unit is told it spent is what it spent; a result line under a park line is not a job to wait for."""
    def setUp(self):
        self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]
        self.u = api_unit(self.d, state="done", run=janus.reserve_budget(self.cfg, self.mc, "u1", 4, "u1.1.100"), runs=["u1.1.100"])
        (self.d / "out/u1").mkdir()

    def test_the_resume_message_states_the_ended_runs_real_cost(self):
        """Catches (review F10): the message a resumed unit reads was built while its ended run was still open in the ledger, so
        the run counted at its budget: a unit that had spent $1.20 was told 'spent $4.00 of $4' and wrote 'budget used up'."""
        m = janus.resume_message(self.cfg, {}, self.u, [])                # the run's json is not here: its cost is not known
        self.assertNotIn("$4.00 of", m); self.assertIn("not known yet", m)
        (self.d / "out/u1/api.json").write_text(json.dumps({"total_cost_usd": 1.2, "session_id": "S"}))
        m = janus.resume_message(self.cfg, {}, self.u, [])
        self.assertNotIn("$4.00 of", m); self.assertIn("used $1.20 of its $4", m)
        with FakeHome(self.d): run_resume(self.cfg, self.u, m)            # and the run it starts is told the same figure
        text = (self.d / "out/u1/resume-2.md").read_text()
        self.assertNotIn("$4.00 of", text); self.assertEqual(text.count("used $1.20 of its $4"), 2)

    REPORT = ("VERDICT: PENDING\nstep 2 done\nWAITING here:6 here:7\nStep 3 results (jobs finished):\n- here:6 exit 0: all green\n"
              "- here:3 exit 0: run1..run10=0\nStep 4: next I start the last build.\n")

    def test_a_result_line_under_a_park_line_is_not_a_job_to_wait_for(self):
        """Catches (review F1): every later line naming a job was read as part of the park list, so a result line (`- here:3
        exit 0: ...`) under an old park line parked the unit on a job it never waited for."""
        self.assertEqual(janus.park_jobs(self.cfg, self.REPORT)[1], ["here:6", "here:7"])
        for text, want in (("WAITING\n- here:9\n- here:10\nresult of here:3: exit 0\n", ["here:9", "here:10"]),     # a list of ids still parks on all
                           ("WAITING on jobs:\n1. `here:9`, here:10 and here:11\n", ["here:9", "here:10", "here:11"]),
                           ("WAITING for nothing.\n\nJobs and exit codes:\n- here:9 exit 0\n", [])):
            self.assertEqual(janus.park_jobs(self.cfg, text)[1], want, repr(text))

    def test_a_capped_run_with_results_under_its_park_line_is_wrapped_up(self):
        """Catches (review F1, the consequence): resumed for its jobs, the run wrote their results and ended at its budget cap;
        the result lines read as a new park, so the unit was held 'parked' and the wrap-up run never came."""
        (self.d / "out/u1/REPORT.md").write_text(self.REPORT); (self.d / "out/u1/api.json").write_text(json.dumps({"total_cost_usd": 4.0, "session_id": "S", "subtype": "error_max_budget_usd"}))
        u = {**self.u, "handled_jobs": ["here:6", "here:7"]}; u.pop("state")
        hs = {"here": {"ok": True, "res": [], "hist": [], "sched": [], "direct": [], "cap_pause": "", "cap_admit": "yes", "tmux": [],
                       "pueue": {"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}}}}
        janus.unit_findings(self.cfg, hs, [u], {})
        self.assertNotEqual(u.get("state"), "parked", u.get("_park")); self.assertTrue(janus.wrap_due(self.cfg, u))

class Item7Resumable(unittest.TestCase):
    """A unit stopped by its budget is recorded resumable, its money is kept, and it is resumed before any new work."""
    def setUp(self):
        self.d, self.cfg = project('\n[autopilot]\nenabled = true\n[backlog]\nfile = "S.md"\n[routing]\nnormal = ["m"]\ncritical = ["m"]\n')
        self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]; rates(self.d)
        for run, c in (("u1.1.100", 2.0), ("u1.r2.200", 0.3)):            # a $2 run stopped at its cap, then its wrap-up run
            janus.reserve_budget(self.cfg, self.mc, "u1", 2, run); janus.ledger_done(self.cfg, self.pv, run, "u1", c, 0)
        self.u = api_unit(self.d, budget=2.0, state="done", verdict="VERDICT: BLOCKED - stopped at the budget", run="u1.r2.200", runs=["u1.1.100", "u1.r2.200"], wrapup="u1.r2.200")
        (self.d / "out/u1").mkdir(); (self.d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nstep 1 done\nVERDICT: BLOCKED - stopped at the budget\n")
        (self.d / "out/u1/api.json").write_text(json.dumps({"total_cost_usd": 2.3, "session_id": "sid", "subtype": "success"}))
        FakeBeads.units_ = [self.u]; self.real = janus.Beads; janus.Beads = FakeBeads; self.log = []; self.got = {}
        self.home = FakeHome(self.d); transcript(self.home.__enter__(), self.d, "sid", 100000)      # loading it again: $0.50
    def tearDown(self): janus.Beads = self.real; self.home.__exit__()

    def tend(self, *units):
        with contextlib.ExitStack() as st:
            for m in resume_stubs(self.got): st.enter_context(m)
            janus.tend_resumable(self.cfg, {}, list(units or [self.u]), self.log.append)
        return [c for c in self.got.get("cmds", []) if "--resume-session" in c]

    def test_a_capped_unit_is_recorded_resumable_and_its_money_is_kept(self):
        """Catches (item 7): a unit stopped at its budget was only reported (waste:<unit>); nothing recorded that it could go on,
        and new units took the cap's last money, so it never could."""
        self.pv["cap_usd"] = 4.0                                          # $2.30 committed: no room for the $1.50 it needs ($1.80 held)
        self.assertEqual(self.tend(), [], self.log)
        self.assertEqual(self.u["resumable"]["need"], 1.5)                # a new unit's $1 plus $0.50 to load its context again
        self.assertEqual(janus.finish_reserve(self.cfg, "api", [self.u]), (1.8, {"u1": 1.8}))
        F = dict(janus.resumable_findings(self.cfg, [self.u]))
        self.assertIn("resumed first", F.get("unit:u1:resumable", ""), F); janus.finding("unit:u1:resumable", "x")
        self.pv["cap_usd"] = 5.0                                          # room for u1 OR one new $1 unit: u1 comes first
        (self.d / "S.md").write_text("| # | item | status |\n|---|---|---|\n| X1 | one | READY |\n")
        (self.d / "briefs/x1-a.md").write_text("# x1\nITEM: X1\n"); self.cfg.hosts["here"].update(ram_gb=64, cores=16)
        plan, stops = janus.fill_plan(self.cfg, {"here": host_probe(name="here")}, [self.u])
        self.assertEqual(plan, [], "a new unit was planned on the money a resumable unit waits for"); self.assertIn("kept for 1 open unit(s)", str(stops))

    def test_it_is_resumed_in_its_own_session_when_budget_is_freed_or_added(self):
        """Catches (item 7): after the cap was raised (or other runs ended under their budgets) a capped unit stayed stopped
        until the orchestrator relaunched it by hand, as a new agent without its context."""
        self.pv["cap_usd"] = 4.0; self.tend(); self.assertEqual(self.tend(), [])
        self.pv["cap_usd"] = 5.0                                          # budget added
        cmds = self.tend()
        self.assertEqual(len(cmds), 1, self.log); self.assertIn("--resume-session sid", cmds[0]); self.assertIn("--budget 1.5", cmds[0])
        self.assertNotIn("resumable", self.u); self.assertEqual(self.u["topups"], 1); self.assertEqual(self.u["budget"], 3.5)
        self.assertEqual(list(held(self.d, self.cfg).values()), [1.5]); self.assertNotIn("wrapup", self.u)   # a later cap is wrapped up again
        self.assertIn("continue", (self.d / "out/u1/resume-3.md").read_text())

    def test_the_oldest_resumable_unit_goes_first_and_the_resumes_are_bounded(self):
        """Catches (item 7): two resumable units each kept the other from the cap's room (neither resumed), and a unit that is
        capped every time must not be given new money without end."""
        for run, c in (("u2.1.300", 2.0), ("u2.r2.400", 0.3)): janus.reserve_budget(self.cfg, self.mc, "u2", 2, run); janus.ledger_done(self.cfg, self.pv, run, "u2", c, 0)
        u2 = {**self.u, "name": "u2", "run": "u2.r2.400", "runs": ["u2.1.300", "u2.r2.400"], "wrapup": "u2.r2.400"}
        (self.d / "out/u2").mkdir(); (self.d / "out/u2/REPORT.md").write_text("VERDICT: BLOCKED - stopped at the budget\n")
        (self.d / "out/u2/api.json").write_text(json.dumps({"total_cost_usd": 2.3, "session_id": "sid", "subtype": "success"}))
        self.pv["cap_usd"] = 6.5; self.u["resumable"] = {"why": "capped", "need": 1.5, "at": 5}; u2["resumable"] = {"why": "capped", "need": 1.5, "at": 9}
        cmds = self.tend(u2, self.u)                                      # $4.60 committed: room for one $1.80 hold, not two
        self.assertEqual(len(cmds), 1, self.log); self.assertIn("u1.r3.", cmds[0]); self.assertIn("resumable", u2)
        u2["topups"] = 2; self.pv["cap_usd"] = 20
        self.assertEqual(len(self.tend(u2)), 1, "a unit already resumed [autopilot] relaunch_max times got new money again")   # still u1's one
        self.assertNotIn("resumable", u2); self.assertIn("unit:u2:resume-limit", dict(janus.resumable_findings(self.cfg, [u2])))

    def test_a_parked_unit_whose_budget_cannot_pay_its_resume_is_resumed_with_a_new_one(self):
        """Catches (item 7): a parked unit whose jobs had finished but whose budget could no longer pay for loading its context
        was refused the resume on every watcher pass, for ever."""
        u = {**self.u, "verdict": None, "_park": ["here:5"], "state": "parked", "parked": {"jobs": ["here:5"]}}; u.pop("wrapup")
        (self.d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nWAITING here:5\n")
        hs = {"here": {"ok": True, "pueue": {"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}}}}
        with contextlib.ExitStack() as st:
            for m in resume_stubs(self.got): st.enter_context(m)
            st.enter_context(mock.patch.object(janus, "refresh_remote", lambda *a, **k: None)); st.enter_context(mock.patch.object(janus, "job_result", lambda *a: (0, "/log")))
            janus.tend_parked(self.cfg, hs, [u], self.log.append)
            self.assertEqual(u.get("resumable", {}).get("need"), 1.5, self.log)   # $2.30 of $2 used: nothing left for the reload
            janus.tend_resumable(self.cfg, hs, [u], self.log.append)
        text = (self.d / "out/u1/resume-3.md").read_text()
        self.assertIn("Your jobs finished", text); self.assertIn("here:5: exit 0", text); self.assertEqual(u.get("handled_jobs"), ["here:5"])

    def test_without_the_unattended_loop_it_is_recorded_and_left_to_the_orchestrator(self):
        """Catches (item 7): with the loop off the orchestrator must be told the unit is resumable and how."""
        self.cfg.c["autopilot"]["enabled"] = False
        self.assertEqual(self.tend(), []); self.assertEqual(self.u["resumable"]["need"], 1.5)
        self.assertIn("janus resume u1 --budget 1.5", dict(janus.resumable_findings(self.cfg, [self.u])).get("unit:u1:resumable", ""))

class Item8Unattended(unittest.TestCase):
    """With a spending cap the watcher keeps work moving on its own (relaunch, resume, fill), says so when that is off, and one
    failing step never stops the rest of a pass."""
    STEPS = ("heartbeat_beat", "board_alerts", "tend_parked", "tend_checkpoint", "tend_resumable", "tend_autopilot", "tend_thrash", "tend_tidy")
    def setUp(self):
        self.real = janus.Beads; janus.Beads = FakeBeads; FakeBeads.units_ = []; self.log = []
    def tearDown(self): janus.Beads = self.real; FakeBeads.units_ = []

    def crashed(self, d): return {"name": "u1", "id": "b-1", "host": "here", "state": "done", "via": "pueue", "model": "m", "brief": "b.md", "cwd": str(d), "run": "u1.1"}
    def relaunched(self, cfg, d):
        got = []
        with mock.patch.object(janus, "cmd_launch", lambda cfg, a: got.append(a.unit)), mock.patch.object(janus, "board", lambda c, *a, **k: (0, "", "")), \
             mock.patch.object(janus, "board_questions", lambda c: []):
            janus.tend_autopilot(cfg, {}, [self.crashed(d)], [], self.log.append)
        return got
    def watch(self, cfg, F=(), **more):
        """One watcher pass with every step stubbed (`more` replaces some); returns the names of the steps that ran."""
        ran = []
        stubs = {n: (lambda *a, _n=n, **k: ran.append(_n) or []) for n in self.STEPS + ("orphan_jobs", "cmd_fill")}
        stubs.update(run_check=lambda cfg, cycle=False, writer=True: ([janus.finding(k, "t") for k in F], None, {}, []), herdr=lambda *a: None, **more)
        with mock.patch.multiple(sys.modules["janus.watch"], **stubs): janus.watch_pass(cfg, self.log.append)
        return ran

    def test_with_a_cap_the_watcher_relaunches_and_fills_without_being_configured(self):
        """Catches (item 8): a project with a cap and no [autopilot]/[fill] section left a crashed unit dead and ready backlog
        units unlaunched all night: the unattended loop was off unless the owner had found both settings."""
        d, cfg = project()
        self.assertEqual(self.relaunched(cfg, d), ["u1-r1"], "a crashed unit was not relaunched in a project with a cap")
        self.assertIn("cmd_fill", self.watch(cfg, F=["idle-with-backlog"]), "ready backlog units were not launched in a project with a cap")
        self.assertEqual(janus.unattended_findings(cfg, [self.crashed(d)]), [])

    def test_the_owner_can_turn_it_off_and_check_says_that_it_is_off(self):
        """Catches (item 8): the loop forced on against an explicit setting; and a loop that is off with nobody told, so the
        orchestrator leaves for the night believing the watcher will keep work moving."""
        d, cfg = project("\n[autopilot]\nenabled = false\n[fill]\nwatcher = false\n[backlog]\nfile = \"S.md\"\n")
        self.assertEqual(self.relaunched(cfg, d), []); self.assertNotIn("cmd_fill", self.watch(cfg, F=["idle-with-backlog"]))
        F = dict(janus.unattended_findings(cfg, [self.crashed(d)]))
        self.assertEqual(list(F), ["unattended:off"]); self.assertIn("[autopilot] enabled", F["unattended:off"]); self.assertIn("[fill] watcher", F["unattended:off"])
        self.assertFalse(janus.finding("unattended:off", F["unattended:off"]).urgent)   # registered: check can report it
        d, cfg = project("\n[fill]\nwatcher = false\n")   # the autopilot on by the cap; a fill with no backlog file does nothing
        self.assertEqual(janus.unattended_findings(cfg, [self.crashed(d)]), [])

    def test_without_a_cap_it_stays_off_and_check_says_so_while_work_is_open(self):
        """Catches (item 8): the watcher launching and relaunching agents in a project with no spending cap; and that project's
        orchestrator not told that nothing recovers its units while it is away."""
        d, cfg = project(); del cfg.prov["api"]["cap_usd"]
        self.assertEqual(self.relaunched(cfg, d), []); self.assertNotIn("cmd_fill", self.watch(cfg, F=["idle-with-backlog"]))
        F = dict(janus.unattended_findings(cfg, [self.crashed(d)]))
        self.assertEqual(list(F), ["unattended:off"]); self.assertIn("cap_usd", F["unattended:off"])
        self.assertEqual(janus.unattended_findings(cfg, []), [], "reported with no open unit and no backlog: nothing to keep moving")
        cfg.c["autopilot"] = {"enabled": True}
        self.assertEqual(self.relaunched(cfg, d), ["u1-r1"]); self.assertEqual(janus.unattended_findings(cfg, [self.crashed(d)]), [])

    def test_a_failing_step_is_logged_and_the_rest_of_the_pass_still_runs(self):
        """Catches (item 8): one watcher step that raised (a unit record it could not read) ended the whole pass, every pass: no
        resume, no relaunch, no fill and no orchestrator wake until someone looked at the log."""
        d, cfg = project()
        def bad(*a, **k): raise KeyError("host")
        for i in range(3):
            ran = self.watch(cfg, F=["idle-with-backlog"], tend_parked=bad); os.utime(cfg.f("last_fill"), (time.time() - 7200,) * 2)
        self.assertEqual([n for n in self.STEPS + ("cmd_fill",) if n not in ran], ["tend_parked"], "steps after the failing one did not run")
        self.assertTrue(any("tend_parked" in l and "KeyError" in l for l in self.log), self.log)
        F = dict(janus.watch_step_findings(cfg)); self.assertEqual(list(F), ["watcher:step:tend_parked"]); self.assertIn("3 passes", F["watcher:step:tend_parked"])
        self.assertIn("tend_parked", self.watch(cfg)); self.assertEqual(janus.watch_step_findings(cfg), [], "still reported after the step worked again")

    def test_a_pass_that_dies_is_logged_and_the_watcher_command_still_ends_cleanly(self):
        """Catches (item 8): an unexpected error in a pass (not a janus refusal) ended the watcher command with a traceback and
        nothing in watch.log, so nobody could see why no pass had acted."""
        d, cfg = project()
        def dies(cfg, log): raise ValueError("bad state file")
        with mock.patch.object(sys.modules["janus.watch"], "watch_pass", dies): janus.cmd_watch(cfg, ns())
        self.assertIn("ValueError: bad state file", cfg.f("watch.log").read_text())
        self.assertEqual([k for k, _ in janus.watch_step_findings(cfg)], [], "one failed pass is not yet a finding")

if __name__ == "__main__": unittest.main()
