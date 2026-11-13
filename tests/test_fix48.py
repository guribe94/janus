"""skill-fix48 (ISSUES rows 231-241): defects a project's evening soak found. One test per failure, each naming what it catches.
Standard library only; no network, no spend. Run by ./check (it discovers tests/test_*.py)."""
import argparse, contextlib, io, json, os, signal, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

import janusload
from test_janus import janus, project


def git(wt, *a): return subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True).stdout


class ProbeCountsWaitingJobs(unittest.TestCase):
    def test_a_job_waiting_for_admission_is_counted(self):
        """Catches row 231: the probe's `cut -d' ' -f1` lost its quotes inside a Python string, the pid read failed, and wait_jobs
        was 0 with jobs waiting (blocked:, waiting:<unit>, placement and capacity never saw them)."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "7.wait").write_text(f"{os.getpid()} 1700000000\n"); Path(d, "8.wait").write_text("999999999 1700000000\n")   # 8: its job is gone
            out = subprocess.run(["sh", "-c", janus.probe_extra({"_rd": d, "local": True})], capture_output=True, text=True).stdout
        self.assertIn("wait_jobs=1\n", out, "the live .wait file is not counted")
        self.assertIn("waitjob=7\n", out)


class PodmanJobUsesTheAccountHome(unittest.TestCase):
    def test_a_podman_job_does_not_inherit_the_callers_home(self):
        """Catches row 232: a job queued by an agent whose HOME is not the account's ran rootless podman on an empty image store
        (exit 125): the job script used the HOME pueue handed it."""
        d, cfg = project()
        a = argparse.Namespace(lane="jobs", subdir=None, usb=False, wt=str(d), broad=None)
        pod = {"ssh": "u@b", "root": "/r", "_rd": "/r/runs", "runner": "podman", "container": {"image": "i", "mount": "/r", "mount_at": "/w"}}
        js = janus.job_script(cfg, "b", pod, a, "t", "make", "t-1")
        self.assertIn(janus.ACCOUNT_HOME, js, "the podman job script keeps the caller's HOME")
        self.assertLess(js.index(janus.ACCOUNT_HOME), js.index("podman run"), "HOME is set after podman runs")
        self.assertLess(js.index(janus.ACCOUNT_HOME), js.index("cap.sh"), "the admission (it lists podman containers) still reads the caller's store")
        import pwd, shutil
        if shutil.which("getent"):
            got = subprocess.run(["sh", "-c", janus.ACCOUNT_HOME + 'echo "$HOME"'], env={**os.environ, "HOME": "/agent/home"}, capture_output=True, text=True).stdout.strip()
            self.assertEqual(got, pwd.getpwuid(os.getuid()).pw_dir)


class OneSyncPerHostAndWorktree(unittest.TestCase):
    def test_two_syncs_of_one_tree_to_one_host_do_not_overlap(self):
        """Catches row 233: a gate's steps placed on one remote host each ran rsync --delete of the same worktree at once and
        removed each other's temp files (exit 23, 9 false step failures)."""
        import threading, time
        d, cfg = project(); h = {"ssh": "u@b", "root": "/r", "_rd": "/r/runs"}; live = []; peak = []; errs = []
        def fake_rsync(cmd, **k):
            live.append(1); peak.append(len(live)); time.sleep(0.15); live.pop(); return subprocess.CompletedProcess(cmd, 0)
        def one():
            try: janus.sync_wt(cfg, "b", h, str(d), "t")
            except Exception as e: errs.append(e)
        with mock.patch("subprocess.run", fake_rsync):
            ts = [threading.Thread(target=one) for _ in range(3)]
            for t in ts: t.start()
            for t in ts: t.join()
        self.assertEqual(errs, []); self.assertEqual(len(peak), 3); self.assertEqual(max(peak), 1, "two rsyncs of one tree ran at once")
        # a failed sync keeps the command's exit and its stderr (the row 141 case: only "failed" reached the operator)
        bad = lambda cmd, **k: subprocess.CompletedProcess(cmd, 255, stderr="Host key verification failed.\n")
        with mock.patch("subprocess.run", bad), self.assertRaises(janus.Err) as c: janus.sync_wt(cfg, "b", h, str(d), "t")
        self.assertIn("exit 255", str(c.exception)); self.assertIn("Host key verification failed", str(c.exception), "the rsync cause was dropped")


