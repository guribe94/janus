"""State files survive a crash mid-write (section 7, Step 0 / seam 2).

Cfg.jput writes straight to the file today (janus.py:52-53), so a kill during the write leaves truncated JSON and jget silently
returns {} — every ack, open problem, the 3-strike Stop counter and learned.json reset without a word. This test injects a crash
into the write of a new value and asserts the previous value is still on disk (jput writes a temp file and renames it). The
writer test pins the one writer lock: a second writer process waits, then is refused. Standard library only; no network."""
import argparse, contextlib, io, json, os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path
from unittest import mock

from janusload import conf, load

janus = load()


def project():
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    conf(d).write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        '''))
    return d, janus.Cfg(d)


class State(unittest.TestCase):
    def test_jput_keeps_the_old_value_when_the_write_dies(self):
        d, cfg = project()
        cfg.jput("x.json", {"a": 1})
        p = cfg.f("x.json")
        real = Path.write_text

        def crash(self, data, *a, **k):   # the write of the NEW value dies after a partial write
            if data == '{"a": 2}':
                real(self, '{"a": 2')
                raise OSError("killed mid-write")
            return real(self, data, *a, **k)

        with mock.patch.object(Path, "write_text", crash):
            with self.assertRaises(OSError):
                cfg.jput("x.json", {"a": 2})
        self.assertEqual(json.loads(p.read_text()), {"a": 1}, "a crash mid-write left the state file truncated")
        self.assertEqual([x.name for x in p.parent.iterdir() if x.name.endswith(".tmp")], [], "the dead write left its temp file")

    def test_one_writer_at_a_time(self):
        """Catches: the watcher and the orchestrator's check (or a command) mutating state at once (double resume, lost acks)."""
        d, cfg = project()
        with cfg.writer():
            with cfg.writer(): pass   # re-entrant: the watcher's pass runs the writer check inside its own lock
            code = ("import sys; sys.path.insert(0, %r); from janus.context import Cfg, Err; from pathlib import Path\n"
                    "try:\n with Cfg(Path(%r)).writer(wait=0.5): print('got it')\nexcept Err as e: print('refused', e)") % (str(Path(janus.__file__).parent), str(d))
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
            self.assertIn("refused", out, "a second writer took the state lock while the first held it")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
        self.assertIn("got it", out, "the lock was not released")

    def test_a_slow_host_probe_does_not_hold_the_writer_lock(self):
        """Catches ISSUES 54 (writer lock held across remote I/O): a writer check probed every host inside the state lock, so one
        slow host blocked every ack for the length of its timeout. A local writer must get the lock while the probe is still
        out, and its state change must survive the check's own writes."""
        import threading
        d, cfg = project(); inside, release = threading.Event(), threading.Event()
        def slow_probe(cfg):
            inside.set(); release.wait(30); return {"here": {"ok": False, "name": "here", "err": "slow"}}
        code = ("import sys, json; sys.path.insert(0, %r); from janus.context import Cfg, Err; from pathlib import Path\n"
                "c = Cfg(Path(%r))\n"
                "try:\n with c.writer(wait=3): c.jput('acks.json', {'k': {'t': __import__('time').time(), 'd': 'x'}}); print('got it')\n"
                "except Err as e: print('refused', e)") % (str(Path(janus.__file__).parent), str(d))
        with mock.patch.object(janus, "probe_all", slow_probe):
            t = threading.Thread(target=janus.run_check, args=(cfg,), kwargs={"cycle": False, "writer": True}); t.start()
            try:
                self.assertTrue(inside.wait(30)); out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
            finally: release.set(); t.join(60)
        self.assertIn("got it", out, "a local writer was refused the state lock while a host probe was out")
        self.assertIn("k", cfg.jget("acks.json"), "the check's write lost the ack made while it probed")


    def test_a_slow_backup_of_a_running_remote_unit_does_not_hold_the_writer_lock(self):
        """Catches ISSUES 54 (the periodic backup of a running remote unit, refresh_remote in the watcher's pass, rsynced under
        the state lock): an ack waited for the copy. The copy is made before the lock, once, and the unit is saved under it."""
        import threading
        d, cfg = project(); d2, _ = d, None
        conf(d).write_text(conf(d).read_text() + '\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n'); cfg = janus.Cfg(d)
        inside, release = threading.Event(), threading.Event(); copies = []; saved = []
        u = {"name": "u1", "id": "1", "host": "far", "via": "pueue", "task": 5, "saved_at": 0}
        class Bd:
            def __init__(self, cfg): pass
            def units(self): return [dict(u)]
            def save(self, x): saved.append(x["name"])
            def comment(self, *a): pass
            def issues(self, *a): return []
            def run(self, *a, **k): return []
        def slow_save(cfg, x): copies.append(x["name"]); inside.set(); release.wait(30); return True
        down = {n: {"ok": False, "name": n, "err": "x"} for n in ("here", "far")}
        code = ("import sys; sys.path.insert(0, %r); from janus.context import Cfg, Err; from pathlib import Path\n"
                "try:\n with Cfg(Path(%r)).writer(wait=3): print('got it')\nexcept Err as e: print('refused', e)") % (str(Path(janus.__file__).parent), str(d))
        with mock.patch.object(janus, "probe_all", lambda cfg: down), mock.patch.object(janus, "Beads", Bd), \
             mock.patch.object(janus, "save_output", slow_save), mock.patch.object(janus, "remote_patch", lambda *a, **k: (0, "", "")), \
             mock.patch.object(janus, "herdr", lambda *a, **k: None):
            t = threading.Thread(target=janus.cmd_watch, args=(cfg, argparse.Namespace(alert=False, reset=False))); t.start()
            try: self.assertTrue(inside.wait(30)); out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
            finally: release.set(); t.join(60)
        self.assertIn("got it", out, "an ack was refused the state lock while a running unit's backup was being copied")
        self.assertEqual(copies, ["u1"], "the backup was copied again under the lock")
        self.assertIn("u1", saved, "the fetched backup was never recorded on the unit")


    def test_a_slow_collect_does_not_hold_the_writer_lock_and_records_on_the_current_unit(self):
        """Catches ISSUES 54 (janus collect held the lock for its whole ssh and rsync, up to 600 s): an ack waited for the copy.
        The lock is taken after the remote reads, and the fingerprint is saved on the record read then, not the one read first."""
        import threading
        d, cfg = project()
        conf(d).write_text(conf(d).read_text() + '\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n')
        inside, release = threading.Event(), threading.Event(); saved = []; recs = [{"name": "u1", "id": "1", "host": "far", "cwd": str(d), "v": 1}]
        class Bd:
            def __init__(self, cfg): pass
            def units(self): return [dict(recs[0])]
            def save(self, x): saved.append(x)
            def comment(self, *a): pass
        def slow_save(cfg, x): inside.set(); release.wait(30); recs[0] = {**recs[0], "v": 2}; return True   # another command changes the record meanwhile
        code = ("import sys; sys.path.insert(0, %r); from janus.context import Cfg, Err; from pathlib import Path\n"
                "try:\n with Cfg(Path(%r)).writer(wait=3): print('got it')\nexcept Err as e: print('refused', e)") % (str(Path(janus.__file__).parent), str(d))
        env = {k: v for k, v in os.environ.items() if k != "JANUS_ROLE"}; cwd = os.getcwd(); os.chdir(d)
        with mock.patch.object(janus, "Beads", Bd), mock.patch.object(janus, "save_output", slow_save), mock.patch.object(janus, "remote_patch", lambda *a, **k: (0, "", "")), \
             mock.patch.object(janus, "fingerprint", lambda cfg, u: {"fp": "x"}), mock.patch.object(janus, "X", lambda *a, **k: (0, "", "")), \
             mock.patch.object(janus, "save_transcript", lambda *a: True), \
             mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["janus", "collect", "u1"]):
            from janus.cli import main; t = threading.Thread(target=main); t.start()
            try: self.assertTrue(inside.wait(30)); out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
            finally: release.set(); t.join(60); os.chdir(cwd)
        self.assertIn("got it", out, "an ack was refused the state lock while collect copied a unit's output")
        self.assertEqual([x["v"] for x in saved], [2], "the fingerprint was saved on the stale record read before the copy")


    # ---- launch and close hand the lock back during their remote part (the unit is recorded starting / closing, with a busy mark)
    def _two_phase(self, op, fail=False):
        """Runs janus launch (op 'launch') or close ('close') of unit u in a thread, blocked on an Event inside its remote part, with a
        second process writer as the ack, then lets it finish. Returns (ack's output, the stored records, what the ledger was told,
        the record as the ack-time writer found it)."""
        import threading
        from test_janus import project as jproject
        d, cfg = jproject('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n'); (d / ".janus/briefs/u.md").write_text("do it\n"); (d / "wt").mkdir()
        inside, release = threading.Event(), threading.Event(); recs = {}; calls = []; ledger = []; seen = {}
        if op == "close": recs["1"] = {"name": "u", "id": "1", "host": "far", "via": "tmux", "cwd": str(d / "wt"), "rcwd": "/r/worktrees/u", "run": "u.1"}
        class Bd:
            def __init__(self, cfg): pass
            def units(self): return [dict(x) for x in recs.values()]
            def create(self, *a): return "1"
            def run(self, *a): calls.append(a); return []
            def save(self, x): recs[x["id"]] = dict(x)
            def comment(self, *a): pass
        def X(cfg, h, cmd, **k):
            if ("pueue add" in cmd and not fail) or "rm -rf" in cmd:
                seen.update(recs["1"]); inside.set(); release.wait(30)
                recs["1"] = {**recs["1"], "note": "kept"}   # another command changes the record meanwhile
            return (1, "", "boom") if "pueue add" in cmd and fail else (0, "7\n", "")
        if fail:   # the failing launch blocks on the same hook, then pueue refuses
            X0 = X
            def X(cfg, h, cmd, **k):
                if "pueue add" in cmd: inside.set(); release.wait(30); recs["1"] = {**recs["1"], "note": "kept"}; return 1, "", "boom"
                return 0, "", ""
        hs = {"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "100", "load": "0"}}
        a = argparse.Namespace(unit="u", kind="implement", tier="normal", host="here", model="m", brief=None, cwd=str(d), budget=0.5, test_host=None,
                               allow_main=True, of=None, report=None, resume_of=None, item=None, probes=hs, force=False, handover=False, reason="done", duplicate_of=None)
        code = ("import sys; sys.path.insert(0, %r); from janus.context import Cfg, Err; from pathlib import Path\n"
                "try:\n with Cfg(Path(%r)).writer(wait=3): print('got it')\nexcept Err as e: print('refused', e)") % (str(Path(janus.__file__).parent), str(d))
        fn = janus.cmd_launch if op == "launch" else janus.cmd_close; err = []
        def run():
            try:
                with cfg.writer(), contextlib.redirect_stdout(io.StringIO()): fn(cfg, a)   # the cli's own hold
            except Exception as e: import traceback; err.append(e); err.append(traceback.format_exc()[-1500:])
        patches = [mock.patch.object(janus, n, v) for n, v in (("Beads", Bd), ("X", X), ("herdr", lambda *a, **k: None), ("read_secret", lambda p: "k"),
            ("write_cap", lambda *a: None), ("reserve_budget", lambda *a: "u.1"), ("confirm_started", lambda *a: ("running", "")), ("unit_tab", lambda *a: None),
            ("ledger_done", lambda *a: ledger.append(a[-1])), ("uncollected", lambda *a: None), ("close_tab", lambda *a: None), ("probe", lambda n, h, **k: {"ok": True}),
            ("unit_state", lambda *a, **k: "done"), ("probe_all", lambda cfg: hs),
            ("settle_run", lambda *a: ledger.append("settled")), ("board", lambda *a, **k: (0, "", "")))]
        for pt in patches: pt.start()
        try:
            t = threading.Thread(target=run); t.start()
            try: self.assertTrue(inside.wait(10), err); out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
            finally: release.set(); t.join(60)
        finally:
            for pt in patches: pt.stop()
        return out, recs, ledger, seen, calls, err

    def test_a_slow_launch_does_not_hold_the_writer_lock_and_keeps_concurrent_changes(self):
        """Catches ISSUES 54 (launch rsynced, queued and started under the lock): an ack waited for the host. The unit is recorded
        starting with a busy mark before, and the final save keeps what another command wrote to the record meanwhile."""
        out, recs, ledger, seen, calls, err = self._two_phase("launch")
        self.assertEqual(err, [], err); self.assertIn("got it", out, "an ack was refused the lock while a launch started the agent")
        self.assertEqual((seen["via"], seen["busy"]["op"]), ("starting", "launch"), "the unit was not recorded starting + busy before the remote part")
        r = recs["1"]; self.assertEqual((r["via"], r["task"], r.get("note")), ("pueue", 7, "kept")); self.assertNotIn("busy", r)

    def test_a_failed_launch_after_the_lock_was_let_go_is_rolled_back_on_the_current_record(self):
        """Catches the rollback hole of a two-phase launch: a failure after the remote part must close the unit and keep another
        command's change, not write the stale record back."""
        out, recs, ledger, seen, calls, err = self._two_phase("launch", fail=True)
        self.assertTrue(err and "pueue add failed" in str(err[0]), err)
        r = recs["1"]; self.assertEqual((r["via"], r.get("note")), ("failed", "kept")); self.assertNotIn("busy", r)
        self.assertIn(("close", "1", "--reason", "launch failed"), calls); self.assertIn("launch-failed", ledger, "the budget run was not released")

    def test_a_slow_close_does_not_hold_the_writer_lock_and_collect_refuses_a_closing_unit(self):
        """Catches ISSUES 54 (close removed the host copy under the lock): an ack waited for the host. The unit is marked closing
        + busy first (collect and the sync skip it), and the bd issue is closed after the cleanup."""
        out, recs, ledger, seen, calls, err = self._two_phase("close")
        self.assertEqual(err, [], err); self.assertIn("got it", out, "an ack was refused the lock while close cleaned the host")
        self.assertEqual((seen["closing"], seen["busy"]["op"]), (True, "close")); self.assertIn(("close", "1", "--reason", "done"), calls); self.assertIn("settled", ledger)
        u = dict(seen); self.assertTrue(janus.no_remote_copy(u), "sync and collect would copy a copy that is being removed")

    def test_a_launch_or_close_that_died_is_named_interrupted(self):
        """Catches a stranded unit: a launch or close killed in its remote part left via=starting / closing for ever, unseen."""
        d, cfg = project(); dead = {"op": "close", "pid": 2 ** 22 + 12345, "at": int(janus.now())}
        u = {"name": "u", "id": "1", "host": "here", "via": "tmux", "closing": True, "busy": dead, "started": int(janus.now())}
        F, _ = janus.unit_findings(cfg, {"here": {"ok": True, "name": "here"}}, [u], {})
        self.assertIn("unit:u:interrupted", [k for k, _ in F]); self.assertIsNone(janus.busy_dead({**u, "busy": {**dead, "pid": os.getpid(), "at": 1}}), "a live close is called dead (age alone must not)")


class UnitRecord(unittest.TestCase):
    def test_v1_record_migrates_to_v2(self):
        """A v1 record (launch wrote only run; a resume added runs) reads as v2: every run id in runs, the current one last."""
        r = lambda notes: janus.unit_record({"notes": json.dumps(notes) if isinstance(notes, dict) else notes})
        self.assertEqual(r({"run": "u.1"}), {"run": "u.1", "runs": ["u.1"], "v": 2})
        self.assertEqual(r({"run": "u.r2.9", "runs": ["u.1"]})["runs"], ["u.1", "u.r2.9"])
        self.assertEqual(r({"v": 2, "run": "x", "runs": ["x"], "host": "h"}), {"v": 2, "run": "x", "runs": ["x"], "host": "h"})
        self.assertEqual(r("not json"), {})


if __name__ == "__main__":
    unittest.main()
