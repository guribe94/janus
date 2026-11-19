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
    """2026-11-18: two agents started on another host (tmux over ssh) sat at Claude's trust prompt since launch; only herdr panes
    were checked for startup dialogs, and the tmux session existing read as running. Then (same day) 3 of 4 launches on rigpc sat
    there 35-51 min: the pattern was written for a screen nobody had captured. TRUST is Claude Code 2.1.328's, read on rigpc."""
    TRUST = (" Accessing workspace:\n /home/vegtam/janus-repro-tmux\n Quick safety check: Is this a project you created or one you trust? (Like your\n"
             " own code, a well-known open source project, or work from your team).\n ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm · Esc to cancel")

    def test_one_loop_gets_past_a_dialog_by_screen_alone_or_by_agent_status(self):
        """Catches: a screen read before the client drew its dialog taken as clear; a dialog that never goes away taken as started."""
        sent = []
        with mock.patch("time.sleep", lambda s: None):
            screens = iter(["", self.TRUST, "Claude Code\n> ", "Claude Code\n> "])
            self.assertIsNone(janus.past_dialogs(lambda: next(screens), lambda *k: sent.append(k)))
            self.assertEqual(sent, [("Down", "Enter")])                         # "Yes", not the "No, exit" the cursor starts on; then settled
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
        self.assertTrue(any("send-keys -t janus-u@t Down Enter" in c for c in cmds), cmds)
        self.assertTrue(any("tmux new -d -s janus-u@t -x 200 -y 50 " in c for c in cmds), cmds)   # tall enough for the trust dialog
        self.assertTrue(any("has-session" in c for c in cmds))


