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
        for n in ("u1-proof", "u10", "gone", "u1-r3", "u1-r5"): git(r, "worktree", "add", "-q", "-b", f"wk/{n}", str(w / n))
        cfg.run.all_units = {"u1": {"status": "open"}}; kept = []
        with mock.patch.object(janus, "ws_id", lambda cfg, create=False: "w1"):
            P = janus.tidy_plan(cfg, [{"name": "u1", "cwd": str(d)}, {"name": "u1-r5", "cwd": str(d)}], {}, {}, kept)
        # 2026-11-18: u1-r3, a closed relaunch of the open (stranded) u1, was kept as u1's own tree: four 700 MB trees on a full disk
        self.assertEqual(sorted(Path(t).name for k, t, _ in P if k == "worktree"), ["gone", "u1-r3", "u10"], "an open unit's own proof tree is offered for removal")
        self.assertIn("u1-proof", [Path(t).name for t, _ in kept]); self.assertIn("u1-r5", [Path(t).name for t, _ in kept])


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


class StartupDialogs(unittest.TestCase):
    """2026-11-18: two agents started on another host (tmux over ssh) sat at Claude's "Do you trust this folder" prompt since launch;
    only herdr panes were checked for startup dialogs, and the tmux session existing read as running."""
    TRUST = "Do you trust the files in this folder?\n> 1. Yes, proceed"

    def test_one_loop_gets_past_a_dialog_by_screen_alone_or_by_agent_status(self):
        """Catches: a screen read before the client drew its dialog taken as clear; a dialog that never goes away taken as started."""
        sent = []
        with mock.patch("time.sleep", lambda s: None):
            screens = iter(["", self.TRUST, "Claude Code\n> ", "Claude Code\n> "])
            self.assertIsNone(janus.past_dialogs(lambda: next(screens), lambda *k: sent.append(k)))
            self.assertEqual(sent, [("Enter",)])                                # answered once, then the screen settled
            self.assertIn("still shown", janus.past_dialogs(lambda: self.TRUST, lambda *k: None))
            self.assertIsNone(janus.past_dialogs(lambda: "{}", lambda *k: None, lambda: "working"))   # herdr: its status says started
            self.assertIn("agent blocked", janus.past_dialogs(lambda: "x", lambda *k: None, lambda: "blocked"))

    def test_a_tmux_start_answers_the_trust_prompt_on_its_host(self):
        """Catches: the tmux start never looking at the agent's screen."""
        d, cfg = project('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n'); h = cfg.hosts["far"]; cmds = []; screens = iter([self.TRUST, "ready", "ready"])
        def hrun(h, s, **k):
            cmds.append(s)
            if "tmux new" in s: return 0, "4242\n", ""
            if "capture-pane" in s: return 0, next(screens, "ready"), ""
            return 0, "", ""
        with mock.patch.object(janus, "hrun", hrun), mock.patch("time.sleep", lambda s: None):
            self.assertEqual(janus.start_tmux(cfg, h, "janus-u@t", "cd /r && exec claude"), 4242)
        self.assertTrue(any("send-keys -t janus-u@t Enter" in c for c in cmds), cmds)
        self.assertTrue(any("has-session" in c for c in cmds))


