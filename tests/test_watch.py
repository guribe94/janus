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
        stubs = {"run_check": lambda cfg, cycle=False, writer=True: ([janus.finding("unit:x:finished", "t")], None, {}, []),
                 "herdr": herdr, "new_tab": lambda cfg, n, root: (started.append(n), ("P3", None))[1], "wait_shell": lambda *a: None,
                 "sh": lambda *a, **k: (0, "", "")}
        stubs.update({n: (lambda *a, **k: []) for n in ("heartbeat_beat", "board_alerts", "tend_parked", "tend_autopilot", "tend_thrash", "tend_tidy", "orphan_jobs")})
        with mock.patch.multiple(sys.modules["janus.watch"], **stubs):
            for _ in range(janus.ORCH_WAIT): janus.watch_pass(cfg, lambda m: None)
        self.assertEqual(started, [], "a second orchestrator was started next to the pane it started before")
        self.assertEqual(cfg.f("pane").read_text(), "P2")
        self.assertIn("orchestrator:unregistered", [k for k, _ in janus.orchestrator_findings(cfg)])


if __name__ == "__main__":
    unittest.main()