class TmuxAgents(unittest.TestCase):
    """2026-11-18: no resume janus typed into a tmux agent was ever submitted (none is in any agent's transcript on asgard or rigpc),
    and the agents sat idle for hours reading "working". The screens below are Claude Code 2.1.328's, read on rigpc."""
    RULE = "─" * 80
    FOOT = "  ⏵⏵ auto mode on (shift+tab to cycle)"
    IDLE = f"● ONE\n✻ Crunched for 1s\n{RULE}\n❯ \n{RULE}\n{FOOT}"
    BUSY = f"✻ Harmonizing…\n{RULE}\n❯ \n{RULE}\n{FOOT} · esc to interrupt"
    HELD = f"● done\n{RULE}\n❯ [Pasted text #1 +146 lines]\n{RULE}\n  paste again to expand"
    SENT = f"❯ [janus] Your jobs finished\n  memory line\n● Done.\n✻ Cogitated for 1s\n{RULE}\n❯ \n{RULE}\n{FOOT}"
    QUEUED = f"❯ [janus] Your jobs finished\n  ⎿  Queued · ctrl+x ctrl+s to send now\n✢ Discombobulating…\n{RULE}\n❯ Press up to edit queued messages\n{RULE}\n{FOOT} · esc to interrupt"
    TEXT = "[janus] Your jobs finished  \n" + "memory line\n" * 2000

    def submit(self, screens, text=TEXT):
        d, cfg = project('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n'); h = cfg.hosts["far"]; sent = []; cmds = []; it = iter(screens)
        def hrun(h, s, **k):
            cmds.append(s); return (0, next(it), "") if "capture-pane" in s else (0, "", "")
        with mock.patch.object(janus, "X", lambda cfg, h, s, **k: sent.append((s, k.get("inp"))) or (0, "", "")), \
             mock.patch.object(janus, "hrun", hrun), mock.patch("time.sleep", lambda s: None):
            try: return sent, cmds, janus.tmux_submit(cfg, h, "janus-u@t", text)
            except janus.Err as e: return sent, cmds, e

    def test_a_long_prompt_is_pasted_whole_and_confirmed(self):
        """Catches: `send-keys -l <text>` refuses a command over about 16 KB ("command too long"), a resume carries the project
        memory (47 KB), the `&&` skipped the Enter and the exit status was never read, so janus logged a resume nothing received."""
        sent, cmds, r = self.submit([self.IDLE, self.BUSY])
        self.assertIsNone(r); self.assertEqual(len(sent), 1)
        self.assertIn("paste-buffer -p", sent[0][0]); self.assertNotIn("send-keys -t janus-u@t -l", sent[0][0])
        self.assertGreater(len(sent[0][1]), 20000)                      # the text goes on stdin, never on a command line
        self.assertIsNone(self.submit([self.IDLE, self.SENT])[2])       # a turn already over: the prompt stands in the history

    def test_only_new_proof_counts(self):
        """Catches: an empty box taken as delivered (a lost paste leaves it empty too); an earlier resume's line in the history
        (resumes share their first line) taken as this one; "esc to interrupt" in the agent's output taken as its footer."""
        self.assertIsInstance(self.submit([self.IDLE] * 11)[2], janus.Err)
        self.assertIsInstance(self.submit([self.SENT] * 11)[2], janus.Err)
        quoted = f"● the probe greps esc to interrupt\n{self.RULE}\n❯ \n{self.RULE}\n{self.FOOT}"
        self.assertFalse(janus.busy(quoted)); self.assertTrue(janus.busy(self.BUSY))
        held = f"● done\n{self.RULE}\n❯ go on\n{self.RULE}\n{self.FOOT}"   # a short draft shows in the box as `❯ <text>`
        self.assertEqual(janus.sent_lines(held, "❯ go on"), 0)
        self.assertEqual(janus.sent_lines(f"❯ go on\n{self.RULE}\n{self.FOOT}", "❯ go on"), 0)   # one rule: no box drawn, no proof

    def test_a_prompt_is_never_sent_twice(self):
        """Catches: a prompt left unsent read as delivered; a retry pasting onto an earlier unsent paste, sending the text twice."""
        sent, cmds, r = self.submit([self.IDLE] + [self.HELD] * 5 + [self.BUSY])   # held: one more Enter, never a second paste
        self.assertIsNone(r); self.assertEqual(len(sent), 1); self.assertEqual(sum(c.endswith("send-keys -t janus-u@t Enter") for c in cmds), 1)
        sent, cmds, r = self.submit([self.HELD, self.BUSY])                         # an earlier unsent paste is cleared first
        self.assertIsNone(r); self.assertTrue(cmds[1].endswith("send-keys -t janus-u@t C-u"), cmds)
        sent, cmds, r = self.submit([self.BUSY, self.QUEUED])                       # working: the prompt queues, and the queue shows it
        self.assertIsNone(r); self.assertFalse(any("C-u" in c or "Escape" in c for c in cmds), cmds)
        self.assertIsInstance(self.submit([self.BUSY] * 11)[2], janus.Err)         # working before: busy proves nothing about this prompt

    def test_a_tmux_agent_reads_working_only_while_its_screen_says_so(self):
        """Catches: a tmux unit read "working" whenever its session existed, so an agent idle at its prompt for hours raised
        nothing. An idle tmux agent is alive: its jobs are not orphans, and close and handover still ask for janus stop."""
        out = "avail_mb=1\n--pueue\n{}\n--tmux\njanus-a@t busy\njanus-b@t\nother\n"
        with mock.patch.object(janus, "hrun", lambda *a, **k: (0, out, "")):
            p = janus.probe("far", {"ssh": "u@far", "_rd": "/r"})
        self.assertEqual((p["tmux"], p["tmux_busy"]), (["janus-a@t", "janus-b@t", "other"], ["janus-a@t"]))
        st = lambda t: janus.unit_state({"via": "tmux", "tmux": t, "host": "far"}, {"far": p}, {})
        self.assertEqual([st("janus-a@t"), st("janus-b@t"), st("janus-c@t")], ["working", "idle", "gone"])
        self.assertFalse(janus.agent_ended({"via": "tmux", "state": "idle"})); self.assertTrue(janus.agent_ended({"via": "tmux", "state": "gone"}))
        self.assertTrue(janus.agent_live({"via": "tmux"}, "idle")); self.assertFalse(janus.agent_live({"via": "herdr"}, "idle"))

    def test_the_probe_reads_busy_from_the_footer_of_a_real_screen(self):
        """Catches: the probe loop never run (only parsed); the words in the agent's output read as its footer."""
        with tempfile.TemporaryDirectory() as d:
            sock = str(Path(d, "s")); tm = lambda *a: subprocess.run(["tmux", "-S", sock, *a], capture_output=True, text=True)
            if subprocess.run(["sh", "-c", "command -v tmux"], capture_output=True).returncode: self.skipTest("no tmux")
            for n, scr in (("w", self.BUSY), ("i", f"esc to interrupt\n{self.IDLE}")):
                f = Path(d, n); f.write_text(scr + "\n"); tm("new", "-d", "-s", n, "-x", "120", "-y", "20", f"cat {f}; sleep 30")
            time.sleep(0.5)
            try:
                loop = janus.PROBE.split('echo "--tmux"; ')[1].rsplit("; true", 1)[0].replace("tmux ", f"tmux -S {sock} ")
                out = subprocess.run(["sh", "-c", loop], capture_output=True, text=True).stdout
            finally: tm("kill-server")
        self.assertEqual(sorted(out.split("\n")[:-1]), ["i", "w busy"], out)


