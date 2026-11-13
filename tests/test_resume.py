"""imp-resume: `janus resume` continues the orchestration in a Claude or Codex session with nothing lost.

Each test names the failure it catches. Standard library only; no network, no API spend."""
import contextlib, io, subprocess, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project

def args(**k):
    d = {"unit": None, "runtime": None, "here": False, "message": None, "force": False}
    d.update(k)
    return mock.Mock(**d)

class Takeover(unittest.TestCase):
    def test_two_orchestrator_refusal(self):
        """Catches: starting a second orchestrator when the registered one will not stop (two orchestrators, one state)."""
        d, cfg = project('\n[orchestrator]\ntakeover_wait_s = 0.1\n')
        cfg.f("pane").write_text("w1:p1"); cfg.f("session").write_text("live-sid")
        started = []
        def fake_herdr(*a, **k):
            if a[:2] == ("pane", "list"): return {"panes": [{"pane_id": "w1:p1"}]}
            if a[:2] == ("agent", "get"): return {"agent": {"agent_status": "working"}}
            return {}
        with mock.patch.object(janus, "herdr", fake_herdr), mock.patch.object(janus, "session_alive", lambda cfg, t=None: True), \
             mock.patch.object(janus, "start_orchestrator", lambda cfg, kind: started.append(kind)):
            with self.assertRaises(janus.Err) as e: janus.cmd_resume_orch(cfg, args())
        self.assertIn("two orchestrators", str(e.exception))
        self.assertEqual(started, [])
        self.assertEqual(cfg.f("session").read_text().strip(), "live-sid", "the registration was replaced without taking over")

    def test_a_pending_start_is_not_started_again(self):
        """Catches: a second orchestrator started beside one that was just started and has not registered yet (panel F2/F5)."""
        d, cfg = project()
        cfg.f("pane").write_text("w1:p2"); cfg.f("session").write_text(f"pending:{int(janus.now())}")
        started = []
        def fake_herdr(*a, **k): return {"panes": [{"pane_id": "w1:p2"}]} if a[:2] == ("pane", "list") else {"agent": {"agent_status": "idle"}}
        with mock.patch.object(janus, "herdr", fake_herdr), mock.patch.object(janus, "session_alive", lambda cfg, t=None: False), \
             mock.patch.object(janus, "start_orchestrator", lambda cfg, kind: started.append(kind)):
            with self.assertRaises(janus.Err) as e: janus.cmd_resume_orch(cfg, args())
        self.assertIn("has not", str(e.exception))
        self.assertEqual(started, [])

    def test_a_gone_orchestrator_is_not_waited_for(self):
        """Catches: refusing (or waiting) when the registered orchestrator's pane is absent and its process has ended."""
        d, cfg = project()
        cfg.f("pane").write_text("w1:p1")
        started = []
        with mock.patch.object(janus, "herdr", lambda *a, **k: {"panes": []}), \
             mock.patch.object(janus, "session_alive", lambda cfg, t=None: None), \
             mock.patch.object(janus, "start_orchestrator", lambda cfg, kind: started.append(kind)):
            janus.cmd_resume_orch(cfg, args(runtime="codex"))
        self.assertEqual(started, ["codex"])

