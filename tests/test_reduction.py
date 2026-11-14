"""The reduced design's high-signal behaviors: stop without a loop, routing that cannot be bypassed, an unknown host is never a trigger,
admission that does not over-admit, no gate lane, the monitor's filter and read-only root."""
import tempfile, unittest
from pathlib import Path
from unittest import mock

from janusload import conf, load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    conf(d).write_text('[project]\nname = "t"\n[hosts.here]\nlocal = true\n[providers.api]\nenabled = true\n[providers.sub]\nenabled = true\nauth = "subscription"\n'
                       '[models.m]\nprovider = "api"\nkind = "api"\napi_id = "x"\nvendor = "anthropic"\n[models.c]\nprovider = "sub"\nkind = "codex"\nvendor = "openai"\n' + extra)
    return d, janus.Cfg(d)


class Reduction(unittest.TestCase):
    def test_the_orchestrator_stop_blocks_once_over_the_threshold_and_never_otherwise(self):
        d, cfg = project(); cfg.state.mkdir(exist_ok=True)
        with mock.patch.object(janus, "orch_tokens", lambda c: 10):
            self.assertIsNone(janus.stop_reset(cfg, {}))                       # under the threshold: nothing
        with mock.patch.object(janus, "orch_tokens", lambda c: 10**9), mock.patch.dict("os.environ", {"JANUS_IN_TESTS": "1"}):
            self.assertIn("HANDOFF.md", janus.stop_reset(cfg, {}))             # one reminder to update the handoff
            self.assertIsNone(janus.stop_reset(cfg, {"stop_hook_active": True}))   # the re-fired stop passes: no loop, the reset is asked

    def test_a_denied_route_cannot_launch_or_be_substituted(self):
        d, cfg = project('[routing]\nallow_tools = ["claude"]\nallow_auth = ["api_key"]\nnormal = ["c", "m"]\n')
        self.assertFalse(janus.model_ok(cfg, "c")[0]); self.assertIn("allow_tools", janus.model_ok(cfg, "c")[1])
        with self.assertRaises(janus.Err) as e: janus.pick_model(cfg, "normal", "implement")    # the first entry is denied: refused, not a substitute
        self.assertIn("fallback", str(e.exception))
        cfg.route["fallback"] = True; note = []                                             # only the human's explicit setting allows the later entry
        self.assertEqual(janus.pick_model(cfg, "normal", "implement", note=note), "m"); self.assertIn("skipped", note[0])

    def test_a_denied_route_also_blocks_a_resume_and_each_tool_has_its_verified_command_line(self):
        d, cfg = project('[routing]\nallow_tools = ["claude"]\n'); cfg.hosts["here"]["local"] = True
        u = {"name": "u", "host": "here", "model": "c", "via": "herdr"}
        with self.assertRaises(janus.Err) as e: janus.resume_unit(cfg, u, "go")
        self.assertIn("allow_tools", str(e.exception))
        self.assertEqual(janus.tool_argv({"kind": "claude", "model": "opus"}, "p", "s1"), ["claude", "--model", "opus", "--resume", "s1", "p"])
        self.assertEqual(janus.tool_argv({"kind": "codex", "args": ["--x"]}, "p", "s1"), ["codex", "resume", "s1", "--x", "p"])
        self.assertEqual(janus.tool_argv({"kind": "opencode", "model": "a/b"}, "p", "s1"), ["opencode", "run", "--model", "a/b", "--session", "s1", "p"])
        with self.assertRaises(janus.Err): janus.tool_argv({"kind": "gemini"}, "p")           # no unverified guess
        self.assertEqual(set(janus.RESET_CMD), {"claude", "codex", "opencode"})

    def test_fill_uses_the_configured_nested_repository_and_never_a_parent_repository(self):
        import subprocess
        d, cfg = project('[fill]\nrepo = "app"\n'); subprocess.run(["git", "init", "-q", str(d / "app")], check=True)
        self.assertEqual(janus.fill_repo(cfg, "u", d / "none.md"), str((d / "app").resolve()))      # the project folder is no Git root: the nested repo
        d2, cfg2 = project(); subprocess.run(["git", "init", "-q", str(d2)], check=True)
        self.assertEqual(janus.fill_repo(cfg2, "u", d2 / "none.md"), str(d2.resolve()))             # the folder is itself a repository's top
        d3 = Path(tempfile.mkdtemp(prefix="janus-test-")); subprocess.run(["git", "init", "-q", str(d3)], check=True); (d3 / "ws").mkdir()
        d4, cfg4 = project(); import shutil; shutil.move(str(d4 / ".janus"), str(d3 / "ws" / ".janus")); cfg4 = janus.Cfg(d3 / "ws")
        self.assertIsNone(janus.fill_repo(cfg4, "u", d3 / "none.md"))                                # a parent's repository is not found by walking up

    def test_a_removed_or_disabled_model_fails_closed_on_every_resume_path_without_a_prompt_or_process(self):
        d, cfg = project('[providers.off]\nenabled = false\n[models.gone_off]\nprovider = "off"\nkind = "claude"\n')
        acts = []
        with mock.patch.object(janus, "herdr", lambda *a, **k: acts.append(("herdr", a))), mock.patch.object(janus, "X", lambda *a, **k: acts.append(("X", a)) or (0, "", "")), \
             mock.patch.object(janus, "hrun", lambda *a, **k: acts.append(("hrun", a)) or (0, "", "")), mock.patch.object(janus, "api_remote", lambda *a, **k: acts.append(("api", a))):
            for model in ("removed", "gone_off", None):                       # not in the config, a disabled provider, none recorded
                for via in ("herdr", "tmux", "pueue"):
                    u = {"name": "u", "host": "here", "via": via, "model": model, "tmux": "t", "task": 1}
                    with self.assertRaises(janus.Err) as e: janus.resume_unit(cfg, u, "go")
                    self.assertIn("not resumed", str(e.exception))
        self.assertEqual(acts, [])                                           # nothing was prompted, queued or started

    def test_a_resume_releases_the_writer_lock_for_host_io_and_never_starts_twice(self):
        """Catches: the watcher holding the project writer lock through a slow resume, a second pass or a manual resume starting the same
        session while the lock is free, and a watcher pass running beside another."""
        import fcntl, threading
        d, cfg = project(); rec = {"id": "1", "name": "u", "host": "here", "via": "herdr", "model": "c", "kind": "implement", "parked": {"jobs": ["here:5"], "since": 1}}
        class B:
            def __init__(s, c): pass
            def units(s): return [dict(rec)]
            def save(s, u): rec.clear(); rec.update({k: v for k, v in u.items() if k != "state" and not k.startswith("_")})
            def comment(s, *a): pass
        hs = {"here": {"ok": True, "pueue": {"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}}}}
        prompts, free, again, ran = [], [], [], []
        def slow_host_io(*a, **k):                                     # the herdr prompt stands for ssh/tmux/queueing: it takes as long as the host does
            prompts.append(a); f = open(cfg.f("write.lock"), "a")
            try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); free.append(True)   # another command could write now
            except BlockingIOError: free.append(False)
            finally: f.close()
            janus.tend_parked(cfg, hs, [dict(rec, state="idle", _park=["here:5"])], again.append, {})   # a second pass meets the busy mark
            try: janus.resume_outside_lock(cfg, B(cfg), dict(rec), lambda: "again")                    # so does a manual resume
            except janus.Err as e: again.append(str(e))
        with mock.patch.object(janus, "Beads", B), mock.patch.object(janus, "herdr", slow_host_io), mock.patch.object(janus, "job_result", lambda *a: (0, "/x.log")):
            with cfg.writer(): janus.tend_parked(cfg, hs, [dict(rec, state="idle", _park=["here:5"])], again.append, {})
        self.assertEqual(len(prompts), 1, again); self.assertEqual(free, [True])      # lock free during the host I/O; one resume only
        self.assertTrue(any("busy since" in x for x in again) and any("no second resume" in x for x in again), again)
        self.assertNotIn("busy", rec); self.assertEqual(rec.get("handled_jobs"), ["here:5"]); self.assertNotIn("parked", rec)   # state written back under the lock, mark cleared
        import subprocess, sys   # another process holds the pass lock (the lock is re-entrant inside one process)
        other = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f,2); print('held',flush=True); time.sleep(30)", str(cfg.f("watch.lock"))], stdout=subprocess.PIPE, text=True)
        self.addCleanup(other.kill); self.assertEqual(other.stdout.readline().strip(), "held")
        with mock.patch.object(janus, "watch_step", lambda *a, **k: None), mock.patch.object(janus, "sync_ticker", lambda *a: threading.Event()), \
             mock.patch.object(janus, "read_remote", lambda c: ran.append(1)):
            janus.cmd_watch(cfg, type("A", (), {"alert": False, "reset": False})())       # the pass lock is held: this pass is skipped, not run
        self.assertEqual(ran, []); self.assertIn("pass skipped", cfg.f("watch.log").read_text())

    def test_an_unknown_job_state_is_not_done(self):
        self.assertEqual(janus.job_state({"here": {"ok": False}}, "here:5")[0], "unknown")   # tend_parked resumes only on done or gone

    def test_no_gate_lane_and_the_first_job_must_fit_the_host_alone(self):
        self.assertEqual(janus.LANES, ("jobs", "place"))
        d, cfg = project('[capacity]\njob_gb = 64\n'); h = cfg.hosts["here"]; h.update(cores=4, ram_gb=8, reserve_gb=1)
        p = {"ok": True, "cap_rj": "0", "cap_rc": "0", "cap_rm": "0", "cap_ra": "0", "cap_ram": "0", "hist": []}
        self.assertEqual(janus.host_cap(cfg, "here", p, [])["free"], 0)       # a 64 GB job on an 8 GB host: not admitted as the first job either

    def test_the_monitor_filter_keeps_matching_rows_and_counts(self):
        M = janus.Model(); M.rows["Active"] = [janus.Row("a", ("alpha", "working"), detail=["Host: x"]), janus.Row("b", ("beta", "blocked"))]
        M.counts["Active"] = 2
        f = janus.filtered(M, "ALPH"); self.assertEqual([r.key for r in f.rows["Active"]], ["a"]); self.assertEqual(f.counts["Active"], 2)
        self.assertIn("No row matches", janus.filtered(M, "zzz").empty["Active"])


if __name__ == "__main__":
    unittest.main()