class ResumeReopensReport(unittest.TestCase):
    def test_a_resumed_unit_does_not_read_done_from_its_old_line_one(self):
        """Catches (2026-11-18): a unit resumed with more to do kept its report's old `VERDICT: DONE` on line 1, so it read finished
        until its agent rewrote the line; launch already reopens a resumed report, a resume did not. Remote: reset where the agent
        writes (its host), or the next sync brings the old line back."""
        d, cfg = project('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n'); rep = d / ".janus/out/u/REPORT.md"; rep.parent.mkdir(parents=True)
        rep.write_text("VERDICT: DONE (landed)\nhistory\nWAITING here:3\n"); sent = []
        u = {"name": "u", "host": "here", "via": "herdr", "model": "m"}
        with mock.patch.object(janus, "herdr", lambda *a, **k: sent.append(a)), mock.patch.object(janus, "route_ok", lambda *a: (True, "")):
            janus.resume_unit(cfg, u, "your jobs finished")
        self.assertEqual(rep.read_text().splitlines()[:2], ["VERDICT: PENDING", "history"]); self.assertTrue(sent)
        cmds = []; rep.write_text("VERDICT: BLOCKED x\n")
        remote = lambda h, s, **k: (0, "VERDICT: DONE (on the host)\nnotes\n", "") if s.startswith("cat ") else (0, "", "")
        with mock.patch.object(janus, "X", lambda cfg, h, s, **k: cmds.append((s, k.get("inp"))) or (0, "", "")), mock.patch.object(janus, "hrun", remote), \
             mock.patch.object(janus, "route_ok", lambda *a: (True, "")):
            janus.resume_unit(cfg, {"name": "u", "host": "far", "via": "tmux", "tmux": "t", "model": "m"}, "go on")
        self.assertIn("VERDICT: PENDING\nnotes\n", [i for c, i in cmds if "/r/out/u/REPORT.md" in c], cmds)   # written on the host, by the file it read there
        self.assertTrue(rep.read_text().startswith("VERDICT: PENDING"))


class JobProgress(unittest.TestCase):
    def test_a_jobs_state_age_and_usual_time_come_from_one_reading(self):
        """Catches: the job-age finding and the monitor each working out a job's age their own way (they disagreed once the
        monitor started showing it); a waiting job timed from its start instead of its enqueue."""
        t0 = int(time.time()) - 840; q = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0 - 60))
        hs = {"h": {"ok": True, "waitjob": ["j8"], "res": [f"4242 1 512 {t0} cargo_test j9.res -"], "cls": ["cargo_test 12 120 1"],
                    "pueue": {"tasks": {"9": {"status": {"Running": {}}, "command": "bash /x/runs/j9.sh"},
                                        "8": {"status": {"Running": {"start": q}}, "command": "bash /x/runs/j8.sh"},
                                        "7": {"status": {"Queued": {"enqueued_at": q}}, "command": "bash /x/runs/j7.sh"}}}}}
        st, rc, since, usual = janus.job_progress(hs, "h:9"); self.assertEqual((st, since, usual), ("running", t0, 120))
        st, _, since, usual = janus.job_progress(hs, "h:8"); self.assertEqual(st, "admission"); self.assertIsNone(since)   # held: timed from its enqueue, which pueue did not give
        st, _, since, _ = janus.job_progress(hs, "h:7"); self.assertEqual((st, int(since)), ("queued", t0 - 60))
        self.assertEqual(janus.job_progress(hs, "h:5")[0], "gone"); self.assertEqual(janus.job_progress({"h": {"ok": False}}, "h:9")[0], "unknown")


class CapacityRuleFollowsReleases(unittest.TestCase):
    def test_the_watcher_rewrites_a_hosts_rule_when_janus_or_its_config_changes_it(self):
        """Catches: a new release (or a [capacity] change) reached a host's runs/cap.sh only with its next launch or job, so a
        host paused by the old rule took no work and stayed paused."""
        d, cfg = project(); hs = {"here": {"ok": True}}; log = []; f = Path(cfg.hosts["here"]["_rd"]) / "cap.sh"
        janus.tend_cap(cfg, hs, log.append); self.assertTrue(f.is_file()); self.assertEqual(len(log), 1)
        janus.tend_cap(cfg, hs, log.append); self.assertEqual(len(log), 1)            # the same rule: not written again
        cfg.capc["psi_pause"] = 20; janus.tend_cap(cfg, hs, log.append)
        self.assertEqual(len(log), 2); self.assertIn("PSIP=20", f.read_text())
        janus.tend_cap(cfg, {"here": {"ok": False}}, log.append); self.assertEqual(len(log), 2)   # a host that does not answer: next pass
