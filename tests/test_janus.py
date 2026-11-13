"""Checks for janus.py, run before every release (./check, and install runs them against the copy it installs).
JANUS_PY names the janus.py under test (default: the one next to tests/). Standard library only; no network, no API spend."""
import contextlib, io, json, os, re, stat, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

from janusload import JANUS_PY, load

janus = load()
# Every fixture (temp project, script, daemon) lives under one root named with janus's fixture mark, so a live janus check running
# meanwhile skips the processes started here (fixes10 item 6); JANUS_IN_TESTS lets the suite itself see them.
import atexit, shutil
FIX_ROOT = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-", dir="/tmp")); tempfile.tempdir = str(FIX_ROOT)
os.environ["JANUS_IN_TESTS"] = "1"
atexit.register(shutil.rmtree, FIX_ROOT, True)

def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    (d / "janus.toml").write_text(textwrap.dedent('''
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
    return d, janus.Cfg(d)

class Ledger(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]
    def rows(self): return (self.d / "out/ledger.tsv").read_text().splitlines()

    def test_one_running_one_done_idempotent(self):
        run = janus.reserve_budget(self.cfg, self.mc, "u1", 2)
        self.assertEqual(janus.reserve_budget(self.cfg, self.mc, "u1", 2, run), run)   # again: no second running row
        self.assertTrue(janus.ledger_done(self.cfg, self.pv, run, "u1", 0.5, 0))
        self.assertFalse(janus.ledger_done(self.cfg, self.pv, run, "u1", 0.5, 0))      # again: no second done row
        self.assertEqual(len(self.rows()), 2)
        self.assertAlmostEqual(janus.committed(self.cfg, self.pv), 0.5)
        with self.assertRaises(janus.Err): janus.reserve_budget(self.cfg, self.mc, "u1", 2, run)   # a finished run never re-runs

    def test_cap_per_run_id(self):
        run = janus.reserve_budget(self.cfg, self.mc, "u1", 2)
        for _ in range(3): janus.ledger_append(self.cfg, self.pv, ["2026-01-01 00:00:00", "u1", "3", "done rc=0", run])   # a looping writer
        runs, bad, _ = janus.ledger_read(self.cfg, self.pv)
        self.assertEqual(janus.spend(runs), 3.0)    # counted once per run id, not 9
        self.assertTrue(bad)                        # but the ledger is malformed ...
        with self.assertRaises(janus.Err) as e: janus.reserve_budget(self.cfg, self.mc, "u2", 1)   # ... so launches are refused
        self.assertIn("malformed", str(e.exception))
        self.assertFalse(janus.model_ok(self.cfg, "m")[0])

    def test_cap_refuses(self):
        janus.reserve_budget(self.cfg, self.mc, "u1", 7)   # $8.40 held with the overshoot margin
        with self.assertRaises(janus.Err): janus.reserve_budget(self.cfg, self.mc, "u2", 2)

    def test_repair(self):
        run = janus.reserve_budget(self.cfg, self.mc, "u1", 2)
        for _ in range(4): janus.ledger_append(self.cfg, self.pv, ["2026-01-01 00:00:00", "u1", "1", "done rc=0", run])
        janus.ledger_append(self.cfg, self.pv, ["garbage"])
        a = type("A", (), {"action": "check", "provider": None})()
        self.assertEqual(janus.cmd_ledger(self.cfg, a), 1)
        a.action = "repair"; self.assertEqual(janus.cmd_ledger(self.cfg, a), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(list((self.d / "out").glob("ledger.tsv.bak-*")))
        a.action = "check"; self.assertEqual(janus.cmd_ledger(self.cfg, a), 0)

class Secret(unittest.TestCase):
    def test_key_file_mode(self):
        d, cfg = project(); k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n")
        k.chmod(0o644)
        with self.assertRaises(janus.Err): janus.read_secret(cfg.prov["api"])
        k.chmod(0o600)
        self.assertEqual(janus.read_secret(cfg.prov["api"]), ("ANTHROPIC_API_KEY", "abc123"))

class ApiRun(unittest.TestCase):
    def test_fake_agent(self):
        """janus api-run with a fake agent CLI: the key reaches only the agent, the ledger gets one running and one done row."""
        d, cfg = project('\n[providers.api.x]\n')
        fake = d / "fake.py"
        fake.write_text("import json, os, sys\nk = os.environ.get('ANTHROPIC_API_KEY')\n"
                        "print(json.dumps({'total_cost_usd': 0.25, 'key_ok': k == 'abc123', 'argv_has_key': any('abc123' in a for a in sys.argv)}))\n")
        t = (d / "janus.toml").read_text().replace("[providers.api.x]\n", "").replace('enabled = true\ncap_usd', f'enabled = true\ncmd = ["{sys.executable}", "{fake}", "{{prompt}}"]\ncap_usd', 1)
        (d / "janus.toml").write_text(t); k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n"); k.chmod(0o600)
        (d / "briefs/u.md").write_text("do nothing\n")
        env = {x: v for x, v in os.environ.items() if x not in ("ANTHROPIC_API_KEY", "JANUS_ROOT", "JANUS_UNIT", "JANUS_ROLE", "JANUS_REPORT")}
        r = subprocess.run([sys.executable, str(JANUS_PY), "api-run", "u", "--run", "u.1.1", "--model", "m", "--budget", "1",
                            "--brief", str(d / "briefs/u.md"), "--cwd", str(d)], cwd=d, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads((d / "out/u/api.json").read_text())
        self.assertTrue(out["key_ok"]); self.assertFalse(out["argv_has_key"])
        rows = (d / "out/ledger.tsv").read_text().splitlines()
        self.assertEqual([x.split("\t")[2] for x in rows], ["running", "0.25"])
        r = subprocess.run([sys.executable, str(JANUS_PY), "api-run", "u", "--run", "u.1.1", "--model", "m", "--budget", "1",
                            "--brief", str(d / "briefs/u.md"), "--cwd", str(d)], cwd=d, env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)   # the same run id again: refused, nothing written
        self.assertEqual(len((d / "out/ledger.tsv").read_text().splitlines()), 2)
        self.assertNotIn("abc123", r.stdout + r.stderr + (d / "out/ledger.tsv").read_text())

class Hooks(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project()
    def j(self, c): return janus.judge(self.cfg, c, str(self.d), janus.edit_ok_dirs(self.cfg))
    def test_data_is_not_commands(self):
        for c in ['git commit -m "close the worktrees while loop"', "git commit -m 'for each worktree, kill it'",
                  'git commit -m "$(cat <<\'EOF\'\nfix: while loop over worktrees; janus close --force; kill 1\nEOF\n)"',
                  'git commit -F - <<EOF\nrm -rf / ; kill 1; for x in worktrees; do git add; done\nEOF', 'echo "kill -9 1"',
                  'bd comment x-1 "kill the loop; rm -rf it"', 'git -C /x/worktrees/a commit -m "while"',
                  'ssh host pueue status', "ssh -o BatchMode=yes h 'pueue status --json' | jq .", 'ssh h pueue --color never status',
                  'ssh h "pueue group; pueue log 3"', 'for h in a b; do ssh $h pueue status; done',
                  'rg -n "rm -rf|kill -9" .', "grep -c 'pueue kill' log"]:   # a search pattern is data too
            self.assertIsNone(self.j(c), c)
    def test_commands_are_judged(self):
        for c in ['for d in worktrees/*; do git -C $d add -A; done', 'kill 5', 'ls | xargs rm -rf', 'bash -c "kill 3"',
                  'ssh h pueue kill 3', 'ssh h pueue parallel -g jobs 4', 'echo "$(kill 1)"', 'git push --force',
                  'janus api-run u --run x', 'cat <<EOF | bash\nkill 1\nEOF']:
            self.assertIsNotNone(self.j(c), c)
    def test_plan_files_are_writable(self):
        """Plan mode writes ~/.claude/plans/<name>.md: the hook blocked it (2026-11-11); other files stay denied."""
        w = lambda fp: janus.pretool_why(self.cfg, {"tool_name": "Write", "tool_input": {"file_path": fp}, "cwd": str(self.d)})
        self.assertIsNone(w(str(Path.home() / ".claude/plans/next-steps.md")))
        self.assertIsNotNone(w(str(Path.home() / ".claude/settings.json"))); self.assertIsNotNone(w(str(self.d / "src.py")))

class AgentStop(unittest.TestCase):
    """F1/F6: an agent that obeys the park rule is not told it is unfinished, and a block can never loop forever. The failure
    caught: an agent that wrote 'WAITING <job ids>' and ended its turn (per PARK_RULE) was blocked with 'Not finished' and its
    only exit was to poll (or to write a false DONE)."""
    def setUp(self):
        self.d, self.cfg = project()
        self.rp = self.d / "out/u/REPORT.md"; self.rp.parent.mkdir(parents=True, exist_ok=True)
    def stop(self, text, active=False):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        self.rp.write_text(text); buf = io.StringIO()
        with mock.patch.dict(os.environ, {"JANUS_ROLE": "agent", "JANUS_REPORT": str(self.rp)}), \
             mock.patch("sys.stdin", io.StringIO(json.dumps({"stop_hook_active": active}))):
            with redirect_stdout(buf): janus.cmd_hook("Stop")
        return buf.getvalue()
    def test_parked_report_is_not_blocked(self):
        self.assertNotIn("Not finished", self.stop("VERDICT: PENDING\ndoing x\nWAITING here:7\n"))
        self.assertNotIn("Not finished", self.stop("VERDICT: PENDING\ndoing x\nWAITING here:7\nmoved on\nVERDICT: PENDING\nWAITING here:8\n"))
    def test_a_block_does_not_loop(self):
        t = "VERDICT: PENDING\nnothing done yet\n"
        self.assertIn("Not finished", self.stop(t))                     # first stop: blocked
        msg = self.stop(t, active=True)
        self.assertIn("Not finished", msg)                              # the block re-fires (repeat one)
        for w in ("'VERDICT: DONE'", "'VERDICT: BLOCKED'", "'VERDICT: FAILED'"): self.assertIn(w, msg)   # names all three
        self.assertNotIn("Not finished", self.stop(t, active=True))     # repeat two: give up, the block never loops
    def test_finished_report_stops(self):
        self.assertNotIn("Not finished", self.stop("VERDICT: DONE\nran the suite: exit code 0\n"))
        # a final verdict after a line-1 PENDING (janus reads the last VERDICT line) is finished too
        self.assertNotIn("Not finished", self.stop("VERDICT: PENDING\nwork\nVERDICT: BLOCKED\nno commands run: exit codes none\n"))


class UserPrompt(unittest.TestCase):
    """F3: an owner instruction is filed to one store per audience — beads memories for the orchestrator's own rules, MEMORY.md
    for what agents must follow. The failure caught: the old text told the orchestrator to copy the same fact into both stores,
    and a rule filed with `bd remember` never reached an agent."""
    def setUp(self):
        self.d, self.cfg = project()
        self.cfg.f("session").write_text(f"pending:{int(time.time())}")
    def prompt(self, text):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        created = []
        class FakeBeads:
            def __init__(self, cfg): pass
            def create(self, title, kind, body): created.append((title, kind)); return "i-7"
        saved = (janus.Beads, janus.session_root); janus.Beads = FakeBeads; janus.session_root = lambda sid: None
        try:
            with mock.patch.dict(os.environ, {"JANUS_ROLE": ""}), \
                 mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "sid", "cwd": str(self.d), "prompt": text}))):
                buf = io.StringIO()
                with redirect_stdout(buf): janus.cmd_hook("UserPromptSubmit")
        finally: janus.Beads, janus.session_root = saved
        return created, buf.getvalue()
    def test_the_two_stores_are_named_and_not_copied(self):
        created, out = self.prompt("Make the gate use the new runner")
        self.assertEqual(created, [("instr: Make the gate use the new runner", "instruction")])
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bd remember", text)              # the orchestrator's own rules (beads)
        self.assertIn("janus remember", text)           # what agents must follow (MEMORY.md)
        self.assertNotIn("also janus remember", text)   # the old copy-the-same-fact-into-both instruction


class PromptFixes(unittest.TestCase):
    """F7/F8/F9/F13/F15 and the board direction: the wrap-up and resume texts, the refusal's named alternative, the wake's
    reason, read only showing new directions, and the header's absolute report path (for the opencode launch path, too)."""
    def setUp(self): self.d, self.cfg = project()
    def test_resume_message_names_the_report_path(self):
        u = {"name": "u", "host": "here", "budget": 2.0}
        m = janus.resume_message(self.cfg, {}, u, [])
        self.assertIn(str(self.cfg.p("out", "out") / "u/REPORT.md"), m)
        self.assertIn("Read only the logs you need", m); self.assertIn("WAITING <job ids>", m)
    def test_a_refusal_names_the_command_that_does_the_job(self):
        w = lambda c: janus.pretool_why(self.cfg, {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(self.d)})
        self.assertEqual(janus.deny_alt(w("pueue kill 3")), "janus cancel <host>:<task> (see them with janus queue)")
        self.assertEqual(janus.deny_alt(w("kill 5")), "janus stop <unit>")
        self.assertEqual(janus.deny_alt(w("git branch -D x")), "janus tidy --dry-run, then janus tidy")
        self.assertEqual(janus.deny_alt(w("rm -rf build")), "janus tidy --dry-run, then janus tidy")
        self.assertEqual(janus.deny_alt(w("janus api-run u --run x")), "janus launch")
    def test_the_wake_says_why_and_reads_handoff_only_after_clear(self):
        w = janus.wake(self.cfg, "late: here:3, here:4")
        self.assertIn("late: here:3, here:4", w); self.assertNotIn("HANDOFF.md", w)   # no full read on every wake
        self.assertIn(str(self.cfg.root), w)                                         # item 33: the root, by absolute path
        self.assertIn("cycle overdue", janus.wake(self.cfg, "cycle overdue"))
        self.assertIn(str(self.cfg.f("HANDOFF.md")), janus.wake_clear(self.cfg, "the context was cleared"))
    def test_read_shows_only_new_directions(self):
        bd = self.d / "board"; bd.mkdir(); (bd / ".master").write_text("here\n")
        B = lambda *a: janus.board(self.cfg, *a)
        B("post", "a1", "status", "start")
        B("direct", "a1", "first"); B("direct", "a1", "second")
        o1 = B("read", "a1")[1]; self.assertIn("second", o1); self.assertIn("first", o1)
        o2 = B("read", "a1")[1]; self.assertNotIn("second", o2); self.assertNotIn("first", o2)
        B("direct", "a1", "third")
        o3 = B("read", "a1")[1]; self.assertIn("third", o3); self.assertNotIn("second", o3)
    def test_the_opencode_launch_writes_the_absolute_report_path_in_the_header(self):
        d, cfg = project('\n[providers.oc]\nenabled = true\n[models.oc]\nprovider = "oc"\nkind = "opencode"\nmodel = "z"\n')
        (d / "briefs/u.md").write_text("TIER: NORMAL\nSCOPE: all\nWrite REPORT.md.\nPROOF: x\n")
        hs = {"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "100", "load": "0"}}
        class FakeBeads:
            def __init__(self, cfg): pass
            def units(self): return []
            def create(self, *a): return "u-1"
            def run(self, *a): return {"issues": []}
            def save(self, u): pass
            def comment(self, *a): pass
        saved = (janus.Beads, janus.herdr)
        janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None   # new_tab finds no tab and raises, after the header is written
        try:
            a = type("A", (), {"unit": "u", "kind": "implement", "tier": "normal", "host": "here", "model": "oc", "brief": None,
                               "cwd": str(d), "budget": None, "test_host": None, "allow_main": True, "of": None, "report": None,
                               "resume_of": None, "item": None, "probes": hs, "share_with": None, "dry_run": False})()
            with self.assertRaises(janus.Err): janus.cmd_launch(cfg, a)
        finally: janus.Beads, janus.herdr = saved
        f = d / "out/u/u.md"
        self.assertTrue(f.exists(), "the brief with the header was not written")
        hd = f.read_text(); rep = str(d / "out/u/REPORT.md")
        self.assertIn(f"Write your report to `{rep}`", hd)     # the absolute path, once, prominently (the opencode path)
        self.assertIn(rep, hd)                                 # and the same path in the park rule and the quality block


class Registration(unittest.TestCase):
    """panel-eng F5: the watcher records the pane it started the new orchestrator in (pending:<ts>:<pane>); only a hook in that
    pane claims the registration. The failure caught: any session in the project tree claiming the pending registration."""
    def setUp(self):
        self.d, self.cfg = project()
        self.saved = (janus.session_root, janus.sh)
        janus.session_root = lambda sid: None; janus.sh = lambda *a, **k: (0, "(prime)", "")
    def tearDown(self): janus.session_root, janus.sh = self.saved
    def hook(self, pane=""):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        with mock.patch.dict(os.environ, {"JANUS_ROLE": "", "HERDR_PANE_ID": pane}):
            if not pane: os.environ.pop("HERDR_PANE_ID", None)
            with mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "sid", "cwd": str(self.d)}))):
                with redirect_stdout(io.StringIO()): janus.cmd_hook("SessionStart")
        return self.cfg.f("session").read_text().strip()
    def test_claimed_from_its_own_pane(self):
        self.cfg.f("session").write_text(f"pending:{int(time.time())}:pane-A")
        self.assertEqual(self.hook("pane-A"), "sid")
    def test_not_claimed_from_another_pane(self):
        self.cfg.f("session").write_text(f"pending:{int(time.time())}:pane-A")
        self.assertTrue(self.hook("pane-B").startswith("pending:"))
    def test_a_pending_without_a_pane_is_still_claimed(self):
        """janus register without a session id writes pending:<ts> (no pane): the fallback still claims it."""
        self.cfg.f("session").write_text(f"pending:{int(time.time())}")
        self.assertEqual(self.hook("pane-B"), "sid")


class Backlog(unittest.TestCase):
    def test_table_and_checkboxes(self):
        d, cfg = project('\n[backlog]\nfile = "S.md"\nready = ["READY", "FIX"]\n')
        (d / "S.md").write_text("# S\n| # | item | status | next |\n|---|---|---|---|\n| A1 | one | DONE | - |\n| A2 | two | FIX | x |\n"
                                "| A3 | three | NOT STARTED | y |\n## more\n- [ ] four things\n- [x] five\n")
        it = {i: (st, dn, rd) for i, t, st, dn, rd in janus.backlog_items(cfg)}
        self.assertEqual(it["A1"], ("DONE", True, False)); self.assertEqual(it["A2"], ("FIX", False, True))
        self.assertEqual(it["A3"], ("NOT STARTED", False, False)); self.assertEqual(it["four-things"][1:], (False, False))

class Resume(unittest.TestCase):
    def test_report_path(self):
        d, cfg = project(); (d / "out/orig").mkdir(); rep = d / "out/orig/REPORT.md"; rep.write_text("VERDICT: BLOCKED\nold\nVERDICT: BLOCKED\n")
        b = d / "briefs/r.md"; b.write_text(f"Continue the same report {rep} (line 1 PENDING).\n")
        a = type("A", (), {"report": None, "resume_of": None, "unit": "r"})()
        self.assertIsNone(janus.resume_report(cfg, a, b))   # never parsed from the brief's prose (a launch died on such a path)
        a.resume_of = "orig"; b.write_text("x\n"); self.assertIsNone(janus.resume_report(cfg, a, b)); self.assertEqual(janus.seed_report(cfg, a), rep.resolve())   # continued as a copy (soak 43b)
        a.resume_of = None; a.report = str(rep); self.assertEqual(janus.resume_report(cfg, a, b), rep.resolve())
        u = {"name": "r", "report": str(rep), "resume": True, "started": time.time() + 5}
        self.assertIsNone(janus.verdict(cfg, u))   # the old verdict does not count
        u["started"] = time.time() - 5; rep.write_text("VERDICT: DONE\nnew\nVERDICT: DONE\n")
        self.assertEqual(janus.verdict(cfg, u), "VERDICT: DONE")

    def test_pueue_command_quoting(self):
        """pueue joins `add -- <args>` with spaces and runs sh -c on it: a value reaches the task intact only when quoted twice."""
        odd = "/x/(`B/out it's/REPORT.md"
        words = [sys.executable, "-c", "import sys; print(sys.argv[1])", odd]
        stored = subprocess.run(["sh", "-c", f"{sys.executable} -c 'import sys; print(\" \".join(sys.argv[1:]))' {janus.pq(words)}"],
                                capture_output=True, text=True).stdout.rstrip("\n")      # what pueue stores as the command
        self.assertEqual(subprocess.run(["sh", "-c", stored], capture_output=True, text=True).stdout.rstrip("\n"), odd)

class ResumeBudget(unittest.TestCase):
    def test_a_capped_unit_is_resumed_with_a_new_budget_in_its_own_tree(self):
        """Catches (item 24): a unit capped at its budget with an unpushed commit could not be continued: janus resume refused
        (under resume_min_usd) and told you to use launch --resume-of, which said 'already launched' for the same unit, was
        refused by H9 for a successor with --cwd, and close said 'unpushed commits'."""
        d, cfg = project()
        b = d / "briefs/u1.md"; b.write_text("# Brief: u1\n")
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "model": "m", "task": 5, "budget": 2.0, "run": "u1.1",
             "brief": str(b), "rbrief": str(b), "rcwd": str(d), "started": 0}
        got = {}
        saved = {k: getattr(janus, k) for k in ("get_unit", "probe_all", "session_of", "run_cost", "ledger_done", "reserve_budget",
                                                "unit_spent", "X", "confirm_started", "save_task_log", "board")}
        bd = type("B", (), {"save": lambda s, x: None, "comment": lambda s, *a: None})()
        janus.get_unit = lambda cfg, n: (bd, u); janus.probe_all = lambda cfg: {"here": {"ok": True, "pueue": {"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}}}}
        janus.session_of = lambda cfg, u: "sid"; janus.run_cost = lambda *a, **k: (2.0, True); janus.ledger_done = lambda *a, **k: None
        janus.unit_spent = lambda cfg, u, wrapup=False: 2.0                       # the whole budget is spent (capped)
        janus.reserve_budget = lambda cfg, mc, name, budget, run=None: got.update(budget=budget) or "u1.r2.1"
        janus.X = lambda cfg, h, cmd, **k: (0, "7", ""); janus.confirm_started = lambda cfg, h, tid: ("running", "")
        janus.save_task_log = lambda *a, **k: None; janus.board = lambda *a, **k: None
        a = type("A", (), {"unit": "u1", "message": "continue", "force": True, "budget": 3.0, "runtime": None, "here": False})()
        try: janus.cmd_resume(cfg, a)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertEqual(got.get("budget"), 3.0, "the new run did not get the new budget")   # 3.0, not 2.0-2.0=0
        self.assertEqual(u["budget"], 5.0)                                        # the unit's budget is its spend plus the new run's


class LaunchQuoting(unittest.TestCase):
    """Defect 18:35 (row 56): launch built the agent's shell command with an unquoted report path that held '(' and a backtick;
    sh failed at once and launch still said 'launched'. Here real launches run from a project path with '(', a backtick, a
    quote and spaces, through a private pueued: the agent gets the --report path intact; a run that dies at once fails."""
    def setUp(self):
        import shutil
        if not shutil.which("pueued") or not shutil.which("pueue"): self.skipTest("pueue not installed")
        self.pd = Path(tempfile.mkdtemp(prefix="oq"))   # short: a unix socket path has a length limit
        (self.pd / "p.yml").write_text(f"shared:\n  pueue_directory: {self.pd}/d\n  runtime_directory: {self.pd}/r\n"
                                       f"  use_unix_socket: true\n  unix_socket_path: {self.pd}/s.sock\n")
        (self.pd / "d").mkdir(); (self.pd / "r").mkdir()
        subprocess.run(["pueued", "-c", str(self.pd / "p.yml"), "-d"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        for _ in range(50):
            if (self.pd / "s.sock").exists(): break
            time.sleep(0.1)
        self.real = (janus.Beads, janus.herdr)

    def tearDown(self):
        janus.Beads, janus.herdr = self.real
        subprocess.run(["pueue", "-c", str(self.pd / "p.yml"), "shutdown"], capture_output=True, timeout=20)

    def test_odd_path_launches_and_dead_run_is_not_launched(self):
        import argparse, io, contextlib
        d = Path(tempfile.mkdtemp(prefix="janus t (`B it's ")); (d / "out").mkdir(); (d / "briefs").mkdir()
        fake = d / "agent dir (`x)" / "fake.py"; fake.parent.mkdir()
        fake.write_text("import json, os, sys\nr = os.environ['JANUS_REPORT']\nopen(r, 'a').write('agent saw ' + r + '\\nVERDICT: DONE\\n')\n"
                        "print(json.dumps({'total_cost_usd': 0.01, 'subtype': 'success'}))\n")
        prov = lambda n, cmd: (f'[providers.{n}]\nenabled = true\ncap_usd = 10\nbudget_per_unit = 1\nledger = "out/ledger-{n}.tsv"\n'
                               f'key_file = {json.dumps(str(d / "key.env"))}\ncmd = {json.dumps(cmd)}\n')
        (d / "janus.toml").write_text(f'[project]\nname = "t"\n[hosts.here]\nlocal = true\npueue_config = "{self.pd}/p.yml"\n'
            + prov("api", [sys.executable, str(fake), "{prompt}"]) + prov("bad", [str(d / "no such agent"), "{prompt}"])
            + '[models.m]\nprovider = "api"\nkind = "api"\napi_id = "x"\n[models.bad]\nprovider = "bad"\nkind = "api"\napi_id = "x"\n')
        k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n"); k.chmod(0o600)
        cfg = janus.Cfg(d)
        (d / "out/orig").mkdir(); rep = d / "out/orig/REPORT.md"; rep.write_text("VERDICT: BLOCKED\nold\n")
        for n in ("u", "dead"): (d / f"briefs/{n}.md").write_text(f"Continue the same report /x/(`B/out/{n}/REPORT.md\n")   # prose: ignored
        saved = {}
        class FakeBeads:   # no beads database in a temp project
            def __init__(self, cfg): pass
            def units(self): return []
            def create(self, title, *a): return title
            def run(self, *a): return []
            def save(self, u): saved[u["name"]] = dict(u)
            def comment(self, *a): pass
        janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None   # never the live herdr session
        hs = {"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "100", "load": "0"}}
        ns = lambda unit, model, report=None: argparse.Namespace(unit=unit, kind="implement", tier="normal", host="here", model=model,
            brief=None, cwd=str(d), budget=0.5, test_host=None, allow_main=True, of=None, report=report, resume_of=None, item=None, probes=hs)
        out = io.StringIO()
        with contextlib.redirect_stdout(out): janus.cmd_launch(cfg, ns("u", "m", report=str(rep)))
        self.assertIn("launched u", out.getvalue())
        tid = saved["u"]["task"]
        subprocess.run(["pueue", "-c", str(self.pd / "p.yml"), "wait", "-q", str(tid)], timeout=60)
        self.assertEqual(janus.task_rc(janus.task_status(cfg.hosts["here"], tid)), 0)
        self.assertIn(f"agent saw {rep.resolve()}\nVERDICT: DONE", rep.read_text())   # the --report path arrived intact
        self.assertTrue(rep.read_text().startswith("VERDICT: PENDING"))              # the resumed report restarted as PENDING
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(janus.Err) as e: janus.cmd_launch(cfg, ns("dead", "bad"))
        self.assertIn("died at once", str(e.exception)); self.assertNotIn("launched dead", out.getvalue())
        self.assertEqual(saved["dead"]["via"], "failed")
        self.assertIn("died-at-start", (d / "out/ledger-bad.tsv").read_text())
        # janus run in the same odd tree: the job script it would queue parses and quotes the odd paths, and the busy record it
        # writes (row 53) holds the tree while the job is queued or running -- no real pueue or admission (item 22: the lane's
        # admission disk floor made this test wait forever on a full root volume)
        wt = d / "wt (`q) x"; wt.mkdir(); (wt / "f it's").write_text("hi\n")
        r = argparse.Namespace(host="here", lane="jobs", unit=None, wt=str(wt), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=False, command=["--", "cat", "f it's"])
        js = janus.job_script(cfg, "here", cfg.hosts["here"], r, "wt (`q) x", "cat f it's", "j-1")
        self.assertEqual(subprocess.run(["bash", "-n"], input=js, text=True, capture_output=True).returncode, 0, js)
        self.assertIn("bash -c 'cat f it'", js, "the command with a quote is not shell-quoted in the job script")
        bdir = cfg.f("runs") / "busy"; bdir.mkdir(parents=True, exist_ok=True); rec = bdir / "j-1"; rec.write_text(f"here:7\t{wt}\n")
        hs = {"here": {"ok": True, "pueue": {"tasks": {"7": {"status": {"Running": {}}, "label": "run:wt___q__x@t"}}}}}
        why = janus.busy_reasons(cfg, wt, hs=hs, ps="")
        self.assertEqual(len(why), 1, why); self.assertRegex(why[0], "is running")
        self.assertEqual(janus.busy_reasons(cfg, d / "briefs", hs=hs, ps=""), [])   # another tree is not busy
        rec.unlink()
        self.assertEqual(janus.busy_reasons(cfg, wt, hs=hs, ps=""), [])
        # a remote host whose root has the same characters: every generated script parses
        cfg.hosts["far"] = {"ssh": "nowhere", "root": "$HOME/o (`r) it's", "_rd": "$HOME/o (`r) it's/runs"}
        for txt in (janus.lane_runner(cfg, "far", cfg.hosts["far"], "u", "$HOME/o (`r) it's/worktrees/u"),
                    janus.job_script(cfg, "far", cfg.hosts["far"], r, "wt (`q) x", "cat x", "j-1")):
            self.assertEqual(subprocess.run(["bash", "-n"], input=txt, text=True, capture_output=True).returncode, 0, txt)

class Busy(unittest.TestCase):
    def test_busy_jobs_units_and_mutation_runs(self):
        """row 53: janus busy <wt> holds while a janus job, an open unit's own job or a mutation run uses the tree."""
        d, cfg = project(); wt = d / "wt"; wt.mkdir(); b = cfg.f("runs") / "busy"; b.mkdir(parents=True)
        (b / "j1").write_text(f"here:5\t{wt}\n"); (b / "j2").write_text(f"here:6\t{wt}\n"); (b / "j3").write_text(f"gone:1\t{wt}\n")
        cfg.hosts["gone"] = {"ssh": "nowhere"}
        hs = {"here": {"ok": True, "pueue": {"tasks": {"5": {"status": {"Running": {}}}, "6": {"status": {"Done": {"result": "Success"}}}}}},
              "gone": {"ok": False}}
        why = janus.busy_reasons(cfg, wt, hs=hs, ps="")
        self.assertEqual(len(why), 2, why); self.assertIn("here:5", why[0]); self.assertIn("unreachable", why[1])
        self.assertFalse((b / "j2").exists())                                   # an ended job's record is dropped
        self.assertEqual(janus.busy_reasons(cfg, d / "other", hs=hs, ps=""), [])
        (b / "j1").unlink(); (b / "j3").unlink()
        why = janus.busy_reasons(cfg, wt, hs=hs, ps=f"4242 cargo mutants --in-place -d {wt}\n4343 vim {wt}/x\n")
        self.assertEqual(len(why), 1, why); self.assertTrue(why[0].startswith("process 4242 (cargo mutants"), why)   # vim is no mutation run

class Close(unittest.TestCase):
    def test_close_counts_only_the_units_subtree(self):
        """board 18:51: a unit whose cwd lies inside the project's repo could never close (other agents' board files and reports
        kept that repo dirty). Only the unit's cwd subtree counts, minus janus's state dir and the board folder."""
        d, cfg = project(); g = lambda *x: subprocess.run(["git", "-C", str(d), *x], capture_output=True, text=True)
        g("init", "-q"); (d / "bin").mkdir(); (d / "bin/tool.sh").write_text("x\n"); g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
        for f in ("board/card.md", ".janus/acks.json", "out/other/REPORT.md", "bin/.janus-note"): (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / "board/card.md").write_text("post\n"); (d / ".janus/acks.json").write_text("{}\n"); (d / "out/other/REPORT.md").write_text("x\n")
        self.assertEqual(janus.unit_dirty(cfg, d / "bin"), ([], True))
        (d / "bin/tool.sh").write_text("y\n"); self.assertEqual(janus.unit_dirty(cfg, d / "bin")[0], ["bin/tool.sh"])

class SelfSafety(unittest.TestCase):
    def test_in_place_edit_flagged_rename_not(self):
        d = Path(tempfile.mkdtemp()); s = d / "loop.sh"; s.write_text("#!/bin/bash\nsleep 30\necho done\n")
        p = subprocess.Popen(["/bin/bash", str(s)]); time.sleep(1.5)
        try:
            janus.atomic_write(s, "#!/bin/bash\nsleep 30\necho other\n", 0o755); time.sleep(1.1)
            self.assertFalse([k for k, _ in janus.script_findings() if k.startswith(f"script:{p.pid}:")])   # renamed: safe
        finally: p.kill(); p.wait()
        p = subprocess.Popen(["/bin/bash", str(s)]); time.sleep(1.5)
        try:
            with open(s, "r+") as f: f.seek(0, 2); f.write("echo appended\n")   # in place
            time.sleep(1.1)
            self.assertTrue([k for k, _ in janus.script_findings() if k.startswith(f"script:{p.pid}:")])
        finally: p.kill(); p.wait()

class Register(unittest.TestCase):
    def test_tree_guard(self):
        d = Path(tempfile.mkdtemp()); g = lambda *x: subprocess.run(["git", "-C", str(d), *x], capture_output=True)
        g("init", "-q", "-b", "main"); (d / "a").write_text("x\n"); g("add", "a"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a")
        _, cfg = project()
        self.assertIsNone(janus.tree_guard(cfg, d))
        (d / "a").write_text("<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n")
        self.assertIn("conflict markers", janus.tree_guard(cfg, d))
        (d / "a").write_text("x\n"); g("checkout", "-q", "-b", "wk"); g("checkout", "-q", "main")
        for i in range(11): (d / "a").write_text(f"{i}\n"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", str(i))
        g("checkout", "-q", "wk")
        self.assertIn("11 commits behind", janus.tree_guard(cfg, d))       # gates and land: refused
        self.assertIsNone(janus.tree_guard(cfg, d, behind=False))            # an agent's own jobs: never for being behind (item 5)

def host_probe(**k):
    """A probe result as probe() returns it, for the capacity model."""
    return {"ok": True, "res": [], "hist": [], "sched": [], "direct": [], "pueue": {"tasks": {}}, "cap_pause": "", **k}

class Capacity(unittest.TestCase):
    """janus-fixes8: reservations, history, back-pressure, agents against build capacity (runs/cap.sh is the one rule)."""
    def setUp(self):
        self.d, self.cfg = project('\n[hosts.b]\nssh = "b"\ncores = 8\ncores_per_job = 2\nram_gb = 8\nservices_gb = 2\njob_gb = 2.5\nagent_gb = 1\nmin_disk_gb = 0\n')
        self.h = dict(self.cfg.hosts["b"], local=True, _rd=str(self.d / "rd")); (self.d / "rd").mkdir()
        (self.d / "rd/cap.sh").write_text(janus.cap_lib(self.cfg, "b", self.h))
    def sh(self, script, env=None):
        return subprocess.run(["sh", "-c", f'RD={self.d}/rd; . $RD/cap.sh\n' + script], capture_output=True, text=True, env={**os.environ, **(env or {})}).stdout
    def test_class_and_learned_reservation_match_python(self):
        self.assertEqual(self.sh('cap_class "cd processing && FOO=1 nice -n 10 cargo test -p x"').strip(), "cargo_test")
        self.assertEqual(self.sh('cap_mb cargo_test 2560 2560').strip(), "2560")              # nothing learned: job_gb
        hist = ["1\tcargo_test\t900\t60\t0", "2\tcargo_test\t1200\t300\t0", "3\tother\t9000\t5\t0"] + [f"{i}\tagent\t{v}\t9\t0" for i, v in enumerate((200, 700, 300))]
        (self.d / "rd/history.tsv").write_text("\n".join(hist) + "\n")
        for cls in ("cargo_test", "other", "agent", "none"):
            cap = 0 if cls == "agent" else 2560
            self.assertEqual(int(self.sh(f'cap_mb {cls} 2560 {cap}')), janus.class_mb(hist, cls, 2560, cap), cls)
        self.assertEqual(janus.class_mb(hist, "cargo_test", 2560, 2560), 1380)   # max 1200 + 15 %
        self.assertEqual(janus.class_mb(hist, "agent", 1024, 0), 345)            # median 300 + 15 %
    def test_reservations_admission_and_pause(self):
        holder = subprocess.Popen(["sleep", "60"])
        try:
            (self.d / "rd/a.res").write_text(f"{holder.pid} 6 3000 1 cargo_test\n"); (self.d / "rd/gone.res").write_text("999999 2 9000 1 x\n")
            fake = 'cap_measure() { PSI=${T_PSI:-0}; LOAD=${T_LOAD:-1}; DF=50; }\n'
            out = self.sh(fake + 'CAP_RO=; admit_ok cargo_test && echo ADMIT || echo WAIT; echo "$RJ $RC $RM"')
            self.assertIn("ADMIT", out); self.assertIn("1 6 3000", out)        # 6 + 2 of 8 cores, 3000 + 2560 of 6144 MB: fits
            self.assertFalse((self.d / "rd/gone.res").exists())                # a reservation whose process ended is removed
        finally: pass
        try:
            (self.d / "rd/b.res").write_text(f"{holder.pid} 2 100 1 x\n")      # 8 of 8 cores reserved: the next job waits
            self.assertIn("WAIT", self.sh(fake + 'admit_ok cargo_test && echo ADMIT || echo WAIT'))
            (self.d / "rd/b.res").unlink()
            out = self.sh(fake + 'admit_ok cargo_test && echo ADMIT || echo WAIT; echo "P=$PAUSE"', {"T_PSI": "12.5"})
            self.assertIn("WAIT", out); self.assertIn("memory PSI", out)       # back-pressure pauses admission ...
            self.assertTrue(holder.poll() is None)                             # ... and kills nothing
            darwin = 'uname() { echo Darwin; }\nsysctl() { case "$*" in *pressure*) echo $T_LVL;; *) echo "{ 0.5 0.5 0.5 }";; esac; }\n'   # the Mac branch, any host
            out = self.sh(darwin + 'admit_ok cargo_test && echo ADMIT || echo WAIT; echo "P=$PAUSE"', {"T_LVL": "2"})
            self.assertIn("WAIT", out); self.assertIn("memory PSI avg10 50 over", out)   # a real warn level still pauses (the simulated signal) ...
            self.assertIn("ADMIT", self.sh(darwin + 'admit_ok cargo_test && echo ADMIT || echo WAIT', {"T_LVL": "1"}))   # ... a normal level admits
            mac = 'uname() { echo Darwin; }\n'   # the suite's own calm sysctl (janusload) answers: a Mac's live level must not decide these tests
            self.assertIn("ADMIT", self.sh(mac + 'admit_ok cargo_test && echo ADMIT || echo WAIT'))
            self.assertIn("WAIT", self.sh(mac + 'admit_ok cargo_test && echo ADMIT || echo WAIT', {"JANUS_TEST_PRESSURE": "2"}))
            out = self.sh(fake + 'HOLD=0; admit_ok cargo_test && echo ADMIT || echo WAIT; echo "P=$PAUSE"', {"T_LOAD": "11"})
            self.assertIn("load 11 over 1.25 x 8", out)
            out = self.sh(fake + 'HOLD=60; CAP_RO=; admit_ok x && echo ADMIT || echo WAIT', {"T_LOAD": "11"})   # load must be held HOLD s
            self.assertIn("ADMIT", out)
        finally: holder.kill(); holder.wait()
    def test_job_script_admits_runs_records(self):
        """A Mac (local) jobs-lane job (register item 5): waits for admission, holds its reservation, records its peak. The job
        allocates only after 4 s and then ends, between two 5 s samples: the sampler alone recorded no peak (the flake under
        load, when a slow start pushed the allocation past the last sample); the kernel's high-water mark records it."""
        holder = subprocess.Popen(["sleep", "60"])
        rd = self.d / "rd"; (rd / "h.res").write_text(f"{holder.pid} 8 100 1 x\n")   # all cores reserved by another job
        a = type("A", (), {"lane": "jobs", "subdir": None, "wt": str(self.d), "usb": False})()
        cmd = sys.executable + " -c 'import os, time; time.sleep(4); b = bytearray(os.urandom(60 * 1024 * 1024))'"   # incompressible: macOS compresses zero pages under pressure
        js = janus.job_script(self.cfg, "b", self.h, a, "t", cmd, "t-1"); (rd / "t-1.sh").write_text(js)
        p = subprocess.Popen(["bash", str(rd / "t-1.sh")], env={**os.environ, "JANUS_POLL_S": "1"}, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            time.sleep(3)
            self.assertIn("waiting for admission", (rd / "t-1.log").read_text()); self.assertFalse((rd / "t-1.res").exists())
            holder.kill(); holder.wait(); time.sleep(3)
            self.assertTrue((rd / "t-1.res").exists())                         # admitted: its reservation is held while it runs
            self.assertEqual(p.wait(timeout=30), 0)
        finally:
            holder.kill()
            if p.poll() is None: os.killpg(p.pid, 9)
        self.assertFalse((rd / "t-1.res").exists())
        f = (rd / "history.tsv").read_text().split("\t")
        self.assertTrue(f[1].startswith("python")); self.assertGreaterEqual(int(f[2]), 50)   # class and its peak (60 MB held)
    def test_launch_host_option(self):
        """review-fixes8 #1: --host placed the agent with a 2-tuple where choose() unpacks 3."""
        d, cfg = project()
        a = type("A", (), {"host": "here", "test_host": None, "unit": "u", "kind": "review"})()
        hs = {"here": {"name": "here", "ok": True, "avail_mb": "8000", "disk_gb": "100"}}
        hn, b, why = janus.choose(cfg, a, d / "briefs/none.md", cfg.models["m"], [], hs)
        self.assertEqual((hn, b), ("here", "here"))
    def test_build_room_and_when(self):
        p = host_probe(cap_rj="2", cap_rc="4", cap_rm="5000", hist=[f"{int(time.time()) - 100}\tcargo_test\t2000\t600\t0"],
                       res=[f"1 2 2300 {int(time.time()) - 60} cargo_test a.res"])
        units = [{"name": "u1", "host": "b", "build": "b"}]
        hc = janus.host_cap(self.cfg, "b", p, units)
        self.assertEqual(hc["slots"], 2)                                       # 6144 MB - 1 agent 1024 MB over 2300 MB per job; cores 4
        self.assertTrue(janus.build_room(self.cfg, "b", hc)[0])                 # 1 agent for 2 slots: a free reservation
        units += [{"name": f"u{i}", "host": "mac", "build": "b"} for i in (2, 3)]
        hc = janus.host_cap(self.cfg, "b", p, units); ok, why = janus.build_room(self.cfg, "b", hc)
        self.assertTrue(ok, why); self.assertIn("queue position", why)          # 3 agents, 2 slots: a queue position (1 of 2 running jobs)
        units += [{"name": "u4", "host": "mac", "build": "b"}]
        hc = janus.host_cap(self.cfg, "b", p, units); ok, why = janus.build_room(self.cfg, "b", hc)
        self.assertFalse(ok); self.assertIn("should free about", why)          # 4 agents = 2 x slots: refused, and says when
        hc = janus.host_cap(self.cfg, "b", dict(p, cap_pause="memory PSI avg10 30 over 10"), units[:1])
        self.assertFalse(janus.build_room(self.cfg, "b", hc)[0])

class DiskFloor(unittest.TestCase):
    def test_the_disk_floor_honours_the_host_value_over_capacity(self):
        """Catches (item 20): the disk finding and the land lane used a fixed default instead of [hosts.<h>] min_disk_gb (host
        value over [capacity], the per-host floor the config documents)."""
        import argparse
        d, cfg = project('\n[capacity]\nmin_disk_gb = 3\n')
        h = cfg.hosts["here"]; p = {"ok": True, "disk_gb": "5", "load": "0"}
        self.assertEqual(janus.capv(cfg, h, "min_disk_gb"), 3)                 # [capacity]
        self.assertEqual(janus.margins(h, p, cfg=cfg), [])                     # 5 GB free is over the 3 GB floor
        h["min_disk_gb"] = 7
        self.assertEqual(janus.capv(cfg, h, "min_disk_gb"), 7)                 # the host value wins over [capacity]
        self.assertTrue(any("disk" in w for w in janus.margins(h, p, cfg=cfg)), "the disk finding ignored the host floor")
        h.pop("min_disk_gb")                                                   # the land lane falls back to [capacity] too
        r = argparse.Namespace(host="here", lane="land", unit=None, wt=str(d), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=False, command=["--", "true"], broad=None)
        js = janus.job_script(cfg, "here", h, r, "u", "true", "j-1")
        self.assertIn('"$d" -ge 3', js, "the land lane used a fixed disk floor, not [capacity] min_disk_gb")


class AdmitBound(unittest.TestCase):
    def test_a_blocked_job_says_why_and_gives_up(self):
        """Catches (item 21): a job blocked in admission waited forever with no reason and no bound. It now says why (with
        numbers) at least every REPORT s and gives up with exit 121 after the lane's bound (finding 3: 121 = never admitted,
        distinct from a command's own timeout, 124)."""
        import argparse
        d, cfg = project('\n[capacity]\nmin_disk_gb = 99999\n')
        h = cfg.hosts["here"]; janus.write_cap(cfg, "here", h)
        r = argparse.Namespace(host="here", lane="jobs", unit=None, wt=str(d), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=False, command=["--", "true"], broad=None)
        js = janus.job_script(cfg, "here", h, r, "u", "true", "j-1")
        p = subprocess.run(["bash"], input=js, text=True, capture_output=True, timeout=60,
                           env={**os.environ, "JANUS_POLL_S": "1", "JANUS_ADMIT_MAX_S": "3", "JANUS_ADMIT_REPORT_S": "1"})
        self.assertEqual(p.returncode, 121, p.stdout + p.stderr)
        self.assertGreaterEqual(p.stdout.count("waiting for admission"), 2, p.stdout)
        self.assertIn("disk", p.stdout); self.assertIn("giving up", p.stdout)

    def test_the_caller_reports_a_slow_admission_and_keeps_waiting(self):
        """Catches (item 21; finding 9): the caller's pueue wait had no timeout and no output; a task the host's admission is
        holding is reported with the admission reason, whatever its pueue status."""
        d, cfg = project(); h = cfg.hosts["here"]
        real_run = subprocess.run; n = [0]
        def fake_run(cmd, **k):
            if isinstance(cmd, list) and any("wait -q" in str(x) for x in cmd):
                n[0] += 1
                if n[0] == 1: raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))
                return type("R", (), {"returncode": 0})()
            return real_run(cmd, **k)
        states = [{"status": {"Queued": {}}}, {"status": {"Done": {"result": "Success"}}}]
        real = janus.task_status, janus.probe, janus.hrun
        subprocess.run = fake_run
        janus.task_status = lambda h, tid: states.pop(0) if states else {"status": {"Done": {"result": "Success"}}}
        janus.probe = lambda n, h, **k: {"cap_admit": "disk", "cap_pause": "", "disk_gb": "9"}
        janus.hrun = lambda h, script, **k: (0, "y\n", "")   # this job's own admission-wait file exists (finding 2, fix-install6)
        out = io.StringIO()
        try:
            with contextlib.redirect_stderr(out): rc = janus.finish_job(cfg, "here", h, 7, "j-1", quiet=True)
        finally: subprocess.run = real_run; janus.task_status, janus.probe, janus.hrun = real
        self.assertEqual(rc, 0)
        self.assertIn("held by admission", out.getvalue()); self.assertIn("disk", out.getvalue())

    def test_a_blocked_admission_is_a_finding_with_the_reason(self):
        """Catches (item 21): a job held in admission raised no finding naming why."""
        d, cfg = project()
        hs = {"here": {"ok": True, "pueue": {"groups": {"jobs": {"parallel_tasks": 2, "status": "Running"}}, "tasks": {}},
                       "wait_jobs": "1", "cap_admit": "disk", "cap_pause": "", "disk_gb": "9", "cap_rc": "1", "cap_rm": "2048",
                       "cap_ram": "0", "cap_psi": "0", "res": [], "hist": [], "sched": [], "direct": [], "tmux": []}}
        F = dict(janus.lane_findings(cfg, hs))
        self.assertIn("blocked:here:jobs", F); self.assertIn("disk 9G free, floor 10G", F["blocked:here:jobs"])


class Fill(unittest.TestCase):
    def test_fill_until_capacity_or_budget(self):
        d, cfg = project('\n[backlog]\nfile = "S.md"\n[routing]\nnormal = ["m"]\ncritical = ["m"]\n'
                         '[hosts.b]\nssh = "b"\ncores = 4\ncores_per_job = 2\nram_gb = 8\nservices_gb = 2\njob_gb = 2\nagent_gb = 1\n')
        (d / "S.md").write_text("| # | item | status |\n|---|---|---|\n| X1 | one | READY |\n| X2 | two | READY |\n| X3 | three | READY |\n| X4 | four | DONE |\n")
        for n, it in (("x1-a", "X1"), ("x2-a", "X2"), ("x3-a", "X3")): (d / f"briefs/{n}.md").write_text(f"# {n}\nITEM: {it}\nTEST-HOST: b\n")
        cfg.hosts["here"].update(ram_gb=64, cores=16)
        janus.Beads.run = lambda self, *a: (_ for _ in ()).throw(janus.Err("no beads"))   # no beads here: the checklist is the backlog
        hs = {"here": host_probe(name="here"), "b": host_probe(name="b")}
        plan, stops = janus.fill_plan(cfg, hs, [])
        self.assertEqual([x[0] for x in plan], ["x1-a", "x2-a"])               # b has 2 build slots (4 cores / 2): two agents ...
        self.assertTrue(all(x[4] == "b" for x in plan))
        self.assertIn("x3-a", stops[0])                                        # ... but no more queue position: nothing runs yet
        cfg.prov["api"]["cap_usd"] = 1.5                                       # budget_per_unit 1: the second does not fit
        plan, stops = janus.fill_plan(cfg, hs, [])
        self.assertEqual(len(plan), 1); self.assertIn("budget", stops[0])

class Budget(unittest.TestCase):
    def test_median_of_kind_and_tier_plus_margin(self):
        d, cfg = project('\n[budget]\nmargin_pct = 50\n')
        cfg.run.all_units = {f"u{i}": {"name": f"u{i}", "kind": "implement", "tier": "critical"} for i in range(4)}
        for i in range(4):   # item 16c: a run counts only when its unit ended with a verdict
            o = d / "out" / f"u{i}"; o.mkdir(); (o / "REPORT.md").write_text("VERDICT: DONE\ndone\n")
        for i, c in enumerate((1.0, 2.0, 3.0, 9.0)):
            janus.ledger_append(cfg, cfg.prov["api"], ["t", f"u{i}", "running", 5, f"r{i}"]); janus.ledger_append(cfg, cfg.prov["api"], ["t", f"u{i}", c, "done rc=0", f"r{i}"])
        b, why = janus.budget_for(cfg, cfg.models["m"], "implement", "critical")
        self.assertEqual(b, 4.0); self.assertIn("median $2.50", why)            # median 2.5 + 50 % = 3.75, rounded up to $0.50
        self.assertEqual(janus.budget_for(cfg, cfg.models["m"], "review", "normal")[0], 1)   # no history: budget_per_unit

class Land(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project('\n[landing]\nbranch = "main"\n')
        self.r = self.d / "repo"; self.r.mkdir(); self.wt = self.d / "wt"
        self.g("init", "-q", "-b", "main"); (self.r / "a").write_text("1\n2\n3\n"); self.g("add", "a"); self.g("commit", "-qm", "base")
        self.g("config", "user.name", "t"); self.g("config", "user.email", "t@t")   # land's own commits: a machine without a git identity failed these tests
        self.g("worktree", "add", "-q", "-b", "wk/u", str(self.wt))
        self.u = {"id": "x-1", "name": "u", "cwd": str(self.wt), "tier": "normal"}
        janus.Beads.units = lambda s: [self.u]; janus.Beads.comment = lambda s, *a: None
        self.cfg.run.all_units = {"u": self.u}
    def g(self, *a, cwd=None): return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd or self.r), *a], capture_output=True, text=True)
    def a(self, **k): return type("A", (), {"unit": "u", "no_gate": True, "no_dispatch": True, **k})()
    def test_a_land_on_another_host_merges_gates_and_pushes_there(self):
        """Catches (skill-fix55): a land could only merge, gate and push in the coordinator's worktree: named another host, it
        still did its git work on this machine's (stale) main and pushed nothing, so no other host could ever land."""
        from unittest import mock
        o = self.d / "o.git"; self.g("init", "-q", "--bare", "-b", "main", str(o)); self.g("remote", "add", "origin", str(o)); self.g("push", "-q", "-u", "origin", "main")
        (self.wt / "b").write_text("new\n"); self.g("add", "b", cwd=self.wt); self.g("commit", "-qm", "u: add b", cwd=self.wt)
        other = self.d / "other"; self.g("clone", "-q", str(o), str(other)); (other / "c").write_text("moved\n")
        self.g("add", "c", cwd=other); self.g("commit", "-qm", "main: c", cwd=other); self.g("push", "-q", "origin", "main", cwd=other)   # the remote's main moves; this machine never fetches it
        tip = self.g("rev-parse", "HEAD", cwd=other).stdout.strip()
        self.cfg.hosts["far"] = {"ssh": "far", "root": str(self.d / "far")}; self.cfg.c["landing"]["final_gate"] = "test -f b"
        ran = []
        with mock.patch.object(janus, "hrun", lambda h, script, timeout=25, inp=None, env=None: janus.sh(["sh", "-c", script], timeout=timeout, inp=inp)), \
             mock.patch.object(janus, "ssh_url", lambda h, p: p), mock.patch.object(janus, "cmd_run", lambda cfg, ra: ran.append(ra) or 0), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(janus.cmd_land(self.cfg, self.a(host="far", no_gate=False)), 0)
        tree = self.d / "far" / "land" / "wt" / "u"
        self.assertEqual((ran[0].host, ran[0].host_tree), ("far", str(tree)))   # the gate: in the host's own tree, through its lane
        self.assertTrue((tree / "b").is_file() and (tree / "c").is_file())      # ... which holds the merge made there
        self.assertEqual(self.g("log", "--format=%s %P", "-1", "main", cwd=o).stdout.strip(), f"u: land wk/u (1 commit(s), squashed) {tip}")   # pushed from there, onto the remote's tip
        self.assertEqual(janus.gate_place(self.cfg, "far", "janus gate land-u --wait")[0], "here")   # row 64: a janus gate is the coordinator's dispatcher, never run on the far host

    def test_squash_land_and_conflict_stop(self):
        (self.wt / "b").write_text("new\n"); self.g("add", "b", cwd=self.wt); self.g("commit", "-qm", "u: add b", cwd=self.wt)
        (self.r / "c").write_text("main moved\n"); self.g("add", "c"); self.g("commit", "-qm", "main: c")   # main moves: merge needed
        self.assertEqual(janus.cmd_land(self.cfg, self.a()), 0)
        self.assertEqual(self.g("log", "--format=%s", "-1").stdout.strip(), "u: land wk/u (1 commit(s), squashed)")
        self.assertEqual(sorted(p.name for p in self.r.iterdir() if p.name != ".git"), ["a", "b", "c"])   # the main checkout followed
        self.assertEqual(self.g("rev-list", "--count", "main").stdout.strip(), "3")                        # one squashed commit
        (self.wt / "a").write_text("1\nX\n3\n"); self.g("commit", "-qam", "u: a", cwd=self.wt)
        (self.r / "a").write_text("1\nY\n3\n"); self.g("commit", "-qam", "main: a")
        head = self.g("rev-parse", "HEAD", cwd=self.wt).stdout
        self.assertEqual(janus.cmd_land(self.cfg, self.a()), 3)                 # conflict: stops ...
        self.assertEqual(self.g("rev-parse", "HEAD", cwd=self.wt).stdout, head)   # ... with the tree untouched
        self.assertNotEqual(self.g("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=self.wt).returncode, 0)
    def test_land_once(self):
        """review-fixes8 #2: after a squash-land the unit counts as landed (its tree is on main), and a second land is refused."""
        (self.wt / "b").write_text("new\n"); self.g("add", "b", cwd=self.wt); self.g("commit", "-qm", "u: b", cwd=self.wt)
        (self.r / "c").write_text("main moved\n"); self.g("add", "c"); self.g("commit", "-qm", "main: c")
        self.assertIs(janus.landed(self.cfg, self.u), False)
        self.assertEqual(janus.cmd_land(self.cfg, self.a()), 0)
        self.assertIs(janus.landed(self.cfg, self.u), True)
        with self.assertRaises(janus.Err) as e: janus.cmd_land(self.cfg, self.a())
        self.assertIn("already landed", str(e.exception))
        self.assertEqual(self.g("rev-list", "--count", "main").stdout.strip(), "3")   # no duplicate squash commit
    def _odd_branch(self, name):
        self.assertEqual(self.g("branch", "-m", name, cwd=self.wt).returncode, 0, f"git itself refuses the branch name {name}")
        (self.wt / "b").write_text("new\n"); self.g("add", "b", cwd=self.wt); self.g("commit", "-qm", "u: b", cwd=self.wt)
        (self.r / "c").write_text("main moved\n"); self.g("add", "c"); self.g("commit", "-qm", "main: c")   # behind main: a merge is due
        return self.g("rev-parse", "HEAD", cwd=self.wt).stdout.strip()
    def test_a_legal_branch_name_lands_when_no_gate_command_takes_it(self):
        """Catches (review-fix-install7 F2): `janus land --no-gate` on branch `wk/fix#12` merged main into it and THEN refused
        the name for a gate command that was never going to be built; the same with a gate template that has no {branch}."""
        self._odd_branch("wk/fix#12")
        self.assertEqual(janus.cmd_land(self.cfg, self.a()), 0)
        self.assertEqual(self.g("log", "-1", "--format=%s", "main").stdout.strip()[:1], "u", "nothing was landed on main")
        for legal in ("wk/fix#12", "issue-12=retry", "wk/\u00fcberholt", "release,candidate", "fix%20thing"):
            self.assertIn(legal, janus.final_gate_cmd_checked({"final_gate": "./gate {branch}"}, "u", "/w", legal), f"{legal} is a legal branch name")
        self.assertEqual(janus.final_gate_cmd_checked({"final_gate": "./check"}, "u", "/w", "wk/a;b"), "./check")   # the gate never sees it
    def test_a_branch_the_gate_cannot_take_is_refused_before_anything_is_merged(self):
        """Catches (review-fix-install7 F2): the refusal came after step 1, so the unit's branch was left holding a merge commit
        for a land that could not go on; and a name starting with `-` reached the gate's command as an option."""
        head = self._odd_branch("wk/a;b"); self.cfg.c["landing"].update(final_gate_host="here", final_gate="./gate {branch}")
        with self.assertRaises(janus.Err) as e: janus.cmd_land(self.cfg, self.a(no_gate=False))
        self.assertEqual(self.g("rev-parse", "HEAD", cwd=self.wt).stdout.strip(), head, "main was merged into the branch before the refusal")
        self.assertIn("';'", str(e.exception))
        for hostile in ("--force", "#x", "a b", "a$b", "a`b", "a&b", "a|b", "a'b", 'a"b', "a\nb", "a!b", "a\u00a0b", ""):
            with self.assertRaises(janus.Err, msg=repr(hostile)): janus.final_gate_cmd_checked({"final_gate": "git push origin {branch}"}, "u", "/w", hostile)
    def test_gate_required(self):
        (self.wt / "b").write_text("new\n"); self.g("add", "b", cwd=self.wt); self.g("commit", "-qm", "u: b", cwd=self.wt)
        with self.assertRaises(janus.Err) as e: janus.cmd_land(self.cfg, self.a(no_gate=False))
        self.assertIn("final_gate", str(e.exception))

class Probe(unittest.TestCase):
    def test_scheduler_and_capacity_report(self):
        """One scheduler per host (item 1): another job-running process shows in the probe; the probe carries cap.sh's report."""
        d, cfg = project('\n[hosts.here]\n'.replace("[hosts.here]", "") )
        h = dict(cfg.hosts["here"], schedulers=["sleep 4242"]); cfg.hosts["here"] = h
        s = subprocess.Popen(["sleep", "4242"])
        try:
            Path(h["_rd"]).mkdir(parents=True, exist_ok=True); (Path(h["_rd"]) / "x.res").write_text(f"{s.pid} 1 700 1 cargo_test\n")
            p = janus.probe("here", h, ro=True, cfg=cfg)
            self.assertTrue(p["ok"], p); self.assertTrue(any(str(s.pid) in x for x in p["sched"]), p["sched"])
            self.assertEqual((p["cap_rj"], p["cap_rm"]), ("1", "700")); self.assertTrue(p["res"])
        finally: s.kill(); s.wait()

class Park(unittest.TestCase):
    """janus-fixes9 item 1: a WAITING line parks a unit on its jobs; PENDING is no verdict; a resume continues the session."""
    def test_waiting_line_jobs_and_states(self):
        d, cfg = project(); r = d / "out/u/REPORT.md"; r.parent.mkdir(parents=True)
        r.write_text("VERDICT: PENDING\nstep 1 done\nWAITING here:7 here:8 nohost:9\n")
        u = {"name": "u", "started": 0}
        self.assertIsNone(janus.verdict(cfg, u))   # PENDING is not a verdict (close and park relied on that)
        self.assertEqual(janus.waiting_line_jobs(cfg, u), ["here:7", "here:8"])   # unknown hosts ignored
        u["handled_jobs"] = ["here:7"]; self.assertEqual(janus.waiting_line_jobs(cfg, u), ["here:8"])
        hs = {"here": {"ok": True, "waitjob": ["wt-1"], "pueue": {"tasks": {
            "7": {"status": {"Done": {"result": "Success"}}}, "8": {"status": {"Running": {}}, "command": "bash /x/runs/wt-1.sh", "label": "unit:u@t"},
            "9": {"status": {"Queued": {}}, "label": "unit:u@t"}}}}}
        self.assertEqual(janus.job_state(hs, "here:7"), ("done", 0))
        self.assertEqual(janus.job_state(hs, "here:8")[0], "admission")   # running, but still in janus's admission wait
        self.assertEqual(janus.job_state(hs, "here:9")[0], "queued")
        self.assertEqual(janus.job_state(hs, "here:5")[0], "gone")
        self.assertEqual(sorted(janus.unit_jobs(cfg, hs, "u")), ["here:8", "here:9"])
        # finding 1 (fix-install3): a park line after the last VERDICT line means parked; a closing VERDICT: DONE after it is
        r.write_text("VERDICT: DONE\nexit codes: 0\nWAITING here:8\n"); self.assertIsNone(janus.verdict(cfg, u))
        r.write_text("VERDICT: DONE\nexit codes: 0\nWAITING here:8\nVERDICT: DONE\n"); self.assertEqual(janus.verdict(cfg, u), "VERDICT: DONE")

    def test_wrapup_once_when_capped_without_verdict(self):
        """item 2: a run that ended at its budget cap with no final verdict is wrapped up once; not with a verdict, not twice."""
        d, cfg = project(); o = d / "out/u"; o.mkdir(parents=True); (o / "REPORT.md").write_text("VERDICT: PENDING\nstep 1\n")
        (o / "api.json").write_text(json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": 1.0}))
        u = {"name": "u", "via": "pueue", "model": "m", "state": "failed", "verdict": None, "started": 0}
        self.assertTrue(janus.wrap_due(cfg, u))
        self.assertFalse(janus.wrap_due(cfg, {**u, "wrapup": "u.r2.1"}))
        self.assertFalse(janus.wrap_due(cfg, {**u, "verdict": "VERDICT: BLOCKED"}))
        self.assertFalse(janus.wrap_due(cfg, {**u, "state": "working"}))
        (o / "api.json").write_text(json.dumps({"subtype": "success"})); self.assertFalse(janus.wrap_due(cfg, u))

    def test_orphan_jobs(self):
        """item 3: a job whose owner is gone: closed unit, ended agent not parked, ended wrapper; never an idle herdr agent's or a
        parked unit's job."""
        d, cfg = project(); run = lambda: {"status": {"Running": {}}}
        hs = {"here": {"ok": True, "pueue": {"tasks": {
            "1": {**run(), "label": "unit:closed@t"}, "2": {**run(), "label": "unit:api@t"}, "3": {**run(), "label": "unit:idle@t"},
            "4": {**run(), "label": "unit:parked@t"}, "5": {**run(), "label": "run:x@t", "command": "bash /r/runs/x-1.sh"}, "6": {"status": {"Done": {"result": "Success"}}, "label": "unit:closed@t"}}}}}
        units = [{"name": "api", "via": "pueue", "state": "done"}, {"name": "idle", "via": "herdr", "state": "idle"},
                 {"name": "parked", "via": "pueue", "state": "parked", "_park": ["here:4"]}]
        wd = d / ".janus/runs/wrap"; wd.mkdir(parents=True); (wd / "w-1").write_text("here:5 x-1\n")   # its wrapper is not running
        got = dict(janus.orphan_jobs(cfg, hs, units))
        self.assertEqual(sorted(got), ["here:1", "here:2", "here:5"], got)
        self.assertEqual(janus.stop_job_cmd({"runner": "podman"}, "j1").split(";")[0], "podman stop -t 5 janus-run-j1 >/dev/null 2>&1")
        self.assertIn("janus-j1.scope", janus.stop_job_cmd({}, "j1"))

    def test_uncollected_guard(self):
        """item 4: a remote copy with changes or output that collect has not brought here refuses stop/close; a fresh copy does not."""
        d, cfg = project(); r = d / "remote"
        for x in ("base/u", "worktrees/u", "out/u"): (r / x).mkdir(parents=True)
        (r / "base/u/f").write_text("a\n"); (r / "worktrees/u/f").write_text("a\n"); (r / "out/u/REPORT.md").write_text("VERDICT: PENDING\n")
        cfg.hosts["r"] = {"ssh": "nowhere", "root": str(r), "_rd": str(r / "runs")}
        real = janus.hrun; janus.hrun = lambda h, script, **k: real({"local": True}, script, **k)
        try:
            u = {"name": "u", "host": "r", "via": "tmux"}
            self.assertIsNone(janus.uncollected(cfg, u))                        # nothing written yet
            (r / "worktrees/u/f").write_text("a\nb\n"); self.assertIn("not collected", janus.uncollected(cfg, u))
            u["collected"] = janus.fingerprint(cfg, u)["fp"]; self.assertIsNone(janus.uncollected(cfg, u))   # as collect records it
            (r / "out/u/REPORT.md").write_text("VERDICT: PENDING\nstep 2\n"); self.assertIn("not collected", janus.uncollected(cfg, u))
        finally: janus.hrun = real

    def test_resume_argv_and_no_ack(self):
        mc = {"api_id": "x"}
        a = janus.api_argv({}, mc, 1, "go", "S1")
        self.assertEqual(a[:5], ["claude", "--bare", "-p", "--resume", "S1"]); self.assertEqual(a[-1], "go")
        self.assertEqual(janus.api_argv({"cmd": ["c", "{prompt}"], "resume_cmd": ["c", "resume", "{session}", "{prompt}"]}, mc, 1, "go", "S1"), ["c", "resume", "S1", "go"])
        with self.assertRaises(janus.Err): janus.api_argv({"cmd": ["c", "{prompt}"]}, mc, 1, "go", "S1")   # cannot guess how to resume
        d, cfg = project()
        with self.assertRaises(janus.Err): janus.cmd_ack(cfg, type("A", (), {"problem": "waiting:u", "decision": "x"})())

class Report(unittest.TestCase):
    def test_outcomes_waste_pause_and_checkpoint(self):
        """item 6: spend by outcome; capped-without-verdict spend over waste_pause % pauses launch until acked; report-due."""
        d, cfg = project('\n[report]\nevery_h = 0\nat = ["00:00"]\n'); mc = cfg.models["m"]; pv = cfg.prov["api"]
        for n, cost, sub, rep in (("ok", 1.0, "success", "VERDICT: DONE\nexit codes 0\n"), ("cap", 3.0, "error_max_budget_usd", "VERDICT: PENDING\n"),
                                  ("half", 1.0, "error_max_budget_usd", "VERDICT: PENDING\nstep 1 done\nstep 2 done\n")):
            run = janus.reserve_budget(cfg, mc, n, 5, f"{n}.1"); janus.ledger_done(cfg, pv, run, n, cost, 0)
            o = d / "out" / n; o.mkdir(); (o / "REPORT.md").write_text(rep); (o / f"api-{run}.json").write_text(json.dumps({"subtype": sub}))
        janus.reserve_budget(cfg, mc, "lost", 2, "lost.1")
        cfg.run.all_units = {n: {"name": n, "run": f"{n}.1", "status": "closed"} for n in ("ok", "cap", "half", "lost")}
        got = {x[2]: x[4] for x in janus.run_outcomes(cfg)}
        self.assertEqual(got, {"ok": "verdict", "cap": "capped-nothing", "half": "capped-partial", "lost": "unknown"})
        self.assertIn("waste pause", "refused (waste pause): " + (janus.waste_paused(cfg) or ""))   # 4 of 7 dollars capped: over 25 %
        self.assertIsNotNone(janus.waste_paused(cfg))
        janus.cmd_ack(cfg, type("A", (), {"problem": "waste-pause", "decision": "cause: budgets too small; fixed"})())
        self.assertIsNotNone(janus.waste_paused(cfg), "waste_paused must still state the reason, or the ack is dropped as 'finding gone' (item 7)")
        self.assertIsNone(janus.waste_block(cfg))   # the orchestrator acked the cause: launch and fill are not refused
        self.assertIn("report-due", [k for k, _ in janus.report_findings(cfg, [])])
        cfg.touch("report_sent"); self.assertNotIn("report-due", [k for k, _ in janus.report_findings(cfg, [])])

class FalseFindings(unittest.TestCase):
    """item 7: findings that were false."""
    def test_localtest_skips_a_shim_whose_child_is_janus_run(self):
        d, cfg = project()
        shim = " 10     1    10   0.0    01:00 sh -c cargo test -p x\n"
        self.assertIn("localtest:10", [k for k, _ in janus.proc_findings(cfg, shim)])          # a build outside the lanes: flagged
        child = shim + " 11    10    10   0.0    00:59 python3 /x/janus.py run --host h --wt /w -- cargo test -p x\n"
        self.assertNotIn("localtest:10", [k for k, _ in janus.proc_findings(cfg, child)])     # its child is janus run: a lane call

    def test_git_dirty_ignores_state_files(self):
        d, cfg = project(); g = lambda *x: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *x], capture_output=True)
        (d / ".janus/acks.json").write_text("{}"); g("init", "-q"); g("add", "-A"); g("commit", "-qm", "a")
        (d / ".janus/acks.json").write_text('{"x": 1}'); (d / ".janus/open.json").write_text("{}")   # an ack, a check: janus's own
        self.assertNotIn("git:dirty", [k for k, _ in janus.git_findings(cfg)])
        (d / ".janus/history.md").write_text("notes\n")                                       # the orchestrator's notes count
        self.assertIn("git:dirty", [k for k, _ in janus.git_findings(cfg)])

    def test_idle_lane_uses_the_admission_verdict(self):
        """row 48: jobs admitted inside their scripts (waiting there) are held by admission: the lane is not idle."""
        d, cfg = project('\n[capacity]\nmin_disk_gb = 100000\n'); h = cfg.hosts["here"]
        p = janus.probe("here", h, ro=True, cfg=cfg)
        self.assertIn("disk", p["cap_admit"])                      # the admission's own verdict: blocked by the disk floor
        p["pueue"] = {"groups": {"jobs": {"status": "Running", "parallel_tasks": 4}}, "tasks": {
            "1": {"group": "jobs", "status": {"Running": {}}, "label": "unit:u"}, "2": {"group": "jobs", "status": {"Queued": {}}, "label": "unit:u"}}}
        p["wait_jobs"] = "1"
        self.assertEqual([k for k, _ in janus.lane_findings(cfg, {"here": p}) if k.startswith("idle:")], [])
        p["wait_jobs"] = "0"; p["cap_admit"] = "yes"   # nothing held by admission, it would admit one, pueue's limit holds the queue: idle
        p["pueue"]["groups"]["jobs"]["parallel_tasks"] = 1
        self.assertIn("idle:here:jobs", [k for k, _ in janus.lane_findings(cfg, {"here": p})])

class Memory(unittest.TestCase):
    def test_remember_forget_and_findings(self):
        import argparse
        d, cfg = project('\n[memory]\ncap_lines = 2\n')
        R = lambda text, kind="decision", **k: janus.cmd_remember(cfg, argparse.Namespace(text=text, source=k.get("source"), key=k.get("key"), learned=None,
                                                                                     **{x: x == kind for x in janus.MEM_SECTIONS}))
        with self.assertRaises(janus.Err): R("the cache lives on the build host", "fact")          # a fact needs a source
        (d / "docs").mkdir(); (d / "docs/n.md").write_text("x")
        R("the cache lives on the build host", "fact", source="docs/n.md")
        with self.assertRaises(janus.Err): R("the cache lives on the build host", "fact", source="docs/n.md")   # exact duplicate
        R("tests run on the mac", "pitfall", key="tests where"); R("tests run on linux only", "pitfall", key="tests where")
        es = janus.memory_read(cfg)
        self.assertEqual([e[3] for e in es if e[2] == "tests-where"], ["tests run on linux only"])   # replaced, not beside
        self.assertIn("tests run on the mac", cfg.f("memory-history.md").read_text())
        self.assertEqual(janus.memory_findings(cfg), [])
        R("a third one"); (d / "docs/n.md").unlink()
        keys = {k for k, _ in janus.memory_findings(cfg)}
        self.assertIn("memory-long", keys); self.assertTrue(any(k.startswith("memory-source:") for k in keys))
        janus.cmd_forget(cfg, argparse.Namespace(text="a third")); self.assertEqual(len(janus.memory_read(cfg)), 2)
        hd = janus.brief_header(cfg, "here", cfg.hosts["here"], "u1", d, True)   # every agent's brief starts with it
        self.assertIn("Session memory (curated by the orchestrator; your brief wins on conflict)", hd)
        self.assertIn("tests run on linux only", hd); self.assertIn("peers.md", hd); self.assertIn("janus transcript", hd)
        rh = janus.brief_header(cfg, "far", {"root": "/r", "ssh": "x"}, "u2", "/r/worktrees/u2", False)
        self.assertIn("tests run on linux only", rh); self.assertIn("/r/board/peers.md", rh)

    def test_rule_subject(self):
        S = janus.rule_subject
        self.assertEqual(S("claude-api-on-the-new-key-cap-175"), S("claude-api-on-the-new-key-keychain-janus"))   # one subject, two rules
        self.assertNotEqual(S("keep-build-output-in-the-shared-build-cache"), S("keep-herdr-tidy-one-labelled-tab-per-running"))

class Peers(unittest.TestCase):
    def test_peers_file_and_overlap(self):
        d, cfg = project(); (d / "board").mkdir(); cfg.run.ro = False
        g = lambda *a, cwd: subprocess.run(["git", *a], cwd=cwd, capture_output=True, check=True)
        repo = d / "repo"; repo.mkdir(); g("init", "-q", "-b", "main", cwd=repo); (repo / "a.txt").write_text("a\n"); (repo / "b.txt").write_text("b\n")
        g("add", ".", cwd=repo); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=repo)
        units = []
        for i, f in (("1", "a.txt"), ("2", "a.txt"), ("3", "b.txt")):
            w = d / f"wt{i}"; g("worktree", "add", "-q", "-b", f"u{i}", str(w), cwd=repo); (w / f).write_text("changed\n")
            units.append({"id": i, "name": f"u{i}", "host": "here", "model": "m", "cwd": str(w), "state": "working"})
        F = janus.peer_findings(cfg, units, {})
        self.assertEqual([k for k, _ in F], ["overlap:u1:u2"])
        txt = (d / "board/peers.md").read_text()
        self.assertIn("u3 | host here", txt); self.assertIn("branch u1", txt); self.assertIn("files: a.txt", txt)
        units[1]["state"] = "done"; self.assertEqual(janus.peer_findings(cfg, units, {}), [])   # a finished unit is no peer

class Transcript(unittest.TestCase):
    def test_condense_claude_and_codex_with_grep_and_tail(self):
        d = Path(tempfile.mkdtemp())
        cl = [{"type": "user", "message": {"role": "user", "content": "do the task"}},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": "running it"}, {"type": "tool_use", "name": "Bash", "input": {"command": "make test"}}]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "linker error: undefined symbol foo"}]}]}},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": "fixed by adding the lib"}]}}]
        cx = [{"type": "session_meta", "payload": {"cwd": "/w/x"}},
              {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "codex says hi"}]}},
              {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["ls", "-l"]})}}]
        (d / "c.jsonl").write_text("".join(json.dumps(x) + "\n" for x in cl)); (d / "x.jsonl").write_text("".join(json.dumps(x) + "\n" for x in cx))
        run = lambda *a: subprocess.run([sys.executable, "-", *a], input=janus.CONDENSE, capture_output=True, text=True).stdout
        o = run("ids", "", "0", "0", "", str(d / "c.jsonl"))
        for x in ("USER: do the task", "AGENT: running it", "> Bash: make test", "< ERROR linker error", "AGENT: fixed by"): self.assertIn(x, o)
        self.assertEqual(run("ids", "undefined symbol", "0", "0", "", str(d / "c.jsonl")).strip().splitlines()[1:], ["< ERROR linker error: undefined symbol foo"])
        self.assertEqual(run("ids", "", "1", "0", "", str(d / "c.jsonl")).strip().splitlines()[1:], ["AGENT: fixed by adding the lib"])
        o = run("codex-cwd", "", "0", "0", "/w/x", str(d / "*.jsonl"))
        self.assertIn("AGENT: codex says hi", o); self.assertIn("> shell: ls -l", o); self.assertNotIn("do the task", o)

class Board(unittest.TestCase):
    """The shipped board: question ids that stay until answered, waiting never SILENT, broadcast, learned posts, the alert."""
    def test_questions_waiting_broadcast_learned_alert(self):
        d, cfg = project(); cfg.run.ro = False
        janus.board_upkeep(cfg, [])   # .master, .hosts, .notify
        bd = d / "board"; self.assertTrue((bd / ".master").is_file()); self.assertIn("watch --alert", (bd / ".notify").read_text())
        (bd / ".notify").write_text("true\n")
        B = lambda *a, **env: janus.board(cfg, *a, env=env)
        B("post", "a1", "status", "start"); B("post", "a2", "status", "start")
        B("post", "a1", "question", "which port?"); B("post", "a1", "status", "carrying on")   # still needs an answer
        B("post", "a2", "waiting", "here:3")
        old = "2000-01-01 00:00"; c = bd / "agents/a2.md"; c.write_text(c.read_text().replace(c.read_text().split("- updated: ")[1].strip(), old))
        rows = janus.board_rows(cfg)
        self.assertEqual(rows["a2"]["state"], "waiting"); self.assertFalse(rows["a2"]["silent"])   # parked: never SILENT
        self.assertEqual([k for k, _ in janus.board_findings(cfg, {"a1", "a2"})], ["question:q1"])
        sent = []; real = janus.herdr
        janus.herdr = lambda *a, **k: sent.append(a) or {}
        try:
            cfg.f("pane").write_text("p1"); logs = []
            self.assertIn("QUESTION [q1] from a1: which port?", janus.board_alerts(cfg, logs.append))
            self.assertIsNone(janus.board_alerts(cfg, logs.append))          # told once
        finally: janus.herdr = real
        self.assertEqual(sent[0][:3], ("agent", "prompt", "p1"))
        self.assertIn("broadcast to 2", B("direct", "all", "freeze main")[1])
        self.assertTrue(B("questions")[1].startswith("q1"))   # a broadcast answers no question
        B("answer", "q1", "8080"); self.assertEqual(B("questions")[1], "")
        self.assertIn("[q1] 8080", B("read", "a1")[1]); self.assertIn("freeze main", B("read", "a2")[1])
        B("post", "a2", "learned", "the cache is in /var/c (source: docs/c.md)")
        self.assertEqual([k for k, _ in janus.board_findings(cfg, set())], ["learned:a2:1"])
        janus.learned_mark(cfg, "learned:a2:1", "rejected"); self.assertEqual(janus.board_findings(cfg, set()), [])
        self.assertIn("asked: 1", B("digest", "--since", "1h")[1])

    def test_remote_rule_on_a_fresh_board(self):
        """A remote unit launched before any check got this machine's board path in its rule (exit 127 there)."""
        d, cfg = project('\n[hosts.far]\nssh = "x@far"\nroot = "/r"\n')
        p = janus.agent_prompt(cfg, "u", "/b.md", "far")
        self.assertIn("B=/r/bin/board.sh", p); self.assertIn("$B post u <kind> <text>", p)
        self.assertNotIn(str(janus.JANUS_SELF.parent), p)

    def test_board_contract_is_python_with_one_validator(self):
        """Step 3b: board.sh is a thin shim over the Python module; one name validator refuses path names and '..'."""
        d, cfg = project(); cfg.run.ro = False
        names = dict(janus.board_shipped(cfg))
        self.assertEqual(set(names), {"board.sh", "msgboard.py"})
        self.assertIn("msgboard.py", names["board.sh"])          # the shim execs the module next to it
        self.assertIn("- state: open", names["msgboard.py"])     # the card schema and the question ids live here (one place)
        for bad in ("../evil", "a/b", "..", "."):
            rc, o, e = janus.board(cfg, "post", bad, "status", "x"); self.assertEqual(rc, 2, bad); self.assertIn("bad agent name", e)


class ReviewFixes9(unittest.TestCase):
    """review-fixes9 findings: each test failed before its fix."""
    def fake_remote(self, d):
        """A fake ssh (runs the command here; an `rm` of a sending file is lost, as on a flaky link) and a no-op rsync."""
        b = d / "fakebin"; b.mkdir()
        (b / "ssh").write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do case $1 in -n) shift;; -o) shift 2;; *) break;; esac; done\nshift\n'
                               'case "$*" in "rm -f "*) exit 0;; esac\nexec sh -c "$*"\n')
        (b / "rsync").write_text("#!/bin/sh\nexit 0\n")
        for f in b.iterdir(): f.chmod(0o755)
        return str(b) + ":" + os.environ["PATH"]

    def test_sync_merges_a_surviving_sending_file_once(self):
        """#1: locked()'s local f shadowed merge()'s $f, so .merged-<host> recorded the lock path and a sending file that
        survived its rm was merged again (one question, two ids)."""
        r = Path(tempfile.mkdtemp(prefix="janus-rem-"))
        d, cfg = project(f'\n[hosts.far]\nssh = "far"\nroot = "{r}"\n'); cfg.run.ro = False
        janus.board_upkeep(cfg, []); (janus.board_dir(cfg) / ".notify").write_text("true\n")
        (r / "board-outbox").mkdir(); (r / "board-outbox/outbox.log").write_text("2026-01-01 10:00 eve question one question\n".replace("question one", "question: one"))
        env = {"PATH": self.fake_remote(d)}
        janus.board(cfg, "sync", env=env); janus.board(cfg, "sync", env=env)
        self.assertEqual(len(janus.board_questions(cfg)), 1, janus.board_questions(cfg))

    def test_posts_that_wait_unsynced_are_reported_not_silent(self):
        """Issue 266: a remote agent's posts sat in its host's outbox for minutes with no sign anywhere (the board looked
        stopped). The host's board says how long its oldest post has waited, tells the agent, and check raises board:stale-sync."""
        r = Path(tempfile.mkdtemp(prefix="janus-rem-")); (r / "bin").mkdir()
        d, cfg = project(f'\n[hosts.far]\nssh = "far"\nroot = "{r}"\n')
        for n, t in janus.board_shipped(cfg): (r / "bin" / n).write_text(t)
        b = r / "bin/board.sh"; b.chmod(0o755)
        run = lambda *a: subprocess.run([str(b), *a], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(run("post", "eve", "question", "which", "branch?").stderr, "")
        self.assertEqual(run("outbox").stdout.strip(), "0")   # a fresh post is not late
        ob = r / "board-outbox/outbox.log"; ob.write_text(time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 600)) + ob.read_text()[16:])
        self.assertIn("board:stale-sync", run("read", "eve").stderr)
        line = next(l for l in janus.probe_extra({**cfg.hosts["far"], "_rd": str(r / "runs")}).splitlines() if "outbox_min" in l)
        out = subprocess.run(["sh", "-c", line], capture_output=True, text=True).stdout.strip()
        self.assertRegex(out, r"^outbox_min=(9|10)$")
        self.assertEqual([k for k, _ in janus.sync_findings(cfg, {"far": {"outbox_min": out.split("=")[1]}})], ["board:stale-sync"])
        self.assertEqual(janus.sync_findings(cfg, {"far": {"outbox_min": "3"}, "old": {}}), [])   # within the watcher's cadence; an older shipped board says nothing

    def test_close_withdraws_open_questions(self):
        """#3: closing an agent's card left its open questions open: question:<id> fired forever."""
        d, cfg = project(); cfg.run.ro = False; janus.board_upkeep(cfg, []); (janus.board_dir(cfg) / ".notify").write_text("true\n")
        janus.board(cfg, "post", "carol", "question", "still running?"); self.assertEqual(len(janus.board_questions(cfg)), 1)
        janus.board(cfg, "close", "carol"); self.assertEqual(janus.board_questions(cfg), [])


    def test_parked_unit_that_cannot_resume_is_reported(self):
        """#2: a parked unit whose resume raised stayed parked with no finding (the watch log was the only trace)."""
        d, cfg = project(); (d / "out/u").mkdir(parents=True); (d / "out/u/REPORT.md").write_text("VERDICT: PENDING\nWAITING here:7\n")
        u = {"id": "x-1", "name": "u", "host": "here", "via": "pueue", "model": "m", "rcwd": str(d / "nowhere"), "started": 0, "_park": ["here:7"],
             "parked": {"jobs": ["here:7"], "since": 0, "by": "report"}}
        hs = {"here": {"ok": True, "pueue": {"tasks": {"7": {"status": {"Done": {"result": "Success"}}}}}}}
        saved = []; real = (janus.Beads.save, janus.Beads.comment)
        janus.Beads.save = lambda s, x: saved.append(dict(x)); janus.Beads.comment = lambda s, *a: None
        try: logs = []; janus.tend_parked(cfg, hs, [u], logs.append)
        finally: janus.Beads.save, janus.Beads.comment = real
        self.assertIn("no session id", u.get("resume_error", {}).get("why", ""))
        self.assertTrue(saved and "resume_error" in saved[-1])
        f = janus.parked_findings(cfg, hs, u, ["here:7"])
        self.assertEqual([k for k, _ in f], ["parked:u:resume"]); self.assertIn("no session id", f[0][1])

    def test_landed_needs_the_units_change_on_the_tip(self):
        """#5: a unit whose tree equals an OLD commit's tree on the branch (it reverts a change main still has) counted as landed."""
        d, cfg = project('\n[landing]\nbranch = "main"\n'); r = d / "repo"; r.mkdir(); wt = d / "wt"
        g = lambda *a, cwd=r: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main"); (r / "a").write_text("1\n"); g("add", "a"); g("commit", "-qm", "base")
        (r / "a").write_text("2\n"); g("commit", "-qam", "main: a=2")
        g("worktree", "add", "-q", "-b", "wk/u", str(wt))
        (wt / "a").write_text("1\n"); g("commit", "-qam", "u: revert a", cwd=wt)     # its tree is the base commit's tree
        u = {"name": "u", "cwd": str(wt)}
        self.assertIs(janus.landed(cfg, u), False)
        (r / "a").write_text("1\n"); g("commit", "-qam", "main: the same revert")   # the change is on the tip now: nothing to land
        self.assertIs(janus.landed(cfg, u), True)

    def test_remote_transcript_dir_uses_the_expanded_cwd(self):
        """#4: a remote rcwd under $HOME gave the slug -HOME-...; Claude Code's folder is the expanded absolute path's slug."""
        d, cfg = project('\n[hosts.far]\nssh = "x@far"\nroot = "$HOME/o"\n')
        u = {"name": "u", "host": "far", "rcwd": "$HOME/o/worktrees/u", "rcwd_abs": "/home/me/o/worktrees/u"}
        self.assertIn("/.claude/projects/-home-me-o-worktrees-u/", janus.transcript_where(cfg, u))
        sl = janus.remote_slug_sh({"rcwd": "$HOME/o/worktrees/u"})
        o = subprocess.run(["sh", "-c", f"HOME=/home/me; echo {sl}"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(o, "-home-me-o-worktrees-u")

class GatePolicy(unittest.TestCase):
    """fixes10 item 2: known failures (PASS with only known failures; findings for entries with no open unit or that pass now),
    a per-gate time limit, a quick slot by expected length, and the affected-scope hook."""
    def setUp(self):
        self.d, self.cfg = project('\n[landing]\nknown_failures = "out/known.tsv"\nquick_gate_min = 10\nscope_cmd = "sh scope.sh {unit} {tree}"\n')
        (self.d / "out/known.tsv").write_text("# id cols, cause, unit\nsuite\tflaky_a\tissue 1\tfix-a\nsuite\tstale_b\told\t-\n")
        self.cfg.run.ro = False

    def test_judge_and_default_runner(self):
        g = self.d / "out/g1"; g.mkdir()
        self.assertEqual(janus.judge_gate(self.cfg, "g1", 1)[0], "FAIL")          # names no failure: a build error, a timeout
        (g / "failed.txt").write_text("suite/flaky_a\n"); self.assertEqual(janus.judge_gate(self.cfg, "g1", 1)[:2], ("PASS", []))
        (g / "failed.txt").write_text("suite/stale_b\n"); self.assertEqual(janus.judge_gate(self.cfg, "g1", 1)[0], "FAIL")   # no fixing unit
        (g / "failed.txt").write_text("suite/flaky_a\nsuite/new_c\n"); self.assertEqual(janus.judge_gate(self.cfg, "g1", 1)[1], ["suite/new_c"])
        (g / "tests.sh").write_text('echo suite/flaky_a > "$JANUS_GATE_OUT/failed.txt"; exit 1\n')
        log = self.d / "out/gates.log"
        r = subprocess.run(["sh", "-c", janus.GATE_DEFAULT, "gate", str(g / "tests.sh"), "g1", str(log), sys.executable, str(JANUS_PY)],
                           env={**os.environ, "JANUS_ROOT": str(self.d)}, cwd=self.d, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("KNOWN=1 RESULT: PASS", log.read_text())
        log.write_text("2026-01-01 00:00:00 g1 EXIT_tests=1 RESULT: FAIL\n"); os.utime(g / "failed.txt", (0, 0))
        self.assertEqual(janus.gate_results(self.cfg)["g1"][1], "FAIL")   # an old failed.txt does not re-judge a newer line
        (g / "failed.txt").write_text("suite/flaky_a\n"); log.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + " g1 EXIT_tests=1 RESULT: FAIL\n")
        self.assertEqual(janus.gate_results(self.cfg)["g1"][1], "PASS")   # a project runner's FAIL with known failures only

    def test_known_failure_findings(self):
        keys = [k for k, _ in janus.known_failure_findings(self.cfg, {"fix-a"})]
        self.assertEqual(keys, ["known-failure:suite/stale_b"])                     # no unit named
        self.assertIn("known-failure:suite/flaky_a", [k for k, _ in janus.known_failure_findings(self.cfg, set())])   # its unit is not open
        g = self.d / "out/g2"; g.mkdir(); (g / "passed.txt").write_text("suite/flaky_a\n")
        (self.d / "out/gates.log").write_text(time.strftime("%Y-%m-%d %H:%M:%S") + " g2 EXIT_tests=0 RESULT: PASS\n")
        self.assertIn("known-failure:suite/flaky_a:passes", [k for k, _ in janus.known_failure_findings(self.cfg, {"fix-a"})])

    def test_timeout_and_quick_slot(self):
        a = type("A", (), {"name": "land-u1-x", "timeout_min": 0.01, "command": ["--", "sleep 30"]})()
        t0 = time.time(); self.assertEqual(janus.cmd_gate_exec(self.cfg, a), 124); self.assertLess(time.time() - t0, 15)
        self.assertIn("land-u1-x EXIT_timeout=124 RESULT: FAIL", (self.d / "out/gates.log").read_text())
        self.assertIsNone(janus.gate_estimate(self.cfg, "land-u1-x"))               # a stopped run is not a length
        a.timeout_min = 0; a.command = ["true"]; self.assertEqual(janus.cmd_gate_exec(self.cfg, a), 0)
        self.assertIsNotNone(janus.gate_estimate(self.cfg, "land-u9-x"))              # same family, another unit
        self.assertIsNone(janus.gate_estimate(self.cfg, "other"))
        self.assertIn("gates-quick", janus.ensure_lanes(self.cfg.hosts["here"], self.cfg))

    def test_scope_hook(self):
        tree = self.d / "tree"; tree.mkdir(); (tree / "f").write_text("x\n")
        (self.d / "scope.sh").write_text('echo "# steps for $1"; echo "lint: test -f f"; echo "unit: test $1 = u1"\n')
        st = janus.scope_steps(self.cfg, "u1", tree)
        self.assertEqual(st, [(None, "lint", "test -f f"), (None, "unit", "test u1 = u1")])
        r = subprocess.run(["bash", "-c", janus.scope_script(self.cfg, "u1", tree, st)], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.split()), (0, ["EXIT_lint=0", "EXIT_unit=0"]))
        (self.d / "scope.sh").write_text('echo "@nohost x: true"\n')
        with self.assertRaises(janus.Err): janus.scope_steps(self.cfg, "u1", tree)

class Thrash(unittest.TestCase):
    """fixes10 item 3: a shared host over thrash_psi for thrash_min: this project's newest admitted job is stopped and re-queued
    elsewhere; jobs not recorded by this project (or another task with the same script) are never touched."""
    def test_stop_newest_own_job_and_requeue(self):
        d, cfg = project('\n[hosts.far]\nssh = "x@far"\nroot = "/r"\nshared = true\nthrash_psi = 30\nthrash_min = 5\n'); cfg.run.ro = False
        jd = cfg.f("runs") / "jobs"; jd.mkdir(parents=True)
        for jid, tid, at in (("old-1", "1", 100), ("new-2", "2", 200)):
            (jd / f"{jid}.json").write_text(json.dumps({"host": "far", "task": tid, "lane": "jobs", "unit": "u", "wt": str(d), "cmd": "make", "at": at}))
        (jd / "copied-4.json").write_text(json.dumps({"host": "far", "task": "99", "wt": str(d), "cmd": "x", "at": 999}))   # not this task
        run = lambda jid, start: {"status": {"Running": {"start": start}}, "command": f"bash /r/runs/{jid}.sh", "label": "unit:u@t", "group": "jobs"}
        hs = {"far": {"ok": True, "psi": "55.0", "pueue": {"tasks": {"1": run("old-1", "2026-01-01T10:00:00"), "2": run("new-2", "2026-01-01T11:00:00"),
                       "3": run("theirs-3", "2026-01-01T12:00:00"), "4": run("copied-4", "2026-01-01T13:00:00")}}},
              "here": {"ok": True}}
        calls = []; saved = (janus.cancel_job, janus.cmd_run, janus.host_cap, janus.board)
        janus.cancel_job = lambda cfg, ref, force=False: calls.append(("cancel", ref)) or [f"cancelled {ref}"]
        janus.cmd_run = lambda cfg, a: calls.append(("run", a.host, a.command, a.unit)) or print("here:9")
        janus.host_cap = lambda *a: {"free": 1, "pause": ""}; janus.board = lambda *a, **k: (0, "", "")
        try:
            self.assertEqual(janus.tend_thrash(cfg, hs, [], lambda m: None), [])     # over the limit, not yet for thrash_min
            self.assertEqual(calls, [])
            cfg.jput("thrash.json", {"far": int(time.time()) - 600})
            self.assertIn("would far thrashing", janus.tend_thrash(cfg, hs, [], lambda m: None, dry=True)[0]); self.assertEqual(calls, [])
            out = janus.tend_thrash(cfg, hs, [], lambda m: None)
            self.assertEqual(calls, [("cancel", "far:2"), ("run", "here", ["make"], "u")], out)   # the newest of ours, not theirs-3/copied-4
            self.assertEqual([k.split(":")[0] for k, _ in janus.thrash_findings(cfg)], ["thrash"])
            hs["far"]["psi"] = "5"; janus.tend_thrash(cfg, hs, [], lambda m: None); self.assertNotIn("far", cfg.jget("thrash.json"))
        finally: janus.cancel_job, janus.cmd_run, janus.host_cap, janus.board = saved

    def test_a_shared_host_paused_by_pressure_thrashes_without_a_thrash_psi(self):
        """Catches (item 19): a shared host whose memory pressure paused admission (PSI over psi_pause, default 10) did not
        thrash, because the threshold was a fixed 40: a paused host kept a job for an hour and nothing stopped it."""
        d, cfg = project('\n[hosts.far]\nssh = "x@far"\nroot = "/r"\nshared = true\n'); cfg.run.ro = False
        hs = {"far": {"ok": True, "psi": "20", "pueue": {"tasks": {}}}}
        cfg.jput("thrash.json", {"far": int(time.time()) - 600})                 # over the threshold for 10 min
        out = janus.tend_thrash(cfg, hs, [], lambda m: None, dry=True)
        self.assertTrue(any("no job of this project runs there" in l for l in out), out)   # over the threshold, not "fine"
        self.assertEqual(janus.thrash_cfg(cfg, cfg.hosts["far"])[0], 10)         # psi_pause, not the fixed 40

class RemoteBrief(unittest.TestCase):
    """fixes10 item 4: every absolute path a brief names reaches a remote agent: its tree's paths become its copy's, files under
    the project root are copied to <root>/ctx/<unit>/, other paths must exist on the host, else the launch is refused."""
    def test_paths_rewritten_copied_or_refused(self):
        r = Path(tempfile.mkdtemp(prefix="janus-rem-"))
        d, cfg = project(f'\n[hosts.far]\nssh = "nowhere"\nroot = "{r}"\n'); h = cfg.hosts["far"]
        wt = d / "wt"; (wt / "src").mkdir(parents=True); (d / "out/rev").mkdir(parents=True); (d / "out/rev/REPORT.md").write_text("x\n")
        real = janus.hrun; janus.hrun = lambda h, script, **k: real({"local": True}, script, **k)
        try:
            text = (f"Worktree `{wt}`, run `python3 {wt}/src/x.py`. Findings: {d}/out/rev/REPORT.md. B = `{d}`. Write {d}/out/u/REPORT.md.\n"
                    f"Shell is /bin/sh; see https://example.com/a/b and $HOME/x and <root>/bin/board.sh and s/a/b/.\n")
            t2, copy, notes = janus.brief_plan(cfg, h, "u", wt, text)
            self.assertIn(f"`{r}/worktrees/u`", t2); self.assertIn(f"python3 {r}/worktrees/u/src/x.py", t2)
            self.assertIn(f"{r}/ctx/u/out/rev/REPORT.md", t2); self.assertEqual(copy, [(str((d / "out/rev/REPORT.md").resolve()), "out/rev/REPORT.md")])
            self.assertIn(f"Write {r}/out/u/REPORT.md", t2); self.assertIn(f"B = `{r}/ctx/u`", t2); self.assertTrue(notes)
            self.assertIn("/bin/sh;", t2); self.assertIn("https://example.com/a/b", t2); self.assertIn("<root>/bin/board.sh", t2)
            self.assertNotIn(str(d), t2)
            t3, copy, _ = janus.brief_plan(cfg, h, "u", wt, f"B = `{d}`. Read B/out/rev/REPORT.md and B/out/none.md.")
            self.assertEqual([x for _, x in copy], ["out/rev/REPORT.md"])   # relative to the named root: copied, B points at ctx
            with self.assertRaises(janus.Err) as e: janus.brief_plan(cfg, h, "u", wt, text + f"Also {d.parent}/elsewhere-xyz/f.md and /no/such/path.\n")
            self.assertIn("/no/such/path", str(e.exception)); self.assertIn("elsewhere-xyz", str(e.exception))
        finally: janus.hrun = real

class Tidy(unittest.TestCase):
    """fixes10 item 5: janus tidy removes clean, landed, unused worktrees, panes of closed units and gone cards of no open unit;
    the hook judges a loop by the commands inside it and allows janus close --force on a unit's own tree."""
    def test_tidy_plan_and_run(self):
        d, cfg = project('\n[landing]\nbranch = "main"\n[paths]\nworktrees = "wts"\n'); r = d / "repo"; r.mkdir(); w = d / "wts"; w.mkdir()
        g = lambda *a, cwd=r: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main"); (r / "a").write_text("1\n"); g("add", "a"); g("commit", "-qm", "base")
        for n in ("landed", "dirty", "unlanded", "inuse", "untracked"): g("worktree", "add", "-q", "-b", f"wk/{n}", str(w / n))
        (w / "dirty/a").write_text("2\n"); (w / "untracked/new").write_text("x\n")
        (w / "unlanded/b").write_text("b\n"); g("add", "b", cwd=w / "unlanded"); g("commit", "-qm", "b", cwd=w / "unlanded")
        cfg.run.all_units = {"old": {"status": "closed"}, "live": {"status": "open"}}
        ag = {"old": {"pane_id": "p1", "agent_status": "idle", "workspace_id": "w1"}, "live": {"pane_id": "p2", "workspace_id": "w1"}, "orchestrator": {"pane_id": "p0", "workspace_id": "w1"}}
        rows = {"ghost": {"gone": True}, "live": {"gone": True}, "fine": {"gone": False}}
        (d / "board/agents").mkdir(parents=True); (d / "board/agents/ghost.md").write_text("# ghost\n- project: t\n")
        real = janus.ws_id; janus.ws_id = lambda cfg, create=False: "w1"
        try: P = janus.tidy_plan(cfg, [{"name": "live", "cwd": str(w / "inuse")}], ag, rows)
        finally: janus.ws_id = real
        self.assertEqual([(k, Path(t).name if k == "worktree" else t) for k, t, _ in P], [("worktree", "landed"), ("pane", "p1"), ("card", "ghost")])

    def test_hook_loops_and_force_close(self):
        d, cfg = project(); j = lambda c: janus.judge(cfg, c, str(d), janus.edit_ok_dirs(cfg))
        self.assertIsNone(j("git merge -q origin/main && git push -q; for w in worktrees/a worktrees/b; do janus ack x \"y\"; janus status; done"))
        self.assertIsNone(j("git -C worktrees/a merge origin/main; for p in 1 2; do echo $p; done"))   # the merge is not in the loop
        for c in ("for w in worktrees/*; do git -C $w merge origin/main; done", "for w in worktrees/*; do janus close $w; done",
                  "ls worktrees | xargs -I{} git -C {} add -A", "while read w; do rm $w/f; done < worktrees.txt"):
            self.assertIsNotNone(j(c), c)
        wt = d / "wt"; wt.mkdir(); units = [{"id": "x-1", "name": "u", "cwd": str(wt)}, {"id": "x-2", "name": "v", "cwd": str(d)}]
        real = janus.Beads.units; janus.Beads.units = lambda s: units
        try:
            self.assertIsNone(j("janus close u --force --reason 'capped; continued elsewhere'"))
            self.assertIsNotNone(j("janus close v --force"))             # the project root: shared
            units.append({"id": "x-3", "name": "w", "cwd": str(wt)}); self.assertIsNotNone(j("janus close u --force"))   # shared tree
            self.assertIsNotNone(j("janus close nosuch --force"))
        finally: janus.Beads.units = real

class Fixtures(unittest.TestCase):
    """fixes10 item 6: a live check skips the suite's process fixtures (marked by their root); the suite still sees them."""
    def test_marked_processes_are_skipped_live(self):
        d, cfg = project()
        self.assertIn(janus.FIXTURE_MARK, str(d))                          # every fixture root carries the mark
        ps = (f"501 1 501 0.0 01:00 cargo test -p x --manifest-path {d}/Cargo.toml\n"
              "502 1 502 0.0 01:00 cargo test -p y --manifest-path /w/real/Cargo.toml\n")
        self.assertEqual(len(janus.proc_findings(cfg, ps)), 2)              # the suite sees its own fixtures
        os.environ.pop("JANUS_IN_TESTS")
        try:
            self.assertEqual([k for k, _ in janus.proc_findings(cfg, ps)], ["localtest:502"])   # a live check does not
            s = d / "loop.sh"; s.write_text("#!/bin/bash\nsleep 30\n"); p = subprocess.Popen(["/bin/bash", str(s)]); time.sleep(1.5)
            try:
                with open(s, "a") as f: f.write("echo x\n")
                time.sleep(1.1); self.assertFalse([k for k, _ in janus.script_findings() if k.startswith(f"script:{p.pid}:")])
                self.assertNotIn(f"{janus.FIXTURE_MARK}", janus.probe_extra({"_rd": "/x", "local": True, "schedulers": ["sleep"]}).split("grep -v")[0])
                self.assertIn(f"-e {janus.FIXTURE_MARK}", janus.probe_extra({"_rd": "/x", "local": True, "schedulers": ["sleep"]}))
            finally: p.kill(); p.wait()
        finally: os.environ["JANUS_IN_TESTS"] = "1"

class TidyAll(unittest.TestCase):
    """fixes10 item 5b (owner core rule): tidy covers branches (local and remote), run files, caches over a disk budget, test
    roots, our stopped containers, remote copies of closed units; check raises disk:<host> and tidy:<n>."""
    def test_branches_local_and_remote(self):
        d, cfg = project('\n[landing]\nbranch = "main"\n[paths]\nworktrees = "wts"\n[tidy]\nbranches = ["wk/*"]\n'); r = d / "repo"; r.mkdir()
        g = lambda *a, cwd=r: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main"); (r / "a").write_text("1\n"); g("add", "a"); g("commit", "-qm", "base")
        rem = d / "rem.git"; subprocess.run(["git", "init", "-q", "--bare", str(rem)]); g("remote", "add", "origin", str(rem))
        for b in ("wk/merged", "wk/squashed", "wk/open", "wk/live", "other/x"): g("branch", b)
        g("checkout", "-q", "wk/squashed"); (r / "s").write_text("s\n"); g("add", "s"); g("commit", "-qm", "s")
        g("checkout", "-q", "wk/open"); (r / "o").write_text("o\n"); g("add", "o"); g("commit", "-qm", "o")
        g("checkout", "-q", "main"); (r / "s").write_text("s\n"); g("add", "s"); g("commit", "-qm", "squash-land s")
        g("push", "-q", "origin", "wk/merged", "wk/squashed"); g("branch", "-f", "wk/squashed", "wk/squashed")
        (d / "wts").mkdir(); g("worktree", "add", "-q", str(d / "wts/live"), "wk/live")
        P = janus.tidy_branches(cfg, [])
        got = sorted((k, t.rpartition(":")[2]) for k, t, *_ in P)
        self.assertEqual(got, [("branch", "wk/merged"), ("branch", "wk/squashed"), ("remote-branch", "origin/wk/merged"), ("remote-branch", "origin/wk/squashed")])
        for k, t, *_ in P: self.assertEqual(janus.tidy_do(cfg, k, t)[0], 0, t)
        self.assertEqual(g("branch", "--list", "wk/*").stdout.split(), ["+", "wk/live", "wk/open"])
        self.assertEqual(subprocess.run(["git", "-C", str(rem), "branch"], capture_output=True, text=True).stdout.strip(), "")

    def test_host_items_budget_roots_and_findings(self):
        d, cfg = project(); cfg.run.ro = False; h = cfg.hosts["here"]; c = d / "cache"; c.mkdir()
        for n, age in (("old", 9e5), ("new", 0)):
            (c / n).mkdir(); (c / n / "blob").write_bytes(b"x" * 1500000); os.utime(c / n, (time.time() - age,) * 2)
        rd = Path(h["_rd"]); rd.mkdir(parents=True); (rd / "j1.log").write_text("x"); os.utime(rd / "j1.log", (time.time() - 30 * 86400,) * 2)
        (rd / "j2.log").write_text("x")
        h.update(cache_dirs=[str(c)], disk_budget_gb=0.002)   # 2 MB budget, 3 MB of cache: the oldest entry goes
        P = janus.tidy_host(cfg, "here", h, [], {})
        kinds = [(k, Path(t.split(":", 1)[-1]).name if k == "cache" else t) for k, t, *_ in P]
        self.assertIn(("disk", "here"), kinds); self.assertIn(("cache", "old"), kinds); self.assertNotIn(("cache", "new"), kinds)
        self.assertIn(("run-files", "here"), kinds)
        for k, t, *_ in P:
            if k != "disk": self.assertEqual(janus.tidy_do(cfg, k, t)[0], 0)
        self.assertFalse((c / "old").exists()); self.assertTrue((c / "new").exists())
        self.assertFalse((rd / "j1.log").exists()); self.assertTrue((rd / "j2.log").exists())
        self.assertEqual(janus.tidy_do(cfg, "cache", f"here:{d}/elsewhere")[0], 1)          # never outside cache_dirs
        self.assertEqual(janus.tidy_do(cfg, "test-root", str(d.parent))[0], 1)             # never a root that is not a test root
        self.assertEqual(janus.tidy_do(cfg, "test-root", str(d))[0], 1); self.assertTrue(d.is_dir())   # (nor one nested elsewhere)
        old = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-old-", dir="/tmp")); os.utime(old, (time.time() - 2 * 86400,) * 2)
        self.assertIn(str(old), [t for k, t, *_ in janus.tidy_roots(cfg)]); janus.tidy_do(cfg, "test-root", str(old)); self.assertFalse(old.exists())
        cfg.jput("tidy.json", {"at": int(time.time()), "plan": [list(x) for x in P]})
        self.assertEqual(sorted(k.split(":")[0] for k, _ in janus.tidy_findings(cfg)), ["disk", "tidy"])

class Reconcile(unittest.TestCase):
    """fixes10 item 7: runs whose agent is gone with no done row are found, marked done unknown (budget as the upper bound),
    shown by janus report, and no longer raised once reconciled."""
    def test_reconcile(self):
        d, cfg = project(); pv = cfg.prov["api"]; L = d / "out/ledger.tsv"; old = "2026-01-01 00:00:00"; new = janus.tstamp()
        L.write_text("".join(f"{t}\t{u}\trunning\t{b}\t{k}\n" for t, u, b, k in (
            (new, "closed", 2, "closed.r1"), (new, "live", 1, "live.r1"), (new, "live", 1, "live.r2"), (new, "ended", 3, "ended.r1"),
            (old, "ghost", 4, "ghost.r1"), (new, "fresh", 5, "fresh.r1"))))
        cfg.run.all_units = {"closed": {"name": "closed", "status": "closed", "run": "closed.r1"},
                          "live": {"name": "live", "status": "in_progress", "run": "live.r2", "runs": ["live.r1", "live.r2"]},
                          "ended": {"name": "ended", "status": "in_progress", "run": "ended.r1"}}
        units = [{"name": "live", "run": "live.r2", "runs": ["live.r1", "live.r2"], "state": "working"}, {"name": "ended", "run": "ended.r1", "state": "failed"}]
        got = {k: why for k, _, _, why in janus.unreconciled(cfg, pv, units)}
        self.assertEqual(sorted(got), ["closed.r1", "ended.r1", "ghost.r1", "live.r1"], got)
        for k, un, b, _ in janus.unreconciled(cfg, pv, units): janus.ledger_done(cfg, pv, k, un, b, "unknown")
        self.assertIn("\t4.0\tdone unknown\tghost.r1", L.read_text())
        self.assertEqual(janus.unreconciled(cfg, pv, units), []); self.assertEqual(janus.ledger_read(cfg, pv)[1], [])   # valid ledger
        self.assertAlmostEqual(janus.committed(cfg, pv), 2 + 1 + 1 + 3 + 4 + 5)          # the bound stays counted
        outs = {k: o for _, k, _, _, o, _ in janus.run_outcomes(cfg)}
        self.assertEqual(outs["ghost.r1"], "reconciled"); self.assertEqual(outs["live.r2"], "open")

class Generic(unittest.TestCase):
    def test_no_project_details_in_code(self):
        """No project, host or path names in the skill: code, tests, board, docs (the project's own history lives in the project).
        The forbidden names are not in this public repo: one regex per line in a local untracked file, $JANUS_LEAK_WORDS or
        $XDG_CONFIG_HOME/janus/leak-words (default ~/.config/janus/leak-words); '#' lines are comments."""
        import re
        src = Path(os.environ.get("JANUS_LEAK_WORDS") or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "janus/leak-words")
        if not src.is_file(): self.skipTest(f"no leak-words file ({src}): the leak check needs the local list of names the skill must not contain")
        words = [l.strip() for l in src.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
        if not words: self.skipTest(f"{src} lists no words")
        bad = re.compile("|".join(f"(?:{w})" for w in words), re.I)
        here = JANUS_PY.parent; me = Path(__file__).resolve()
        for f in [JANUS_PY, here / "install", here / "board.sh", here / "check", me, here / "README.md", here / "example.janus.toml",
                  *sorted((here / "skill").glob("*")), *sorted((here / "docs").glob("*.md")), *sorted((here / "launchd").glob("*"))]:
            if f.is_file():
                hits = [f"{f.name}:{i}" for i, l in enumerate(f.read_text(errors="replace").splitlines(), 1) if bad.search(l)]
                self.assertEqual(hits, [], f"project details in {f}")

class Migrate(unittest.TestCase):
    """janus migrate: one-time, idempotent move of a project and the toolkit install from orch to janus."""
    def test_moves_config_state_hooks_and_watcher_once(self):
        d = Path(tempfile.mkdtemp(prefix="janus-mig-")); (d / "orch.toml").write_text("[project]\nname = 't'\n")
        (d / ".orch").mkdir(); (d / ".orch/HANDOFF.md").write_text("old notes\n")
        home = Path(tempfile.mkdtemp(prefix="janus-home-"))
        (home / ".claude").mkdir()
        (home / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/u/.local/bin/orch hook Stop"}]}]}}))
        (home / "Library/LaunchAgents").mkdir(parents=True); (home / "Library/LaunchAgents/com.orch.watch.plist").write_text("old plist\n")
        calls = []; real_run = janus.run; janus.run = lambda *a, **k: (calls.append(a[0][0]), 0, "", "")[1:]
        old_home = os.environ.get("HOME"); os.environ["HOME"] = str(home)
        try:
            self.assertEqual(janus.migrate(str(d)), 0)
            self.assertTrue((d / "janus.toml").is_file()); self.assertFalse((d / "orch.toml").exists())
            self.assertTrue((d / ".janus/HANDOFF.md").is_file()); self.assertFalse((d / ".orch").exists())
            cmd = json.loads((home / ".claude/settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertIn("janus hook Stop", cmd); self.assertNotIn("/orch hook", cmd)
            if sys.platform == "darwin":
                self.assertFalse((home / "Library/LaunchAgents/com.orch.watch.plist").exists())
                self.assertTrue((home / "Library/LaunchAgents/com.janus.watch.plist").is_file()); self.assertIn("launchctl", calls)
            self.assertEqual(janus.migrate(str(d)), 0)   # idempotent: nothing left to do, still exit 0
            self.assertTrue((d / "janus.toml").is_file())
        finally:
            janus.run = real_run
            if old_home is None: os.environ.pop("HOME", None)
            else: os.environ["HOME"] = old_home

    def test_config_values_naming_the_old_state_are_rewritten(self):
        """Catches (item 1): migrate renamed .orch/ to .janus/ but left [project] state = ".orch", so the next janus check read
        .orch/HANDOFF.md and crashed."""
        d = Path(tempfile.mkdtemp(prefix="janus-mig-"))
        (d / "orch.toml").write_text('[project]\nname = "t"\nstate = ".orch"   # where the state lives\n'
                                     '[paths]\ngate_log = ".orch/gates.log"\n')
        (d / ".orch").mkdir(); (d / ".orch/HANDOFF.md").write_text("old notes\n")
        home = Path(tempfile.mkdtemp(prefix="janus-home-"))
        old_home = os.environ.get("HOME"); os.environ["HOME"] = str(home)
        real_run, real_sh = janus.run, janus.sh
        janus.run = lambda *a, **k: (0, "", ""); janus.sh = lambda *a, **k: (1, "", "")
        try: self.assertEqual(janus.migrate(str(d)), 0)
        finally:
            janus.run, janus.sh = real_run, real_sh
            if old_home is None: os.environ.pop("HOME", None)
            else: os.environ["HOME"] = old_home
        t = (d / "janus.toml").read_text()
        self.assertIn('state = ".janus"', t); self.assertIn('gate_log = ".janus/gates.log"', t)
        self.assertIn("# where the state lives", t, "the comment was rewritten")   # comments are left as written
        self.assertNotIn(".orch", t)

    def test_values_that_must_stay_are_not_rewritten_and_continuation_lines_are(self):
        """Catches (finding 1): rewrite_config replaced a bare `orch` token in any value (a path, host, keychain name or word),
        and left a multi-line array's continuation lines alone."""
        d = Path(tempfile.mkdtemp(prefix="janus-mig-"))
        (d / "orch.toml").write_text(
            '[project]\nname = "t"\nstate = ".orch"\n'
            '[hosts.alpha]\nroot = "/home/dev/orch"\n'
            '[hosts.beta]\nroot = "/srv/fleet/orch"\n'
            '[paths]\ngate_log = ".orch/gates.log"\n'
            'scripts = [\n  ".orch/bin/lane.sh",\n  "bin/orch.py",\n]\n'
            '[misc]\nssh = "me@orch"\nkey_keychain = "orch"\nenv = "ORCH_API"\npy = "scripts/torch.py"\ndoc = "docs/research.orchard.md"\n')
        (d / ".orch").mkdir(); (d / ".orch/HANDOFF.md").write_text("old notes\n")
        home = Path(tempfile.mkdtemp(prefix="janus-home-"))
        old_home = os.environ.get("HOME"); os.environ["HOME"] = str(home)
        real_run, real_sh = janus.run, janus.sh
        janus.run = lambda *a, **k: (0, "", ""); janus.sh = lambda *a, **k: (1, "", "")
        try: self.assertEqual(janus.migrate(str(d)), 0)
        finally:
            janus.run, janus.sh = real_run, real_sh
            if old_home is None: os.environ.pop("HOME", None)
            else: os.environ["HOME"] = old_home
        t = (d / "janus.toml").read_text()
        self.assertIn('state = ".janus"', t); self.assertIn('gate_log = ".janus/gates.log"', t)
        self.assertIn('".janus/bin/lane.sh"', t); self.assertIn('"bin/janus.py"', t)   # continuation lines of a multi-line array
        for keep in ('root = "/home/dev/orch"', 'root = "/srv/fleet/orch"', 'ssh = "me@orch"',
                     'key_keychain = "orch"', 'env = "ORCH_API"', 'py = "scripts/torch.py"', 'doc = "docs/research.orchard.md"'):
            self.assertIn(keep, t, f"a value that must stay was rewritten: {keep}")
        # idempotent: a second migrate rewrites nothing
        os.environ["HOME"] = str(home); janus.run = lambda *a, **k: (0, "", ""); janus.sh = lambda *a, **k: (1, "", "")
        try: self.assertEqual(janus.migrate(str(d)), 0)
        finally:
            janus.run, janus.sh = real_run, real_sh
            if old_home is None: os.environ.pop("HOME", None)
            else: os.environ["HOME"] = old_home
        self.assertEqual(t, (d / "janus.toml").read_text())

    def test_dry_run_changes_nothing(self):
        """Catches (item 3): migrate had no --dry-run, so a project could not see what the state-changing command would do."""
        d = Path(tempfile.mkdtemp(prefix="janus-mig-"))
        (d / "orch.toml").write_text('[project]\nname = "t"\nstate = ".orch"\n')
        (d / ".orch").mkdir(); (d / ".orch/HANDOFF.md").write_text("old notes\n")
        home = Path(tempfile.mkdtemp(prefix="janus-home-")); (home / ".claude").mkdir()
        (home / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/u/.local/bin/orch hook Stop"}]}]}}))
        old_home = os.environ.get("HOME"); os.environ["HOME"] = str(home)
        calls = []; real_run, real_sh = janus.run, janus.sh
        janus.run = lambda *a, **k: (calls.append(a[0][0]), 0, "", "")[1:]; janus.sh = lambda *a, **k: (1, "", "")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out): self.assertEqual(janus.migrate(str(d), dry=True), 0)
        finally:
            janus.run, janus.sh = real_run, real_sh
            if old_home is None: os.environ.pop("HOME", None)
            else: os.environ["HOME"] = old_home
        self.assertTrue((d / "orch.toml").is_file()); self.assertTrue((d / ".orch/HANDOFF.md").is_file())
        self.assertFalse((d / "janus.toml").exists())
        cmd = json.loads((home / ".claude/settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn("/orch hook", cmd, "dry-run rewrote the hooks file"); self.assertNotIn("launchctl", calls, "dry-run touched the watcher")
        s = out.getvalue()
        self.assertIn("orch.toml -> janus.toml", s); self.assertIn(".orch/ -> .janus/", s); self.assertIn("hook command(s) -> janus", s)

    def test_adopts_pre_existing_items(self):
        """Catches (item 6): jobs, sessions and cards made before the project id existed were left as machine state and never
        collected, cancelled or tracked."""
        d = Path(tempfile.mkdtemp(prefix="janus-mig-")); (d / "janus.toml").write_text('[project]\nname = "t"\nid = "proj"\n')
        (d / "board/agents").mkdir(parents=True)
        (d / "board/agents/u1.md").write_text("# u1\n- task: t\n- state: working\n- project: \n")
        (d / "board/agents/zzz.md").write_text("# zzz\n- task: t\n- state: working\n- project: \n")
        tasks = {"7": {"label": "unit:u1", "status": "Queued", "path": str(d)},
                 "8": {"label": "unit:u9", "status": "Running", "path": str(d)},
                 "9": {"label": "gate:g@proj", "status": "Running", "path": str(d)},
                 "10": {"label": "unit:u1", "status": "Queued", "path": "/tmp/another-project"}}
        calls = []
        def fake_sh(cmd, **k):
            calls.append(list(cmd))
            b = os.path.basename(cmd[0])
            if b == "pueue" and cmd[1:2] == ["status"]: return 0, json.dumps({"tasks": tasks}), ""
            if b == "pueue" and cmd[1:2] == ["edit"]:   # pueue applies the label the editor wrote
                tasks[cmd[2]]["label"] += "@proj"; return 0, "", ""
            if cmd[:2] == ["tmux", "list-sessions"]: return 0, "janus-u2\norch-u1\njanus-zzz\n", ""
            return 0, "", ""
        real_sh, real_units = janus.sh, janus.all_units
        janus.sh = fake_sh; janus.all_units = lambda cfg: {"u1": {}, "u2": {}, "u3": {}}
        try:
            out = janus.adopt_items(d, dry=False, config=d / "janus.toml")
        finally:
            janus.sh, janus.all_units = real_sh, real_units
        s = "\n".join(out)
        self.assertIn("adopt: job 7: unit:u1 -> unit:u1@proj", s)
        self.assertIn("adopt: job 8 (unit:u9): not one of this project's units", s)
        self.assertIn("adopt: session janus-u2 -> janus-u2@proj", s)
        self.assertIn("adopt: session orch-u1 -> janus-u1@proj", s)
        self.assertIn("adopt: session janus-zzz: not one of this project's units", s)
        self.assertIn("adopt: card u1: project proj", s); self.assertIn("adopt: card zzz: not one", s)
        self.assertIn("- project: proj", (d / "board/agents/u1.md").read_text())
        self.assertEqual(sorted(tuple(c) for c in calls if c[:2] == ["tmux", "rename-session"]),
                         [("tmux", "rename-session", "-t", "janus-u2", "janus-u2@proj"),
                          ("tmux", "rename-session", "-t", "orch-u1", "janus-u1@proj")])
        edits = [c[1:] for c in calls if os.path.basename(c[0]) == "pueue" and c[1:2] == ["edit"]]
        self.assertEqual(edits, [["edit", "7"]], "another project's job was touched")

    def test_adopt_dry_run_changes_nothing(self):
        d = Path(tempfile.mkdtemp(prefix="janus-mig-")); (d / "janus.toml").write_text('[project]\nname = "t"\nid = "proj"\n')
        (d / "board/agents").mkdir(parents=True); (d / "board/agents/u1.md").write_text("# u1\n- state: working\n- project: \n")
        calls = []
        def fake_sh(cmd, **k):
            calls.append(list(cmd))
            b = os.path.basename(cmd[0])
            if b == "pueue" and cmd[1:2] == ["status"]: return 0, json.dumps({"tasks": {"7": {"label": "unit:u1", "status": "Queued", "path": str(d)}}}), ""
            if cmd[:2] == ["tmux", "list-sessions"]: return 0, "janus-u1\n", ""
            return 0, "", ""
        real_sh, real_units = janus.sh, janus.all_units
        janus.sh = fake_sh; janus.all_units = lambda cfg: {"u1": {}}
        try: s = "\n".join(janus.adopt_items(d, dry=True, config=d / "janus.toml"))
        finally: janus.sh, janus.all_units = real_sh, real_units
        self.assertIn("job 7: unit:u1 -> unit:u1@proj", s); self.assertIn("session janus-u1 -> janus-u1@proj", s)
        self.assertFalse(any(c[:2] == ["pueue", "edit"] for c in calls), "dry-run relabelled a job")
        self.assertFalse(any(c[:2] == ["tmux", "rename-session"] for c in calls), "dry-run renamed a session")
        self.assertNotIn("project: proj", (d / "board/agents/u1.md").read_text(), "dry-run wrote the card")


class CheckMissingState(unittest.TestCase):
    def test_missing_handoff_is_a_finding_not_a_crash(self):
        """Catches (item 2): check crashed with FileNotFoundError .orch/HANDOFF.md when git records a state file the disk lost."""
        d = Path(tempfile.mkdtemp(prefix="janus-handoff-"))
        (d / "janus.toml").write_text('[project]\nname = "t"\nstate = ".orch"\n[hosts.here]\nlocal = true\n')
        (d / ".orch").mkdir(); (d / ".orch/HANDOFF.md").write_text("notes\n")
        g = lambda *x: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *x], capture_output=True, check=True)
        g("init", "-q"); g("add", "."); g("commit", "-qm", "a")
        (d / ".orch/HANDOFF.md").unlink()   # on disk gone, still a tracked file in git (the live crash)
        cfg = janus.Cfg(d)
        keys = {k for k, _ in janus.git_findings(cfg)}   # no crash; the deleted state file is just reported
        self.assertIn("git:dirty", keys)
        real = janus.watcher_loaded; janus.watcher_loaded = lambda *a: True
        try: keys = {k for k, _ in janus.session_findings(cfg, 0)}
        finally: janus.watcher_loaded = real
        self.assertIn("handoff:missing", keys)


class DoctorProjectListed(unittest.TestCase):
    """item 4: doctor said 'ok project not listed in ~/.config/janus/projects' while the project IS listed."""
    def _doctor(self, d, listed):
        home = Path(tempfile.mkdtemp(prefix="janus-home-"))
        (home / ".config/janus").mkdir(parents=True); (home / ".config/janus/projects").write_text(str(d) + "\n" if listed else "")
        old = os.environ.get("HOME"); os.environ["HOME"] = str(home)
        real = {k: getattr(janus, k) for k in ("sh", "watcher_loaded", "watcher_service")}
        janus.sh = lambda *a, **k: (0, "", ""); janus.watcher_loaded = lambda *a: True; janus.watcher_service = lambda: ("launchd", "com.janus.watch")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out): janus.doctor(janus.Cfg(d), type("A", (), {"no_hosts": True})())
        finally:
            for k, v in real.items(): setattr(janus, k, v)
            if old is None: os.environ.pop("HOME", None)
            else: os.environ["HOME"] = old
        return next(l for l in out.getvalue().splitlines() if "listed" in l)

    def _project(self):
        d = Path(tempfile.mkdtemp(prefix="janus-doc-")); (d / "janus.toml").write_text('[project]\nname = "t"\n[hosts.here]\nlocal = true\n'); return d


    def test_unlisted_project_is_warned(self):
        line = self._doctor(self._project(), False)
        self.assertTrue(line.startswith("WARN "), line); self.assertIn("not listed in", line); self.assertIn("will not visit", line)

    def test_the_line_follows_the_file_the_watcher_reads(self):
        """Catches (the doctor line): the doctor judged listing by `projects()` (which drops a listed dir without a janus.toml)
        instead of the projects file the watcher actually reads, so a project the watcher will visit could read as 'not listed'
        (and the earlier release printed 'ok project not listed', a self-contradiction)."""
        d = self._project()
        real = janus.projects; janus.projects = lambda: []
        try: line = self._doctor(d, True)
        finally: janus.projects = real
        self.assertTrue(line.startswith("ok "), line); self.assertIn("project listed in", line); self.assertNotIn("not listed", line)


class SkillPointers(unittest.TestCase):
    def test_a_release_copy_missing_a_pointed_path_is_caught(self):
        """Catches (finding 15): SkillPointers passed in a release copy missing docs/REFERENCE.md, because without `install` it
        had nothing to compare (PARTS) and a missing path was skipped."""
        import ast, shutil
        root = JANUS_PY.parent; skill = root / "skill/SKILL.md"
        self.assertTrue(skill.is_file(), skill)
        state = (".janus", ".orch", "out", "briefs", "worktrees", "board", ".beads", "ctx", "run", "base")
        def missing_in(base):
            """Paths SKILL.md names that a copy at `base` does not hold (a project's own state paths are not shipped files)."""
            inst = base / "install"; parts = None
            if inst.is_file():
                parts = next(ast.literal_eval(n.value) for n in ast.parse(inst.read_text()).body
                             if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "PARTS")
            out = set()
            for m in re.finditer(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]*[A-Za-z0-9]", (base / "skill/SKILL.md").read_text()):
                r = m.group(0).rstrip(".,;:)")
                if r.split("/", 1)[0] in state: continue
                if parts is not None and not any(r == p or r.startswith(p + "/") for p in parts): out.add(r); continue
                if not (base / r).exists(): out.add(r)
            return out
        self.assertEqual(sorted(missing_in(root)), [], "SKILL.md names paths the tree does not hold")
        inst = root / "install"
        if inst.is_file():   # build the release copy install would ship and drop a pointed path from it
            parts = next(ast.literal_eval(n.value) for n in ast.parse(inst.read_text()).body
                         if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "PARTS")
            rel = Path(tempfile.mkdtemp(prefix="janus-release-"))
            for p in parts:
                src, dst = root / p, rel / p
                if src.is_dir(): shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                elif src.is_file(): shutil.copy2(src, dst)
            (rel / "docs/REFERENCE.md").unlink()
            self.assertEqual(sorted(missing_in(rel)), ["docs/REFERENCE.md"], "a release copy missing a pointed path passed")


if __name__ == "__main__": unittest.main()
