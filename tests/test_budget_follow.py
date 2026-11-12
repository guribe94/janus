"""Budget follow-through: each run pays its own cost, a resume gets a budget it can use, open units keep what they need to
finish, capped units come first, and the unattended loop carries on by itself. Every test names the failure it catches.
Standard library only; no network, no API spend."""
import argparse, io, contextlib, json, os, subprocess, sys, time, unittest
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

if __name__ == "__main__": unittest.main()