class HostFlap(unittest.TestCase):
    REMOTE = {"ssh": "u@b", "root": "/r", "_rd": "/r/runs"}

    def test_a_mux_error_is_retried_on_a_fresh_connection_before_the_host_is_unreachable(self):
        """Catches row 234: `mux_client_request_session: send fds failed` on the probe was reported as host unreachable although
        a fresh ssh connection answered."""
        fresh = []
        def fake_sh(cmd, **k): fresh.append(cmd); return 0, "avail_mb=7\n--pueue\n--tmux\n", ""
        with mock.patch.object(janus, "hrun", lambda h, s, **k: (255, "", "mux_client_request_session: send fds failed")), \
             mock.patch.object(janus, "sh", fake_sh):
            p = janus.probe("b", dict(self.REMOTE))
        self.assertTrue(p["ok"], "a host that answers a fresh connection is called unreachable"); self.assertEqual(p["avail_mb"], "7")
        self.assertIn("ControlPath=none", fresh[0], "the retry reuses the master connection")
        with mock.patch.object(janus, "hrun", lambda h, s, **k: (255, "", "ssh: connect to host b: timed out")), \
             mock.patch.object(janus, "sh", lambda cmd, **k: (255, "", "ssh: connect to host b: timed out")):
            self.assertFalse(janus.probe("b", dict(self.REMOTE))["ok"])   # a host that does not answer either way stays unreachable

    def test_an_ack_survives_a_check_in_which_a_host_did_not_answer(self):
        """Catches row 234 (and the repeat acks of overlap:/related:): a host flap made the findings read from it vanish for one
        check, their acks were dropped, and they came back unacked."""
        d, cfg = project(); t = janus.now()
        cfg.jput("acks.json", {"overlap:a:b": {"t": t, "d": "told"}, "related:a:b": {"t": t, "d": "told"}})
        up = {"ok": True}; down = {"ok": False, "err": "mux"}
        self.assertEqual(set(janus.acks_after(cfg, [], {"here": up, "b": down})), {"overlap:a:b", "related:a:b"}, "a flap dropped the acks")
        self.assertEqual(janus.acks_after(cfg, [], {"here": up, "b": up}), {})   # every host answered and the findings are gone: so are the acks


class TidyKeepsAnOpenUnitsOwnTrees(unittest.TestCase):
    def test_a_worktree_named_after_an_open_unit_with_a_suffix_is_kept(self):
        """Catches row 235: tidy offered to remove <unit>-proof, a clean worktree an open unit made for its own proof jobs
        (worktrees were matched to open units by exact name only)."""
        d, cfg = project('\n[landing]\nbranch = "main"\n[paths]\nworktrees = "wts"\n'); r = d / "repo"; r.mkdir(); w = d / "wts"; w.mkdir()
        git(r, "init", "-q", "-b", "main"); (r / "a").write_text("1\n"); git(r, "add", "a"); git(r, "commit", "-qm", "base")
        for n in ("u1-proof", "u10", "gone"): git(r, "worktree", "add", "-q", "-b", f"wk/{n}", str(w / n))
        cfg.run.all_units = {"u1": {"status": "open"}}; kept = []
        with mock.patch.object(janus, "ws_id", lambda cfg, create=False: "w1"):
            P = janus.tidy_plan(cfg, [{"name": "u1", "cwd": str(d)}], {}, {}, kept)
        self.assertEqual(sorted(Path(t).name for k, t, _ in P if k == "worktree"), ["gone", "u10"], "an open unit's own proof tree is offered for removal")
        self.assertIn("u1-proof", [Path(t).name for t, _ in kept])