class ResumeReopensReport(unittest.TestCase):
    def test_a_resumed_unit_does_not_read_done_from_its_old_line_one(self):
        """Catches (2026-11-18): a unit resumed with more to do kept its report's old `VERDICT: DONE` on line 1, so it read finished
        until its agent rewrote the line; launch already reopens a resumed report, a resume did not. Remote: reset where the agent
        writes (its host), or the next sync brings the old line back."""
        d, cfg = project('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n'); rep = d / ".janus/out/u/REPORT.md"; rep.parent.mkdir(parents=True)
        rep.write_text("VERDICT: DONE (landed)\nhistory\nWAITING here:3\n"); sent = []
        u = {"name": "u", "host": "here", "via": "herdr", "model": "m"}
        answer = lambda *a, **k: sent.append(a) or ({"agents": [{"name": "u"}]} if a[:2] == ("agent", "list") else {})
        with mock.patch.object(janus, "herdr", answer), mock.patch.object(janus, "route_ok", lambda *a: (True, "")):
            janus.resume_unit(cfg, u, "your jobs finished")
        self.assertEqual(rep.read_text().splitlines()[:2], ["VERDICT: PENDING", "history"]); self.assertTrue(sent)
        cmds = []; rep.write_text("VERDICT: BLOCKED x\n")
        screens = iter([TmuxAgents.IDLE, TmuxAgents.BUSY])   # the agent takes the prompt
        remote = lambda h, s, **k: (0, "VERDICT: DONE (on the host)\nnotes\n", "") if s.startswith("cat ") else (0, next(screens), "") if "capture-pane" in s else (0, "", "")
        with mock.patch.object(janus, "X", lambda cfg, h, s, **k: cmds.append((s, k.get("inp"))) or (0, "", "")), mock.patch.object(janus, "hrun", remote), \
             mock.patch.object(janus, "route_ok", lambda *a: (True, "")), mock.patch("time.sleep", lambda s: None):
            janus.resume_unit(cfg, {"name": "u", "host": "far", "via": "tmux", "tmux": "t", "model": "m"}, "go on")
        self.assertIn("VERDICT: PENDING\nnotes\n", [i for c, i in cmds if "/r/out/u/REPORT.md" in c], cmds)   # written on the host, by the file it read there
        self.assertTrue(rep.read_text().startswith("VERDICT: PENDING"))

    def test_a_resumed_tmux_units_next_turn_end_is_brought_here(self):
        """Catches (2026-11-19): a tmux unit's output was brought here once, when its turn ended on WAITING; its marker never changes
        (no task id), so after the resume its DONE stayed on its host, the local report read PENDING for backup_min, and the
        orchestrator resumed a finished unit by hand (reopening its report each time)."""
        d, cfg = project('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n')
        u = {"name": "u", "host": "far", "via": "tmux", "tmux": "t", "model": "m", "saved": "tmux", "saved_at": int(time.time())}
        self.assertFalse(janus.refresh_due(cfg, {**u, "state": "idle"}))      # its WAITING turn's end: brought here already
        screens = iter([TmuxAgents.IDLE, TmuxAgents.BUSY])
        remote = lambda h, s, **k: (0, next(screens), "") if "capture-pane" in s else (0, "", "")
        with mock.patch.object(janus, "X", lambda *a, **k: (0, "", "")), mock.patch.object(janus, "hrun", remote), \
             mock.patch.object(janus, "route_ok", lambda *a: (True, "")), mock.patch("time.sleep", lambda s: None):
            janus.resume_unit(cfg, u, "your jobs finished")
        self.assertTrue(janus.refresh_due(cfg, {**u, "state": "idle"}))       # the resumed turn's end is brought here when it ends

    def test_a_herdr_unit_whose_agent_is_gone_is_refused_with_its_relaunch(self):
        """Catches (2026-11-19): after janus stop, a resume said 'prompted in its herdr pane' though herdr had no such agent (its
        answer was ignored), and reopened the report; launch --resume-of under the same name said only 'already launched'."""
        d, cfg = project(); rep = d / ".janus/out/u/REPORT.md"; rep.parent.mkdir(parents=True); rep.write_text("VERDICT: DONE\n"); sent = []
        u = {"name": "u", "host": "here", "via": "herdr", "model": "m", "kind": "audit", "tier": "critical", "item": "X1", "brief": "/b/u.md", "cwd": "/w/u"}
        answer = lambda *a, **k: sent.append(a) or ({"agents": [{"name": "other"}]} if a[:2] == ("agent", "list") else None)
        with mock.patch.object(janus, "herdr", answer), mock.patch.object(janus, "route_ok", lambda *a: (True, "")), mock.patch.object(janus, "all_units", lambda cfg: {"u": u}):
            with self.assertRaises(janus.Err) as e: janus.resume_unit(cfg, u, "go on")
        self.assertIn("janus launch u-r2 --kind audit --tier critical --model m --host here --item X1 --resume-of u --brief /b/u.md --cwd /w/u", str(e.exception))
        self.assertNotIn("prompt", [x[1] for x in sent]); self.assertEqual(rep.read_text(), "VERDICT: DONE\n")   # nothing sent, the report kept


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


