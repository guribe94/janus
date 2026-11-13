"""The watcher's orchestrator restart (panel-eng F2). Standard library only; herdr and tabs stubbed."""
import os, sys, time, unittest
from unittest import mock

from test_janus import janus, project


class SecondOrchestrator(unittest.TestCase):
    def test_no_second_orchestrator_while_the_started_one_may_still_be_starting(self):
        """Catches (panel-eng F2): the watcher starting another orchestrator while the pane it started last is still there
        (herdr shows no agent yet) and the old session's pid and transcript are dead, leaving two orchestrators on one state."""
        d, cfg = project('\n[orchestrator]\nkind = "claude"\n')
        cfg.f("session_pid").write_text("999999\t00:00\n"); cfg.f("transcript").write_text("/nonexistent/old.jsonl")
        cfg.f("pane").write_text("P2"); cfg.f("session").write_text("pending:1")
        cfg.touch("last_wake"); os.utime(cfg.f("last_wake"), (time.time() - 700,) * 2)
        started = []
        herdr = lambda *a: {"panes": [{"pane_id": "P2"}]} if a[:2] == ("pane", "list") else ({"agent": None} if a[:2] == ("agent", "get") else {})
        stubs = {"run_check": lambda cfg, cycle=False, writer=True, pre=None: ([janus.finding("unit:x:finished", "t")], None, {}, []),
                 "herdr": herdr, "new_tab": lambda cfg, n, root: (started.append(n), ("P3", None))[1], "wait_shell": lambda *a: None,
                 "sh": lambda *a, **k: (0, "", "")}
        stubs.update({n: (lambda *a, **k: []) for n in ("heartbeat_beat", "board_alerts", "tend_parked", "tend_autopilot", "tend_thrash", "tend_tidy", "orphan_jobs")})
        with mock.patch.multiple(sys.modules["janus.watch"], **stubs):
            for _ in range(janus.ORCH_WAIT): janus.watch_pass(cfg, lambda m: None)
        self.assertEqual(started, [], "a second orchestrator was started next to the pane it started before")
        self.assertEqual(cfg.f("pane").read_text(), "P2")
        self.assertIn("orchestrator:unregistered", [k for k, _ in janus.orchestrator_findings(cfg)])


class Restart(unittest.TestCase):
    """The watcher restarts a dead orchestrator (re-check N1): a pending session blocks a start only inside the claim window."""
    def setUp(self):
        self.d, self.cfg = project('\n[orchestrator]\nkind = "claude"\n'); cfg = self.cfg
        cfg.f("session_pid").write_text("999999\t00:00\n"); cfg.f("transcript").write_text("/nonexistent/old.jsonl")
        cfg.f("pane").write_text("P2"); cfg.touch("last_wake"); os.utime(cfg.f("last_wake"), (time.time() - 7200,) * 2)
        self.started, self.closed = [], []

    def run_passes(self, panes, n=1):
        def herdr(*a):
            if a[:2] == ("pane", "list"): return {"panes": [{"pane_id": p} for p in panes]}
            if a[:2] == ("pane", "close"): self.closed.append(a[2]); return {}
            return {"agent": None} if a[:2] == ("agent", "get") else {}
        stubs = {"run_check": lambda cfg, cycle=False, writer=True, pre=None: ([janus.finding("unit:x:finished", "t")], None, {}, []), "herdr": herdr,
                 "new_tab": lambda cfg, n, root: (self.started.append(n), ("P3", None))[1], "wait_shell": lambda *a: None, "sh": lambda *a, **k: (0, "", "")}
        stubs.update({k: (lambda *a, **kw: []) for k in ("heartbeat_beat", "board_alerts", "tend_parked", "tend_autopilot", "tend_thrash", "tend_tidy", "orphan_jobs")})
        with mock.patch.multiple(sys.modules["janus.watch"], **stubs):
            for _ in range(n): janus.watch_pass(self.cfg, lambda m: None); os.utime(self.cfg.f("last_wake"), (time.time() - 7200,) * 2)

    def test_a_pending_session_blocks_a_start_only_inside_the_claim_window(self):
        """Catches (re-check N1): a pending session older than the claim window (which no hook can claim any more) stopping the
        watcher from ever starting an orchestrator again."""
        self.cfg.f("session").write_text(f"pending:{int(time.time()) - 60}:P2"); self.run_passes([], 2)
        self.assertEqual(self.started, [], "started beside a session still inside its claim window")
        self.cfg.f("session").write_text(f"pending:{int(time.time()) - janus.CLAIM_WINDOW - 1}:P2"); self.run_passes([], 1)
        self.assertEqual(self.started, ["orchestrator"], "a stale pending session blocked the restart")

    def test_a_pane_whose_agent_exited_is_closed_and_a_new_orchestrator_started(self):
        """Catches (re-check N1, lost self-healing): the recorded pane still there with only a shell after its agent exited, the
        process dead and the transcript quiet, and the watcher waiting forever instead of closing it and starting a new one."""
        self.cfg.jput("orch_wait.json", {"since": int(time.time()) - 21 * 60, "passes": 5, "why": "x"}); self.run_passes(["P2"])
        self.assertEqual((self.closed, self.started, self.cfg.f("pane").read_text()), (["P2"], ["orchestrator"], "P3"))


if __name__ == "__main__":
    unittest.main()