class LaunchPlacementNeedsGit(unittest.TestCase):
    def choose(self, brief_text, rooms):
        d, cfg = project('\n[hosts.big]\nssh = "u@big"\nroot = "/r"\n'); b = d / "briefs/u.md"; b.write_text(brief_text)
        a = type("A", (), {"host": None, "test_host": None, "unit": "u", "kind": "feature"})()
        hs = {n: {"name": n, "ok": True} for n in rooms}
        with mock.patch.object(janus, "room", lambda cfg, n, p, units, *x: (rooms[n], "room")), \
             mock.patch.object(janus, "build_room", lambda *x: (True, "build room")), mock.patch.object(janus, "host_cap", lambda *x: {}):
            return janus.choose(cfg, a, b, cfg.models["m"], [], hs)

    def test_a_brief_that_needs_a_push_or_a_landing_gate_goes_to_a_host_with_git_or_launch_warns(self):
        """Catches row 236: a unit whose DONE-WHEN needs a pushed branch and a landing gate was placed on the remote host with
        the most room, where its copy has no git; nothing said so before the agent's first turn."""
        need = "do X\nDONE-WHEN: the branch is pushed and the landing gate passes.\n"
        hn, _, why = self.choose(need, {"here": 1, "big": 9})
        self.assertEqual(hn, "here", "placed on a host without git although a host with git had room"); self.assertNotIn("WARNING", why)
        hn, _, why = self.choose(need, {"here": 0, "big": 9})
        self.assertEqual(hn, "big"); self.assertIn("WARNING", why); self.assertIn("--host here", why)
        hn, _, why = self.choose("do X\nDONE-WHEN: report written.\n", {"here": 1, "big": 9})
        self.assertEqual(hn, "big"); self.assertNotIn("WARNING", why)   # a brief that needs no git still goes where the room is


def alive(pid):
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


