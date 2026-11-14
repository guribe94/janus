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

    def test_hrun_retries_only_a_refused_mux_session_without_the_master(self):
        """Catches runs failing with `Session open refused by peer` from a busy master connection, and the opposite harm: a retry
        of any other rc 255 (a dropped connection mid-command) could run a remote command twice."""
        calls = []
        def fake(err):
            def f(cmd, **k): calls.append(cmd); return (255, "", err) if len(calls) == 1 else (0, "ok", "")
            return f
        refused = "mux_client_request_session: session request failed: Session open refused by peer"
        with mock.patch.object(janus, "sh", fake(refused)):
            self.assertEqual(janus.hrun(dict(self.REMOTE), "true")[0], 0)
        self.assertEqual(len(calls), 2); self.assertIn("ControlPath=none", calls[1]); self.assertNotIn("ControlPath=none", calls[0])
        calls.clear()
        with mock.patch.object(janus, "sh", fake("Connection to b closed by remote host.")):
            self.assertEqual(janus.hrun(dict(self.REMOTE), "true")[0], 255)
        self.assertEqual(len(calls), 1, "a different rc 255 was retried")

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
        d, cfg = project('\n[hosts.big]\nssh = "u@big"\nroot = "/r"\n'); b = d / ".janus/briefs/u.md"; b.write_text(brief_text)
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


if __name__ == "__main__": unittest.main()
