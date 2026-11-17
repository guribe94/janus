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
        stubs.update({n: (lambda *a, **k: []) for n in ("heartbeat_beat", "board_alerts", "tend_parked", "tend_thrash", "tend_tidy", "orphan_jobs")})
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
        stubs.update({k: (lambda *a, **kw: []) for k in ("heartbeat_beat", "board_alerts", "tend_parked", "tend_thrash", "tend_tidy", "orphan_jobs")})
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

    def test_standby_starts_wakes_and_resumes_nothing_but_keeps_the_board_in_sync(self):
        """Catches: the owner said stop, and the watcher kept waking the orchestrator every 10 min, resuming parked units and
        starting a new orchestrator."""
        acted, synced = [], []
        self.cfg.f("standby.json").write_text('{"since": 1, "reason": "owner: done"}')
        acts = {k: (lambda k: lambda *a, **kw: acted.append(k) or [])(k) for k in ("board_alerts", "tend_parked", "tend_thrash", "tend_tidy", "orphan_jobs", "wake_on_change")}
        with mock.patch.multiple(sys.modules["janus.watch"], board_sync=lambda cfg: synced.append(1), **acts):
            self.run_passes([], 1)
        self.assertEqual((self.started, acted), ([], []), "standby still acted")
        self.assertEqual(synced, [1], "the board was not synced in standby")
        self.cfg.f("standby.json").unlink(); self.run_passes([], 1)
        self.assertEqual(self.started, ["orchestrator"], "off standby the restart came back")


class Tabs(unittest.TestCase):
    """Catches: the workspace filled with tabs of units whose job had ended or whose host was down (uncollected units keep their
    record, so close never ran and the tab stayed)."""
    def test_view_tabs_of_ended_units_close_and_live_or_agent_tabs_stay(self):
        d, cfg = project(); closed, saved, logged = [], [], []
        units = [{"name": "end", "via": "pueue", "state": "done", "tab": "t1", "pane": "p1"},
                 {"name": "down", "via": "pueue", "state": "unknown", "tab": "t2", "pane": "p2"},
                 {"name": "run", "via": "pueue", "state": "working", "tab": "t3", "pane": "p3"},
                 {"name": "agent", "via": "herdr", "state": "done", "tab": "t4", "pane": "p4"}]
        class B:
            def __init__(self, cfg): pass
            def save(self, u): saved.append(u["name"])
        herdr = lambda *a: closed.append(a[2]) if a[:2] == ("tab", "close") else {}
        with mock.patch.multiple(sys.modules["janus.watch"], Beads=B, orphan_tabs=lambda cfg, names: [("t9", "old", "old · m · here")]), \
             mock.patch.multiple(sys.modules["janus.actions.launch"], herdr=herdr), mock.patch.multiple(sys.modules["janus.watch"], herdr=herdr):
            janus.tend_tabs(cfg, units, logged.append)
        self.assertEqual(sorted(closed), ["t1", "t2", "t9"]); self.assertEqual(saved, ["end", "down"])
        self.assertNotIn("tab", units[0]); self.assertEqual(units[2]["tab"], "t3")


class Standby(unittest.TestCase):
    """janus standby closes the support tabs and reopens them on off; status says so; --stop-units stops each open unit."""
    def test_on_and_off(self):
        import argparse, io
        from contextlib import redirect_stdout
        d, cfg = project()
        tabs = [{"tab_id": "t1", "label": "1"}, {"tab_id": "t2", "label": "status"}, {"tab_id": "t3", "label": "board"}, {"tab_id": "t9", "label": "u · m · here"}]
        closed, created, stopped = [], [], []
        def herdr(*a):
            if a[:2] == ("tab", "list"): return {"tabs": [t for t in tabs if t["tab_id"] not in closed]}
            if a[:2] == ("tab", "close"): closed.append(a[2]); return {}
            if a[:2] == ("tab", "create"): created.append(a[a.index("--label") + 1]); return {"root_pane": {"pane_id": "p"}}
            return {}
        class B:
            def __init__(self, cfg): pass
            def units(self): return [{"name": "u", "host": "here", "via": "herdr"}]
        stop = lambda cfg, a: stopped.append((a.unit, a.force))
        ns = lambda **k: argparse.Namespace(**{"action": None, "reason": "done", "stop_units": False, "force": False, **k})
        with mock.patch.multiple(sys.modules["janus.setup"], herdr=herdr, ws_id=lambda cfg, create=False: "W", Beads=B, probe=lambda n, h: {"ok": True}), \
             mock.patch.object(sys.modules["janus.actions.lifecycle"], "cmd_stop", stop), redirect_stdout(io.StringIO()):
            janus.cmd_standby(cfg, ns())
            self.assertEqual(sorted(closed), ["t2", "t3"]); self.assertTrue(janus.standby(cfg)); self.assertEqual(stopped, [])
            janus.cmd_standby(cfg, ns(action="off"))
            self.assertIsNone(janus.standby(cfg)); self.assertEqual(sorted(created), ["board", "status", "watcher"])
            janus.cmd_standby(cfg, ns(stop_units=True))
            self.assertEqual(stopped, [("u", False)])


if __name__ == "__main__":
    unittest.main()
