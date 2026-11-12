"""Checks for orch.py, run before every release (./check, and install runs them against the copy it installs).
ORCH_PY names the orch.py under test (default: the one next to tests/). Standard library only; no network, no API spend."""
import importlib.util, json, os, stat, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

ORCH_PY = Path(os.environ.get("ORCH_PY") or Path(__file__).resolve().parent.parent / "orch.py")
spec = importlib.util.spec_from_file_location("orch", ORCH_PY); orch = importlib.util.module_from_spec(spec); spec.loader.exec_module(orch)

def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="orch-test-"))
    (d / "orch.toml").write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        [providers.api]
        enabled = true
        cap_usd = 10
        budget_per_unit = 1
        ledger = "out/ledger.tsv"
        key_file = "KEYFILE"
        [models.m]
        provider = "api"
        kind = "api"
        api_id = "test-model"
        ''').replace("KEYFILE", str(d / "key.env")) + extra)
    (d / "out").mkdir(); (d / "briefs").mkdir()
    return d, orch.Cfg(d)

class Ledger(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]
    def rows(self): return (self.d / "out/ledger.tsv").read_text().splitlines()

    def test_one_running_one_done_idempotent(self):
        run = orch.reserve_budget(self.cfg, self.mc, "u1", 2)
        self.assertEqual(orch.reserve_budget(self.cfg, self.mc, "u1", 2, run), run)   # again: no second running row
        self.assertTrue(orch.ledger_done(self.cfg, self.pv, run, "u1", 0.5, 0))
        self.assertFalse(orch.ledger_done(self.cfg, self.pv, run, "u1", 0.5, 0))      # again: no second done row
        self.assertEqual(len(self.rows()), 2)
        self.assertAlmostEqual(orch.committed(self.cfg, self.pv), 0.5)
        with self.assertRaises(orch.Err): orch.reserve_budget(self.cfg, self.mc, "u1", 2, run)   # a finished run never re-runs

    def test_cap_per_run_id(self):
        run = orch.reserve_budget(self.cfg, self.mc, "u1", 2)
        for _ in range(3): orch.ledger_append(self.cfg, self.pv, ["2026-01-01 00:00:00", "u1", "3", "done rc=0", run])   # a looping writer
        runs, bad, _ = orch.ledger_read(self.cfg, self.pv)
        self.assertEqual(orch.spend(runs), 3.0)    # counted once per run id, not 9
        self.assertTrue(bad)                        # but the ledger is malformed ...
        with self.assertRaises(orch.Err) as e: orch.reserve_budget(self.cfg, self.mc, "u2", 1)   # ... so launches are refused
        self.assertIn("malformed", str(e.exception))
        self.assertFalse(orch.model_ok(self.cfg, "m")[0])

    def test_cap_refuses(self):
        orch.reserve_budget(self.cfg, self.mc, "u1", 9)
        with self.assertRaises(orch.Err): orch.reserve_budget(self.cfg, self.mc, "u2", 2)

    def test_repair(self):
        run = orch.reserve_budget(self.cfg, self.mc, "u1", 2)
        for _ in range(4): orch.ledger_append(self.cfg, self.pv, ["2026-01-01 00:00:00", "u1", "1", "done rc=0", run])
        orch.ledger_append(self.cfg, self.pv, ["garbage"])
        a = type("A", (), {"action": "check", "provider": None})()
        self.assertEqual(orch.cmd_ledger(self.cfg, a), 1)
        a.action = "repair"; self.assertEqual(orch.cmd_ledger(self.cfg, a), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(list((self.d / "out").glob("ledger.tsv.bak-*")))
        a.action = "check"; self.assertEqual(orch.cmd_ledger(self.cfg, a), 0)

class Secret(unittest.TestCase):
    def test_key_file_mode(self):
        d, cfg = project(); k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n")
        k.chmod(0o644)
        with self.assertRaises(orch.Err): orch.read_secret(cfg.prov["api"])
        k.chmod(0o600)
        self.assertEqual(orch.read_secret(cfg.prov["api"]), ("ANTHROPIC_API_KEY", "abc123"))

class ApiRun(unittest.TestCase):
    def test_fake_agent(self):
        """orch api-run with a fake agent CLI: the key reaches only the agent, the ledger gets one running and one done row."""
        d, cfg = project('\n[providers.api.x]\n')
        fake = d / "fake.py"
        fake.write_text("import json, os, sys\nk = os.environ.get('ANTHROPIC_API_KEY')\n"
                        "print(json.dumps({'total_cost_usd': 0.25, 'key_ok': k == 'abc123', 'argv_has_key': any('abc123' in a for a in sys.argv)}))\n")
        t = (d / "orch.toml").read_text().replace("[providers.api.x]\n", "").replace('enabled = true\ncap_usd', f'enabled = true\ncmd = ["{sys.executable}", "{fake}", "{{prompt}}"]\ncap_usd', 1)
        (d / "orch.toml").write_text(t); k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n"); k.chmod(0o600)
        (d / "briefs/u.md").write_text("do nothing\n")
        env = {x: v for x, v in os.environ.items() if x not in ("ANTHROPIC_API_KEY", "ORCH_ROOT", "ORCH_UNIT", "ORCH_ROLE", "ORCH_REPORT")}
        r = subprocess.run([sys.executable, str(ORCH_PY), "api-run", "u", "--run", "u.1.1", "--model", "m", "--budget", "1",
                            "--brief", str(d / "briefs/u.md"), "--cwd", str(d)], cwd=d, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads((d / "out/u/api.json").read_text())
        self.assertTrue(out["key_ok"]); self.assertFalse(out["argv_has_key"])
        rows = (d / "out/ledger.tsv").read_text().splitlines()
        self.assertEqual([x.split("\t")[2] for x in rows], ["running", "0.25"])
        r = subprocess.run([sys.executable, str(ORCH_PY), "api-run", "u", "--run", "u.1.1", "--model", "m", "--budget", "1",
                            "--brief", str(d / "briefs/u.md"), "--cwd", str(d)], cwd=d, env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)   # the same run id again: refused, nothing written
        self.assertEqual(len((d / "out/ledger.tsv").read_text().splitlines()), 2)
        self.assertNotIn("abc123", r.stdout + r.stderr + (d / "out/ledger.tsv").read_text())

class Hooks(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project()
    def j(self, c): return orch.judge(self.cfg, c, str(self.d), orch.edit_ok_dirs(self.cfg))
    def test_data_is_not_commands(self):
        for c in ['git commit -m "close the worktrees while loop"', "git commit -m 'for each worktree, kill it'",
                  'git commit -m "$(cat <<\'EOF\'\nfix: while loop over worktrees; orch close --force; kill 1\nEOF\n)"',
                  'git commit -F - <<EOF\nrm -rf / ; kill 1; for x in worktrees; do git add; done\nEOF', 'echo "kill -9 1"',
                  'bd comment x-1 "kill the loop; rm -rf it"', 'git -C /x/worktrees/a commit -m "while"',
                  'ssh host pueue status', "ssh -o BatchMode=yes h 'pueue status --json' | jq .", 'ssh h pueue --color never status',
                  'ssh h "pueue group; pueue log 3"', 'for h in a b; do ssh $h pueue status; done']:
            self.assertIsNone(self.j(c), c)
    def test_commands_are_judged(self):
        for c in ['for d in worktrees/*; do git -C $d add -A; done', 'kill 5', 'ls | xargs rm -rf', 'bash -c "kill 3"',
                  'ssh h pueue kill 3', 'ssh h pueue parallel -g jobs 4', 'echo "$(kill 1)"', 'git push --force',
                  'orch api-run u --run x', 'cat <<EOF | bash\nkill 1\nEOF']:
            self.assertIsNotNone(self.j(c), c)

class Backlog(unittest.TestCase):
    def test_table_and_checkboxes(self):
        d, cfg = project('\n[backlog]\nfile = "S.md"\nready = ["READY", "FIX"]\n')
        (d / "S.md").write_text("# S\n| # | item | status | next |\n|---|---|---|---|\n| A1 | one | DONE | - |\n| A2 | two | FIX | x |\n"
                                "| A3 | three | NOT STARTED | y |\n## more\n- [ ] four things\n- [x] five\n")
        it = {i: (st, dn, rd) for i, t, st, dn, rd in orch.backlog_items(cfg)}
        self.assertEqual(it["A1"], ("DONE", True, False)); self.assertEqual(it["A2"], ("FIX", False, True))
        self.assertEqual(it["A3"], ("NOT STARTED", False, False)); self.assertEqual(it["four-things"][1:], (False, False))

class Resume(unittest.TestCase):
    def test_report_path(self):
        d, cfg = project(); (d / "out/orig").mkdir(); rep = d / "out/orig/REPORT.md"; rep.write_text("VERDICT: BLOCKED\nold\nVERDICT: BLOCKED\n")
        b = d / "briefs/r.md"; b.write_text(f"Continue the same report {rep} (line 1 PENDING).\n")
        a = type("A", (), {"report": None, "resume_of": None, "unit": "r"})()
        self.assertEqual(orch.resume_report(cfg, a, b), rep.resolve())
        a.resume_of = "orig"; b.write_text("x\n"); self.assertEqual(orch.resume_report(cfg, a, b), rep.resolve())
        u = {"name": "r", "report": str(rep), "resume": True, "started": time.time() + 5}
        self.assertIsNone(orch.verdict(cfg, u))   # the old verdict does not count
        u["started"] = time.time() - 5; rep.write_text("VERDICT: DONE\nnew\nVERDICT: DONE\n")
        self.assertEqual(orch.verdict(cfg, u), "VERDICT: DONE")

class SelfSafety(unittest.TestCase):
    def test_in_place_edit_flagged_rename_not(self):
        d = Path(tempfile.mkdtemp()); s = d / "loop.sh"; s.write_text("#!/bin/bash\nsleep 30\necho done\n")
        p = subprocess.Popen(["/bin/bash", str(s)]); time.sleep(1.5)
        try:
            orch.atomic_write(s, "#!/bin/bash\nsleep 30\necho other\n", 0o755); time.sleep(1.1)
            self.assertFalse([k for k, _ in orch.script_findings() if k.startswith(f"script:{p.pid}:")])   # renamed: safe
        finally: p.kill(); p.wait()
        p = subprocess.Popen(["/bin/bash", str(s)]); time.sleep(1.5)
        try:
            with open(s, "r+") as f: f.seek(0, 2); f.write("echo appended\n")   # in place
            time.sleep(1.1)
            self.assertTrue([k for k, _ in orch.script_findings() if k.startswith(f"script:{p.pid}:")])
        finally: p.kill(); p.wait()

class Generic(unittest.TestCase):
    def test_no_project_details_in_code(self):
        import re
        bad = re.compile(r"collection-rig|slurp|asgard|midgard|fable|vegtam|/Users/|/srv/|d435|orch-anthropic-api|claude-(opus|sonnet|haiku)", re.I)
        here = ORCH_PY.parent
        for f in [ORCH_PY, here / "install", *sorted((here / "skill").glob("*")), here / "example.orch.toml"]:
            if f.is_file():
                hits = [f"{f.name}:{i}" for i, l in enumerate(f.read_text().splitlines(), 1) if bad.search(l)]
                self.assertEqual(hits, [], f"project details in {f}")

if __name__ == "__main__": unittest.main()