class CancelStopsARunnerInItsOwnGroup(unittest.TestCase):
    """Catches row 237: a gate's runner that started its own process group outlived `janus cancel` (and the wrapper's stop): it
    kept the gate's lock and queued more steps."""
    RUNNER = ("import subprocess, sys, time\n"
              "c = subprocess.Popen(['sleep', '300'], start_new_session=True); open(sys.argv[1], 'w').write(str(c.pid)); time.sleep(300)\n")

    def child_pid(self, f):
        t0 = time.time()
        while not (f.is_file() and f.read_text().strip()) and time.time() - t0 < 10: time.sleep(0.1)
        pid = int(f.read_text()); self.addCleanup(lambda: alive(pid) and os.kill(pid, 9)); return pid

    def test_janus_cancel_stops_every_group_of_the_gates_process_tree(self):
        d, cfg = project(); rd = Path(cfg.hosts["here"]["_rd"]); rd.mkdir(parents=True, exist_ok=True)
        root = subprocess.Popen([sys.executable, "-c", self.RUNNER, str(d / "child")], start_new_session=True); self.addCleanup(root.kill)
        child = self.child_pid(d / "child"); (rd / "gate-g-1.pgid").write_text(f"{root.pid}\n")
        task = {"status": {"Running": {}}, "label": janus.tag(cfg, "gate:g"), "group": "gates", "command": "JANUS_WRAP=gate-g-1 janus gate-exec g -- x"}
        with mock.patch.object(janus, "task_status", lambda h, tid: task), mock.patch.object(janus, "save_task_log", lambda *a, **k: None), \
             mock.patch.object(janus, "pueue", lambda h: "true"):
            janus.cancel_job(cfg, "here:5")
        root.wait(timeout=10)
        self.assertFalse(alive(child), "the runner's child in its own process group outlived janus cancel")

    def test_a_signalled_gate_wrapper_stops_every_group_of_its_commands_tree(self):
        d, cfg = project(); (d / "runner.py").write_text(self.RUNNER)
        p = subprocess.Popen([sys.executable, str(janusload.JANUS_PY), "gate-exec", "g", "--", f"{sys.executable} {d / 'runner.py'} {d / 'child'}"],
                             cwd=d, env={**os.environ, "JANUS_ROOT": str(d), "JANUS_WRAP": "gate-g-1"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child = self.child_pid(d / "child"); pgid = Path(cfg.hosts["here"]["_rd"]) / "gate-g-1.pgid"; t0 = time.time()
        while not pgid.is_file() and time.time() - t0 < 10: time.sleep(0.1)
        self.assertTrue(pgid.is_file(), "no group id for janus cancel to stop")   # row 219: what cancel reads
        p.send_signal(signal.SIGTERM); self.assertEqual(p.wait(timeout=40), 128 + signal.SIGTERM)
        self.assertFalse(alive(child), "the runner's child in its own process group outlived the wrapper's stop")
        self.assertFalse((d / "out/gates.log").exists(), "a cancelled gate wrote a result")   # row 219


class RepeatAcks(unittest.TestCase):
    def test_the_handoff_finding_cannot_be_acked(self):
        """Catches row 238: `janus ack handoff ...` hid stale orchestrator notes 12 times in one evening; only writing the notes
        clears the finding, so an ack could only hide it."""
        d, cfg = project()
        with self.assertRaises(janus.Err) as e: janus.cmd_ack(cfg, argparse.Namespace(problem="handoff", decision="seen", decision_class=False))
        self.assertIn("HANDOFF.md", str(e.exception))
        cfg.jput("acks.json", {"handoff": {"t": janus.now(), "d": "seen"}}); self.assertEqual(janus.live_acks(cfg, {"handoff"}), {})

    def test_a_condition_that_returns_by_nature_earns_no_issue_row(self):
        """Catches row 238: three acks of a host's memory pressure or of a repository that new work made dirty again filed
        'fix the root cause' rows (and an issue:<key> finding) although nothing in janus was to fix."""
        d, cfg = project("\n[janus]\nack_issue_n = 3\n")
        cfg.jput("ack-counts.json", {"margin:m:memory": 3, "pressure:m": 3, "git:dirty": 5, "git:unpushed": 3, "handoff": 12, "overlap:a:b": 3})
        self.assertEqual([k for k, _ in janus.repeated_issue_findings(cfg)], ["issue:overlap:a:b"])
        janus.auto_log_issues(cfg); rows = [l for l in janus.issue_path(cfg).read_text().splitlines() if "keeps coming back" in l]
        self.assertEqual(len(rows), 1, rows); self.assertIn("[overlap:a:b]", rows[0])


class Landing(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project('\n[landing]\nbranch = "main"\nfinal_gate_host = "here"\nfinal_gate = "true"\n')
        self.r = self.d / "repo"; self.r.mkdir(); self.wt = self.d / "wt"
        git(self.r, "init", "-q", "-b", "main"); (self.r / "a").write_text("1\n"); git(self.r, "add", "a"); git(self.r, "commit", "-qm", "base")
        git(self.r, "config", "user.name", "t"); git(self.r, "config", "user.email", "t@t")   # land's own commit: no identity from the machine
        git(self.r, "worktree", "add", "-q", "-b", "wk/u", str(self.wt))
        self.u = {"id": "x-1", "name": "u", "cwd": str(self.wt), "tier": "normal", "state": "done", "verdict": "VERDICT: DONE"}
        self.cfg.run.all_units = {"u": self.u}
        for k, v in (("units", lambda s: [self.u]), ("comment", lambda s, *a: None)):
            m = mock.patch.object(janus.Beads, k, v); m.start(); self.addCleanup(m.stop)
    def a(self, **k): return type("A", (), {"unit": "u", "no_gate": True, "no_dispatch": True, **k})()
    def commit(self, wt, name, msg): (Path(wt) / name).write_text(msg + "\n"); git(wt, "add", name); git(wt, "commit", "-qm", msg)
    def land(self, **k):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()): return janus.cmd_land(self.cfg, self.a(**k))

    def test_a_collected_patch_not_yet_applied_stops_land_and_is_a_finding(self):
        """Catches row 239: a unit that ran on a host without the repository had its work only in out/<unit>/changes.patch; land
        said 'already landed ... janus close' (or 'nothing to land') and check said nothing: a close would have dropped the work."""
        (self.wt / "b").write_text("new\n"); git(self.wt, "add", "-N", "b"); od = self.d / "out/u"; od.mkdir()
        (od / "changes.patch").write_text(git(self.wt, "diff")); git(self.wt, "reset", "-q"); (self.wt / "b").unlink()
        with self.assertRaises(janus.Err) as e: self.land()
        self.assertIn("changes.patch is not applied", str(e.exception)); self.assertIn("apply --3way", str(e.exception))
        self.assertIn("landing:u:patch", [k for k, _ in janus.landing_findings(self.cfg, [self.u], {})])
        git(self.wt, "apply", str(od / "changes.patch")); git(self.wt, "add", "b"); git(self.wt, "commit", "-qm", "u: b")
        self.assertNotIn("landing:u:patch", [k for k, _ in janus.landing_findings(self.cfg, [self.u], {})])
        self.assertEqual(self.land(), 0)   # applied and committed: it lands
        (od / "changes.patch").write_text("diff --git a/.pytest_cache/v/x b/.pytest_cache/v/x\nnew file mode 100644\n--- /dev/null\n+++ b/.pytest_cache/v/x\n@@ -0,0 +1 @@\n+1\n")
        self.assertIsNone(janus.unapplied_patch(self.cfg, self.u), "row 251: a patch of tool caches only was taken for unapplied work")

    def test_land_writes_a_missing_or_stale_gate_script_with_gate_gen(self):
        """Catches row 252: `janus land` failed at once ('out/land-<unit>/tests.sh not found') when nobody had generated the final
        gate's script, and gated a stale script after a merge."""
        self.cfg.c["landing"]["gate_gen"] = "mkdir -p out/land-{unit} && echo {worktree} >> out/land-{unit}/tests.sh"
        t = self.d / "out/land-u/tests.sh"; seen = []; self.commit(self.wt, "b", "work")
        def gen():
            with contextlib.redirect_stdout(io.StringIO()): janus.gate_gen(self.cfg, self.cfg.c["landing"], "u", self.wt)
            return len(t.read_text().splitlines())
        with mock.patch.object(janus, "cmd_run", lambda cfg, a: seen.append(t.read_text() if t.is_file() else None) or 0):
            self.assertEqual(self.land(no_gate=False), 0)
        self.assertEqual(seen, [f"{self.wt}\n"], "the gate ran without a generated script")
        self.assertEqual(gen(), 1, "a script newer than the tree was generated again")
        os.utime(t, (1, 1)); self.assertEqual(gen(), 2, "a script older than the tree was kept")

    def test_land_refuses_a_landing_branch_that_is_behind_its_pushed_upstream(self):
        """Catches row 240: the local landing branch was one pushed commit behind its upstream; land counted that commit as the
        unit's and squashed it into the unit's commit on the old base (a rewrite of pushed history)."""
        o = self.d / "origin.git"; subprocess.run(["git", "init", "-q", "--bare", str(o)], check=True)
        git(self.r, "remote", "add", "origin", str(o)); git(self.r, "push", "-q", "-u", "origin", "main")
        self.commit(self.r, "c", "main: c (pushed)"); git(self.r, "push", "-q", "origin", "main"); pushed = git(self.r, "rev-parse", "HEAD").strip()
        git(self.wt, "merge", "-q", "main"); self.commit(self.wt, "b", "u: b")   # the unit holds the pushed commit and its own
        git(self.r, "reset", "-q", "--hard", "HEAD~1")                           # the local landing branch is left one commit behind
        with self.assertRaises(janus.Err) as e: self.land()
        self.assertIn("behind origin/main", str(e.exception)); self.assertIn("merge --ff-only origin/main", str(e.exception))
        git(self.r, "merge", "-q", "--ff-only", "origin/main"); self.assertEqual(self.land(), 0)
        self.assertEqual(git(self.r, "rev-parse", "main~1").strip(), pushed, "the pushed commit was rewritten")

    def test_the_gate_land_queued_is_not_an_orphan_while_land_runs(self):
        """Catches row 241: land's final gate runs under the unit's label; for a done, unparked unit orphan_jobs called it an
        orphan and the watcher cancelled it mid-landing."""
        self.commit(self.wt, "b", "u: b"); seen = []
        task = {"status": {"Running": {}}, "label": janus.tag(self.cfg, "unit:u"), "group": "gates", "command": "bash /r/runs/u-1.sh"}
        hs = {"here": {"ok": True, "pueue": {"tasks": {"7": task}}}}
        def gate(cfg, ra): seen.append([r for r, _ in janus.orphan_jobs(cfg, hs, [self.u])]); return 0
        self.assertEqual([r for r, _ in janus.orphan_jobs(self.cfg, hs, [self.u])], ["here:7"])   # no land running: the job of a done unit is an orphan
        with mock.patch.object(janus, "cmd_run", gate): self.assertEqual(self.land(no_gate=False), 0)
        self.assertEqual(seen, [[]], "land's own gate was called an orphan while land waited for it")
        self.assertEqual([r for r, _ in janus.orphan_jobs(self.cfg, hs, [self.u])], ["here:7"])   # land ended: the exemption ends with it


if __name__ == "__main__": unittest.main()