class StateSync(unittest.TestCase):
    def git(self, *a, cwd):
        r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, " ".join(a) + ": " + r.stderr)
        return r.stdout

    def test_state_pulled_before_takeover(self):
        """Catches: taking over (or starting) before this machine's state is pulled, so the new orchestrator acts on stale state."""
        d, cfg = project()
        origin = Path(str(d) + "-origin.git"); origin.mkdir()
        self.git("init", "--bare", cwd=origin)
        self.git("init", cwd=d); self.git("config", "user.email", "t@t", cwd=d); self.git("config", "user.name", "t", cwd=d)
        self.git("add", "-A", cwd=d); self.git("commit", "-m", "base", cwd=d)
        self.git("remote", "add", "origin", str(origin), cwd=d)
        self.git("push", "-u", "origin", self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=d).strip(), cwd=d)
        other = Path(str(d) + "-other")   # another machine commits and pushes the state
        self.git("clone", str(origin), str(other), cwd=d)
        self.git("config", "user.email", "o@o", cwd=other); self.git("config", "user.name", "o", cwd=other)
        (other / ".janus").mkdir(); (other / ".janus/remote.txt").write_text("from the other machine\n")
        self.git("add", "-A", cwd=other); self.git("commit", "-m", "other state", cwd=other); self.git("push", cwd=other)
        seen = []
        with mock.patch.object(janus, "takeover", lambda cfg: seen.append((cfg.root / ".janus/remote.txt").exists())), \
             mock.patch.object(janus, "start_orchestrator", lambda cfg, kind: None):
            janus.cmd_resume_orch(cfg, args())
        self.assertEqual(seen, [True], "the state was not pulled before the takeover")
        self.assertTrue((cfg.root / ".janus/remote.txt").exists())

    def test_diverged_state_refuses(self):
        """Catches: a silent merge of two machines' state histories (a hand merge is required)."""
        d, cfg = project()
        origin = Path(str(d) + "-origin.git"); origin.mkdir()
        self.git("init", "--bare", cwd=origin)
        self.git("init", cwd=d); self.git("config", "user.email", "t@t", cwd=d); self.git("config", "user.name", "t", cwd=d)
        self.git("add", "-A", cwd=d); self.git("commit", "-m", "base", cwd=d)
        self.git("remote", "add", "origin", str(origin), cwd=d)
        self.git("push", "-u", "origin", self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=d).strip(), cwd=d)
        other = Path(str(d) + "-other"); self.git("clone", str(origin), str(other), cwd=d)
        self.git("config", "user.email", "o@o", cwd=other); self.git("config", "user.name", "o", cwd=other)
        (other / "remote-only.txt").write_text("theirs\n"); self.git("add", "-A", cwd=other); self.git("commit", "-m", "theirs", cwd=other); self.git("push", cwd=other)
        (d / "local-only.txt").write_text("mine\n"); self.git("add", "-A", cwd=d); self.git("commit", "-m", "mine", cwd=d)   # diverged
        with mock.patch.object(janus, "takeover", lambda cfg: None), \
             mock.patch.object(janus, "start_orchestrator", lambda cfg, kind: None):
            with self.assertRaises(janus.Err) as e: janus.cmd_resume_orch(cfg, args())
        self.assertIn("diverged", str(e.exception))

    def test_state_folder_that_is_its_own_repo(self):
        """Catches: a state folder that is a git repo of its own not committed and pushed (or pushed from the wrong directory)."""
        d, cfg = project()
        st = cfg.state
        origin = Path(str(d) + "-state-origin.git"); origin.mkdir()
        self.git("init", "--bare", cwd=origin)
        self.git("init", cwd=st); self.git("config", "user.email", "t@t", cwd=st); self.git("config", "user.name", "t", cwd=st)
        (st / "HANDOFF.md").write_text("state\n")
        self.git("add", "-A", cwd=st); self.git("commit", "-m", "base", cwd=st)
        self.git("remote", "add", "origin", str(origin), cwd=st)
        self.git("push", "-u", "origin", self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=st).strip(), cwd=st)
        (st / "new.txt").write_text("new state\n")
        with mock.patch.object(janus, "takeover", lambda cfg: None), \
             mock.patch.object(janus, "start_orchestrator", lambda cfg, kind: None):
            janus.cmd_resume_orch(cfg, args())
        shown = self.git("--git-dir", str(origin), "show", "HEAD:new.txt", cwd=d)
        self.assertIn("new state", shown)

class Flow(unittest.TestCase):
    def test_dry_run_starts_nothing_and_changes_nothing(self):
        """Catches: a dry run that starts a session or rewrites the registration."""
        d, cfg = project(); cfg.f("session").write_text("old"); cfg.f("pane").write_text("")
        cfg.run.dry = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(janus, "herdr", lambda *a, **k: {}), \
             mock.patch.object(janus, "pane_tool", lambda: "tmux"):   # the test host's own tools must not decide (a container has neither)
            janus.cmd_resume_orch(cfg, args())
        self.assertEqual(cfg.f("session").read_text().strip(), "old")
        self.assertIn("+ start a claude orchestrator", out.getvalue())

    def test_here_registers_and_hands_over_the_context(self):
        """Catches: an in-place resume that registers but never hands the session the handoff and the rules (item 4)."""
        d, cfg = project()
        janus.handoff_path(cfg).write_text("# H\nnotes-for-the-session\n")
        reg = []
        out = io.StringIO()
        with mock.patch.object(janus, "sh", lambda *a, **k: (1, "", "no bd")), \
             mock.patch.object(janus, "cmd_register", lambda cfg, a: reg.append(a.session)), contextlib.redirect_stdout(out):
            janus.cmd_resume_orch(cfg, args(here=True))
        self.assertEqual(reg, [None])
        self.assertIn("notes-for-the-session", out.getvalue())

    def test_resume_dispatches_unit_or_orchestration(self):
        """Catches: `janus resume <unit>` losing the unit path (or an orchestration resume needing a unit)."""
        d, cfg = project()
        units_called, orch_called = [], []
        with mock.patch.object(janus, "cmd_resume_unit", lambda cfg, a: units_called.append(a.unit) or 0), \
             mock.patch.object(janus, "cmd_resume_orch", lambda cfg, a: orch_called.append(1) or 0):
            janus.cmd_resume(cfg, args(unit="u1")); janus.cmd_resume(cfg, args())
            with self.assertRaises(janus.Err): janus.cmd_resume(cfg, args(unit="u1", here=True))
        self.assertEqual(units_called, ["u1"]); self.assertEqual(len(orch_called), 1)

if __name__ == "__main__":
    unittest.main()