class UnitsThatDoNotFinish(unittest.TestCase):
    """2026-11-18: 86 closed units on a project ended without DONE. Each test names the janus cause it catches."""
    def test_a_report_json_an_earlier_run_left_does_not_state_the_verdict(self):
        """Catches (a80-page-signoff, a98-finish-midgard): a unit resumed and finished DONE read BLOCKED / READY-TO-LAND from the
        report.json its run before the resume had written."""
        d, cfg = project(); o = d / ".janus/out/u"; o.mkdir(parents=True); u = {"name": "u", "host": "here", "started": 0, "resumed_at": time.time() - 300}
        (o / "report.json").write_text(json.dumps({"verdict": "BLOCKED"})); (o / "REPORT.md").write_text("VERDICT: DONE (landed)\n")
        os.utime(o / "report.json", (time.time() - 600,) * 2)
        self.assertEqual(janus.verdict(cfg, u), "VERDICT: DONE (landed)")
        os.utime(o / "report.json", None)
        self.assertEqual(janus.verdict(cfg, u), "VERDICT: BLOCKED")                       # this run's json still wins over the prose

    def test_an_answer_resumes_the_stopped_unit_that_asked(self):
        """Catches: janus answer only wrote the board; an agent whose turn had ended on its question never read it, and the unit was
        closed unfinished (6 answered questions, none acted on)."""
        d, cfg = project(); cfg.run.ro = False; resumed = []; state = {"a1": "idle"}
        units = {n: {"name": n, "host": "here", "via": "herdr"} for n in ("a1", "a2")}
        def get_unit(cfg, n):
            if n not in units: raise janus.Err(f"no unit {n}")
            return None, units[n]
        with mock.patch.object(janus, "get_unit", get_unit), mock.patch.object(janus, "herdr_agents", lambda cfg: {n: {"agent_status": s} for n, s in state.items()}), \
             mock.patch.object(janus, "cmd_resume", lambda cfg, a: resumed.append((a.unit, a.message))):
            for who in ("a1", "a2", "orch"): janus.board(cfg, "post", who, "question", f"{who} asks")
            janus.cmd_answer(cfg, argparse.Namespace(id="q1", answer="use 8080"))
            self.assertEqual(resumed, [("a1", "The answer to your question q1: use 8080")])
            state["a2"] = "working"; janus.cmd_answer(cfg, argparse.Namespace(id="q2", answer="x"))   # working: it reads the board itself
            janus.cmd_answer(cfg, argparse.Namespace(id="q3", answer="y"))                          # not a unit: the board answer is all
            state.clear(); janus.board(cfg, "post", "a1", "question", "again"); janus.cmd_answer(cfg, argparse.Namespace(id="q4", answer="z"))   # its pane is gone: a prompt reaches nobody
        self.assertEqual(len(resumed), 1)
