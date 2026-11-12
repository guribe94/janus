"""Budget follow-through: each run pays its own cost, a resume gets a budget it can use, open units keep what they need to
finish, capped units come first, and the unattended loop carries on by itself. Every test names the failure it catches.
Standard library only; no network, no API spend."""
import argparse, io, contextlib, json, os, re, subprocess, sys, time, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project
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

def transcript(home, cwd, sid, ctx, model="test-model", n=1):
    """A session transcript under a fake home: n assistant messages, the last one with a context of ctx tokens."""
    d = Path(home) / ".claude/projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(cwd)); d.mkdir(parents=True, exist_ok=True)
    L = [json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {"id": f"m{i}", "model": model, "usage": {
        "input_tokens": 0, "output_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 1000}}}) for i in range(n)]
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

if __name__ == "__main__": unittest.main()
