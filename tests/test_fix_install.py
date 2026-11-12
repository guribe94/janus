"""fix-install items 7-14: the issues found installing janus on a live project, each test naming the failure it catches.
Standard library only; no network, no API spend."""
import contextlib, io, json, os, re, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path
from unittest import mock

from janusload import JANUS_PY, load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-fix-"))
    (d / "janus.toml").write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        ''') + textwrap.dedent(extra))
    (d / "out").mkdir(); (d / "briefs").mkdir()
    return d, janus.Cfg(d)


def probe_of(**k):
    return {"ok": True, "res": [], "hist": [], "sched": [], "direct": [], "pueue": {"tasks": {}}, "cap_pause": "", "cap_admit": "yes", "tmux": [], **k}


class Item11Verdict(unittest.TestCase):
    """Item 11: WAITING/PENDING is never a final verdict; a recorded park wins over a verdict."""
    def test_waiting_and_pending_are_not_final_verdicts(self):
        """Catches: units.verdict/verdict_line excluded only PENDING, so a report ending 'VERDICT: WAITING' read as finished and
        its jobs were cancelled as orphans."""
        d, cfg = project()
        (d / "out/u1").mkdir(parents=True); (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nbody\nWAITING here:1\nVERDICT: WAITING\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        self.assertEqual(janus.verdict_line({"verdict": "WAITING"}), (None, True))
        self.assertEqual(janus.verdict_line({"verdict": "PENDING"}), (None, True))
        self.assertEqual(janus.verdict_line({"verdict": "DONE"}), ("VERDICT: DONE", True))

    def test_a_recorded_park_wins_over_a_verdict(self):
        """Catches: a unit whose run was resumed and then parked was judged finished (its jobs became orphan:job)."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True); (d / "out/u1/REPORT.md").write_text("VERDICT: DONE\nall good\n")
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "parked": {"jobs": ["here:9"]}}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u["state"], "parked"); self.assertEqual(u["_park"], ["here:9"])
        self.assertNotIn("unit:u1:finished", [k for k, _ in F])

    def test_remote_ended_unit_report_is_synced_before_orphan_judging(self):
        """Catches (item 11 addendum): a remote unit's local report has no WAITING line until collect, so check judged it orphaned
        and its jobs were cancelled."""
        d, cfg = project("\n[hosts.far]\nssh = \"far\"\nroot = \"/home/x\"\n")
        (d / "out/u1").mkdir(parents=True); (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\n")
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "started": 0, "state": "done"}
        hs = {"far": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        real = janus.save_output
        janus.save_output = lambda cfg, u: ((d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nWAITING far:9\n"), True)[1]
        try:
            janus.sync_remote_outputs(cfg, [u], type("B", (), {"save": lambda self, u: None})())
            F, _ = janus.unit_findings(cfg, hs, [u], {})
        finally: janus.save_output = real
        self.assertEqual(u["_park"], ["far:9"]); self.assertEqual(u["state"], "parked")
        self.assertNotIn("orphan:job:far:9", [k for k, _ in janus.orphan_job_findings(cfg, hs, [u], set())])
        import inspect
        self.assertIn("sync_remote_outputs(cfg, units, bd)", inspect.getsource(janus.collect_findings))


class Item7CommitState(unittest.TestCase):
    def test_a_file_that_vanishes_mid_commit_does_not_fail_the_commit(self):
        """Catches: janus commit-state failed with 'git add failed: fatal: unable to stat ...' when a monitor deleted a temp file
        between listing and add."""
        d = Path(tempfile.mkdtemp(prefix="janus-fix-")); (d / "janus.toml").write_text('[project]\nname = "t"\n'); (d / "out").mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True, check=True)
        g("init", "-q"); (d / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
        (d / "b").write_text("2\n")   # the file the commit should still make
        cfg = janus.Cfg(d); calls = []
        real_sh, real_push = janus.sh, janus.push_cmd
        def sh_stub(cmd, **k):
            calls.append(list(cmd))
            if cmd == ["git", "add", "-A"]: return 128, "", "fatal: unable to stat b.tmp: No such file or directory"
            return real_sh(cmd, **k)
        janus.sh = sh_stub; janus.push_cmd = lambda repo, cfg=None: ["true"]
        try: janus.cmd_commit_state(cfg, type("A", (), {"message": "state"})())
        finally: janus.sh, janus.push_cmd = real_sh, real_push
        self.assertEqual(g("log", "-1", "--format=%s").stdout.strip(), "state")
        self.assertTrue(any(c[:4] == ["git", "add", "-A", "--ignore-errors"] for c in calls), "the add was not retried")


class Item7WastePause(unittest.TestCase):
    def test_an_ack_of_waste_pause_keeps_launch_unpaused(self):
        """Catches: waste_paused returned None while its ack was live, so the finding was gone and the next check dropped the ack
        ('finding gone') and re-paused launch and fill."""
        d, cfg = project()
        cfg.jput("acks.json", {"waste-pause": {"t": janus.now(), "d": "cause found"}})
        real = janus.waste_state; janus.waste_state = lambda cfg, outs=None: (10.0, 20.0, 50.0)
        try:
            for _ in range(2):
                self.assertTrue(janus.waste_paused(cfg), "waste_paused hid the reason while its ack was live")
                keys = {k for k, _ in janus.report_findings(cfg, [])}
                self.assertIn("waste-pause", keys)
                self.assertIn("waste-pause", janus.live_acks(cfg, keys), "the ack was dropped as 'finding gone'")
                self.assertIsNone(janus.waste_block(cfg), "launch is refused although waste-pause is acked")
        finally: janus.waste_state = real


class Item13Handover(unittest.TestCase):
    def test_close_force_allows_the_unit_when_own_tree_holds(self):
        """Catches: janus close <u> --force was refused although own_tree() held, because the `2` of `2>&1` counted as a second
        unit name (the shell tokenizer leaves it in the words)."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-h13-")); R = base / "repo"; R.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g("init", "-q"); (R / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
        W = base / "wt"; g("worktree", "add", "-q", str(W), "-b", "unit")
        d, cfg = project()
        u = {"id": "t-1", "name": "u1", "cwd": str(W)}
        real = janus.Beads; janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u]})
        try:
            self.assertTrue(janus.force_close_ok(cfg, ["close", "u1", "--force", "--reason", "a; b", "2"]))
        finally: janus.Beads = real

    def test_resume_of_takes_the_predecessors_cwd(self):
        """Catches: 'janus launch <successor> --resume-of <u> --cwd <its tree>' was refused by H9, so no route handed a unit's
        dirty worktree to a successor."""
        d, cfg = project(); W = d / "wt"; W.mkdir()
        units = [{"id": "t-1", "name": "u1", "cwd": str(W)}]
        self.assertEqual(janus.cwd_taken(cfg, units, W.resolve(), "u2"), (["u1"], None))
        self.assertEqual(janus.cwd_taken(cfg, units, W.resolve(), "u2", resume_of="u1"), ([], "u1"))


class Item12FinalGate(unittest.TestCase):
    def test_final_gate_substitutes_unit_tree_and_branch(self):
        """Catches: janus land took one fixed [landing] final_gate command, so a project that gates per unit could not use it."""
        ld = {"final_gate": "bin/run-handoff.sh out/land-{unit}-linux --tree {wt} --branch {branch}"}
        self.assertEqual(janus.final_gate_cmd(ld, "u1", "/w/u1", "wk/u1"),
                         "bin/run-handoff.sh out/land-u1-linux --tree /w/u1 --branch wk/u1")
        self.assertEqual(janus.final_gate_cmd({}, "u", "/w", "b"), "")

    def test_final_gate_quotes_substitutions_and_leaves_shell_values(self):
        """Catches (finding 7): {unit}, {wt} and {branch} went unquoted into a shell command, so a hostile branch name or a
        path with a space split it (an agent names its own branch, and the command runs on the final-gate host), and a ${...}
        in the config was mangled (${wt} became $/w)."""
        ld = {"final_gate": "bin/gate.sh {unit} {branch} --wt {wt}"}
        self.assertEqual(janus.final_gate_cmd(ld, "u1", "/Users/me/My Code/wt", "x;touch${IFS}/tmp/pwned"),
                         "bin/gate.sh u1 'x;touch${IFS}/tmp/pwned' --wt '/Users/me/My Code/wt'")
        self.assertEqual(janus.final_gate_cmd({"final_gate": "cd ${wt} && ./gate ${unit}"}, "u1", "/w", "b"),
                         "cd ${wt} && ./gate ${unit}")

    def test_a_placeholder_the_config_already_quotes_is_not_substituted_raw(self):
        """Catches (finding 10): with the config form `--branch "{branch}"`, the placeholder's own quotes were kept and the
        agent's branch name was inserted unquoted between them, so `wk/x$(touch${IFS}/tmp/p)';id;'` ran `$(...)`; and the
        single-quoted form let the value close the quote and run `id`."""
        import shlex as _s
        v = "wk/x$(touch${IFS}/tmp/p)';id;'"
        for cfg_form in ('bin/gate.sh --branch "{branch}"', "bin/gate.sh --branch '{branch}'"):
            self.assertEqual(janus.final_gate_cmd({"final_gate": cfg_form}, "u1", "/w", v), "bin/gate.sh --branch " + _s.quote(v), cfg_form)


class Item8ThrashExplicit(unittest.TestCase):
    def test_a_thrash_stop_is_written_to_the_job_log_and_names_the_requeue(self):
        """Catches (finding 8): a shared-host PSI stop/re-queue was recorded only in the watcher log and the check's finding,
        never in the stopped job's own log, and the finding did not say the job was re-queued on the same host (still over the
        limit); the over-since clock was not reset, so each pass stopped the next newest job."""
        import json as _json
        d, cfg = project('\n[hosts.far]\nssh = "x@far"\nroot = "/r"\nshared = true\nthrash_psi = 10\nthrash_min = 5\n'
                         'job_log = "/r/runs/job.log"\n'); cfg.run.ro = False
        jd = cfg.f("runs") / "jobs"; jd.mkdir(parents=True)
        (jd / "new-2.json").write_text(_json.dumps({"host": "far", "task": "2", "lane": "jobs", "unit": "u", "wt": str(d),
                                                    "cmd": "make", "at": 200, "pinned": True}))
        t = {"status": {"Running": {"start": "2026-01-01T11:00:00"}}, "command": "bash /r/runs/new-2.sh", "label": "unit:u@t", "group": "jobs"}
        hs = {"far": {"ok": True, "psi": "12.5", "pueue": {"tasks": {"2": t}}}, "here": {"ok": True}}
        cfg.jput("thrash.json", {"far": int(time.time()) - 600})
        calls = []; hl = []
        saved = (janus.cancel_job, janus.cmd_run, janus.host_cap, janus.board, janus.hrun)
        janus.cancel_job = lambda cfg, ref, force=False: calls.append(("cancel", ref)) or [f"cancelled {ref}"]
        janus.cmd_run = lambda cfg, a: calls.append(("run", a.host)) or print("far:9")
        janus.host_cap = lambda *a: {"free": 0, "pause": "memory PSI 12.5 over 10"}; janus.board = lambda *a, **k: (0, "", "")
        janus.hrun = lambda h, s, **k: (hl.append(s), (0, "", ""))[1]
        try:
            out = janus.tend_thrash(cfg, hs, [], lambda m: None)
            self.assertTrue(any("SAME host" in l for l in out), f"the finding does not name the re-queue target: {out}")
            log = (cfg.f("runs") / "new-2.log").read_text()
            self.assertIn("janus thrash", log); self.assertIn("far:2", log); self.assertIn("far:9", log)
            self.assertTrue(any("job.log" in s and "janus thrash" in s for s in hl), hl)
            n = len(calls); janus.tend_thrash(cfg, hs, [], lambda m: None)   # the clock was reset: no immediate second stop
            self.assertEqual(len(calls), n, "the over-since clock was not reset, so the next pass stopped another job")
        finally:
            janus.cancel_job, janus.cmd_run, janus.host_cap, janus.board, janus.hrun = saved


class Item9ResumeCard(unittest.TestCase):
    def test_resume_posts_the_card_as_waiting_with_the_new_task(self):
        """Catches: a unit resumed with janus resume --message whose run is queued in pueue (agents slot full) showed board GONE
        (relaunch it), because resume did not post the card with the new run."""
        d, cfg = project('\n[providers.api]\nenabled = true\n[models.m]\nprovider = "api"\nkind = "api"\n')
        b = d / "briefs/u1.md"; b.write_text("# Brief: u1\n")
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "model": "m", "task": 5, "budget": 2.0, "run": "u1.1",
             "brief": str(b), "rbrief": str(b), "rcwd": str(d), "started": 0}
        posts = []
        saved = {k: getattr(janus, k) for k in ("session_of", "run_cost", "ledger_done", "reserve_budget", "unit_spent", "X", "confirm_started", "save_task_log", "board")}
        janus.session_of = lambda cfg, u: "sid"; janus.run_cost = lambda *a, **k: (0.1, True); janus.ledger_done = lambda *a, **k: None
        janus.reserve_budget = lambda cfg, mc, name, budget, run=None: "u1.r2.1"; janus.unit_spent = lambda cfg, u, wrapup=False: 0.0
        janus.X = lambda cfg, h, cmd, **k: (0, "7", ""); janus.confirm_started = lambda cfg, h, tid: ("running", ""); janus.save_task_log = lambda *a, **k: None
        janus.board = lambda cfg, *args, env=None, **k: posts.append((args, env))
        try: janus.resume_unit(cfg, u, "your jobs finished")
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertEqual(u["task"], 7)
        self.assertTrue(any(a[0] == "post" and a[2] == "waiting" and (e or {}).get("BOARD_CHECK") == "pueue here:7" for a, e in posts), posts)


class Item10UnitChanges(unittest.TestCase):
    def test_a_staged_landing_merge_does_not_count_the_branchs_files(self):
        """Catches: a unit holding a staged merge of the landing branch counted all of the branch's files as its changes, so it
        overlapped every other such unit."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u10-")); R = base / "repo"; R.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main"); (R / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
        g("checkout", "-q", "-b", "unit"); (R / "a").write_text("1x\n"); g("commit", "-qam", "unit change")
        g("checkout", "-q", "main"); (R / "c").write_text("2\n"); g("add", "."); g("commit", "-qm", "main adds c")
        g("checkout", "-q", "unit"); (R / "a").write_text("1y\n")   # the unit's staged work
        g("merge", "--no-commit", "--no-ff", "main")                 # the landing branch merged and staged
        d, cfg = project()
        u = {"id": "t-1", "name": "u1", "host": "here", "cwd": str(R)}
        hs = {"here": probe_of()}
        br, fs = janus.unit_changes(cfg, u, [u])
        self.assertIn("a", fs); self.assertNotIn("c", fs, "the landing branch's own file counted as the unit's change")


class Item7BlockedLane(unittest.TestCase):
    def test_a_normally_queued_job_is_not_a_blocked_lane(self):
        """Catches (finding 7): `blocked:<host>:<lane>` fired whenever a job waited in admission, even when admission said yes
        (a job merely queued behind an older one), with the advice to cancel and re-queue it."""
        d, cfg = project()
        hs = {"here": {"ok": True, "pueue": {"groups": {"jobs": {"parallel_tasks": 1, "status": "Running"}},
                                            "tasks": {"1": {"status": {"Running": {}}, "label": "unit:x@t"}}},
                       "wait_jobs": "1", "cap_admit": "yes", "cap_pause": "", "cap_rc": "8", "cap_rm": "2048", "cap_ram": "0",
                       "cap_psi": "0", "res": [], "hist": [], "sched": [], "direct": [], "tmux": []}}
        F = dict(janus.lane_findings(cfg, hs))
        self.assertNotIn("blocked:here:jobs", F, f"a normally queued job read as a blocked lane: {F}")
        hs["here"]["cap_pause"] = "memory PSI avg10 12 over 10"   # a paused host IS a blocked lane
        self.assertIn("blocked:here:jobs", dict(janus.lane_findings(cfg, hs)))


class Item7CallerWait(unittest.TestCase):
    def test_only_a_task_itself_held_in_admission_is_reported(self):
        """Catches (finding 2, fix-install6): the caller reported a normally running job as 'held by admission' whenever the
        host's admission said no (`cap_admit` is the answer for ONE MORE job, not for this task; a heavy job on a full host made
        its own caller get the notice every 300 s), and probed the host for every waited job. Report only a task whose OWN
        admission-wait file exists; never probe one that started and runs normally."""
        d, cfg = project(); h = cfg.hosts["here"]
        def run_case(state, wait, adm):
            states = [state, {"status": {"Done": {"result": "Success"}}}]
            def fake_run(cmd, **k): raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))
            calls = []
            saved = (subprocess.run, janus.task_status, janus.probe, janus.hrun)
            subprocess.run = fake_run
            janus.task_status = lambda h_, tid: states.pop(0) if states else {"status": {"Done": {"result": "Success"}}}
            janus.probe = lambda n, h_, **k: (calls.append("probe"), {"cap_admit": adm, "cap_pause": ""})[1]
            janus.hrun = lambda h_, script, **k: (0, "y\n" if wait else "", "")
            out = io.StringIO()
            try:
                with contextlib.redirect_stderr(out): rc = janus.finish_job(cfg, "here", h, 7, "j-1", quiet=True)
            finally: subprocess.run, janus.task_status, janus.probe, janus.hrun = saved
            return rc, out.getvalue(), calls
        rc, msg, calls = run_case({"status": {"Running": {}}}, True, "disk")     # in admission: reported, probed for the reason
        self.assertEqual(rc, 0); self.assertIn("held by admission", msg); self.assertEqual(calls, ["probe"])
        rc, msg, calls = run_case({"status": {"Running": {}}}, False, "cores")   # running normally: silent, no probe
        self.assertEqual(rc, 0); self.assertEqual(msg, "", "a normally running task was reported")
        self.assertEqual(calls, [], "a task that started and runs normally was probed")
        rc, msg, calls = run_case({"status": {"Queued": {}}}, False, "yes")      # not started yet: silent
        self.assertEqual(rc, 0); self.assertEqual(msg, "", "a task not yet started was reported")


class Item10GiveUpResidues(unittest.TestCase):
    """Finding 10: the give-up clock residues -- a jobs job yielding to a gates job said "no reason reported", and nothing read
    exit 121 when a unit was resumed."""

    def test_a_resumed_unit_is_told_what_exit_121_means(self):
        """Catches (finding 10): nothing read exit 121, so a resumed unit got 'exit 121' with no explanation beyond the log."""
        d, cfg = project()
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "model": "m", "started": 0, "budget": 2.0}
        saved = janus.job_result; janus.job_result = lambda cfg, hs, j, host: (121, "/runs/j.log")
        try: msg = janus.resume_message(cfg, {"here": {"ok": True}}, u, ["here:9"])
        finally: janus.job_result = saved
        self.assertIn("never admitted", msg, msg)

    def test_a_jobs_job_yielding_to_a_gates_job_says_why_and_does_not_give_up(self):
        """Catches (finding 10): while a gates job waited, a jobs-lane job skipped the admission test -- its blocked clock ran
        with 'no reason reported' and it gave up (exit 121) although the gate in front of it was progressing."""
        import argparse
        d, cfg = project('\n[capacity]\nmin_disk_gb = 0\n')
        h = cfg.hosts["here"]; janus.write_cap(cfg, "here", h)
        rd = Path(h["_rd"]); rd.mkdir(parents=True, exist_ok=True)
        gate = subprocess.Popen(["sleep", "30"])
        (rd / "g.gwait").write_text(f"{gate.pid} 60 {int(time.time())} 256 1\n")
        r = argparse.Namespace(host="here", lane="jobs", unit=None, wt=str(d), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=False, command=["--", "true"], broad=None)
        js = janus.job_script(cfg, "here", h, r, "u", "true", "j-2")
        p = subprocess.Popen(["bash"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env={**os.environ, "JANUS_POLL_S": "1", "JANUS_ADMIT_MAX_S": "3", "JANUS_ADMIT_REPORT_S": "1"})
        p.stdin.write(js); p.stdin.close()
        try:
            time.sleep(4.5)
            self.assertIsNone(p.poll(), "the job gave up while a gates job waited: the yield should clear the blocked clock")
            (rd / "g.gwait").unlink()
            p.wait(timeout=30); out = p.stdout.read()
        finally:
            gate.kill(); p.kill()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("a gates job is waiting", out, out)


class Item8ResumeDryRun(unittest.TestCase):
    def test_dry_run_says_would_prompt(self):
        """Catches: janus resume --dry-run <herdr unit> printed '<unit>: prompted in its herdr pane' although nothing was prompted."""
        d, cfg = project(); cfg.run.dry = True
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "herdr", "pane": "p1", "model": "m", "started": 0}
        called = []; real = janus.herdr; janus.herdr = lambda *a, **k: called.append(a)
        try: line = janus.resume_unit(cfg, u, "go")
        finally: janus.herdr = real
        self.assertIn("would prompt", line); self.assertNotIn(": prompted", line)
        self.assertEqual(called, [], "the dry run prompted the pane")


class Item14OldOrch(unittest.TestCase):
    def test_old_orch_standing_loops_are_listed_not_waiters_or_editors(self):
        """Catches (finding 4): migrate matched any process with an `orch` token (a job waiter, an ssh, an editor) and never
        listed the old orch's standing loops (fleet-loops.sh, handoff-queue.sh, orch-backup.sh, wip-snapshot.sh, its watcher),
        and cut the list at 100 characters."""
        ps = (" 101 /bin/zsh /Users/x/proj/bin/fleet-loops.sh\n"
              " 102 /bin/zsh /Users/x/proj/bin/handoff-queue.sh w1 /Users/x/proj/out/handoff-queue-linux2\n"
              " 103 /bin/zsh /Users/x/proj/bin/orch-backup.sh 30\n"
              " 104 /bin/zsh /Users/x/proj/bin/wip-snapshot.sh 30\n"
              " 105 /bin/zsh /Users/x/proj/bin/watch-stream.sh\n"
              " 106 /usr/bin/python3 /Users/x/proj/orch.py run --host h --lane jobs --wt /w -- cargo test\n"
              " 107 /bin/zsh /Users/x/proj/bin/pueue wait -q 9\n"
              " 108 ssh host cd /Users/x/proj && ls\n"
              " 109 /usr/bin/vim /Users/x/proj/orch.py\n"
              " 111 /usr/bin/python3 /Users/x/proj/janus.py watch\n")
        self.assertEqual([p for p, _ in janus.old_orch_procs(ps, "/Users/x/proj")], [101, 102, 103, 104, 105])
        out = janus.retire_old_orch(dry=False, retire=False, ps_text=ps, root="/Users/x/proj")
        self.assertTrue(any("101" in l for l in out)); self.assertTrue(any("not stopped" in l for l in out))
        self.assertTrue(any("handoff-queue-linux2" in l for l in out), "the command line was cut before its argument")
        kills = []
        def fake_run(cmd, **k):   # finding 6: retire first asks for each process's live children (pgrep), never a kill
            if cmd[:1] == ["pgrep"]: return (0, "", "")
            kills.append(list(cmd)); return (0, "", "")
        real = janus.run; janus.run = fake_run
        try: janus.retire_old_orch(dry=False, retire=True, ps_text=ps, root="/Users/x/proj")
        finally: janus.run = real
        self.assertEqual(kills, [["kill", "-TERM", str(p)] for p in (101, 102, 103, 104, 105)])

    def test_old_orch_procs_are_scoped_to_this_project(self):
        """Catches (finding 4): another project's old-orch loop was listed for this project's migrate."""
        root = "/Users/x/proj"
        ps = (" 101 /bin/zsh /Users/x/proj/bin/fleet-loops.sh\n"
              " 110 /bin/zsh /other/proj/bin/fleet-loops.sh\n")
        self.assertEqual([p for p, _ in janus.old_orch_procs(ps, root)], [101])


class Item6RetireMatch(unittest.TestCase):
    def test_retire_matches_only_the_old_orchs_own_loops(self):
        """Catches (finding 6): `migrate --retire` matched a sibling project's loop (a path-prefix substring), a job waiter
        (`janus.py run ... -- bash bin/handoff-queue.sh`), a reader (`tail -f .../orch-backup.sh`) and an agent's own `-c` shell
        that merely named the scripts (the live listing showed 19 matches where 18 were loops)."""
        root = "/Users/x/proj"
        ps = (" 101 /bin/zsh /Users/x/proj/bin/fleet-loops.sh far\n"
              " 102 /bin/bash /Users/x/proj-two/bin/fleet-loops.sh far\n"
              " 103 /usr/bin/python3 /x/janus.py run --host far --wt /Users/x/proj -- bash bin/handoff-queue.sh\n"
              " 104 tail -f /Users/x/proj/bin/orch-backup.sh\n"
              " 105 /bin/zsh -c source ~/.zshenv && /Users/x/proj/bin/watch-stream.sh\n"
              " 106 /bin/zsh /Users/x/proj/orch.py watch\n"
              " 107 /bin/zsh /Users/x/proj/bin/pueue wait -q 9\n")
        self.assertEqual([p for p, _ in janus.old_orch_procs(ps, root)], [101, 106])

    def test_a_relative_loop_script_is_matched_by_the_process_own_cwd(self):
        """Catches (finding 6): a loop started with a relative script path (the old orch's own cwd) was not listed; and a loop
        started the same way in another project's tree must never be this project's."""
        ps = (" 201 /bin/zsh bin/fleet-loops.sh far\n"
              " 202 /bin/zsh bin/fleet-loops.sh far\n")
        real = janus._proc_cwd
        janus._proc_cwd = lambda pid: {201: Path("/w/proj"), 202: Path("/other/proj")}.get(pid)
        try: got = [p for p, _ in janus.old_orch_procs(ps, "/w/proj")]
        finally: janus._proc_cwd = real
        self.assertEqual(got, [201])

    def test_retire_keeps_a_mid_handoff_worker_whose_child_gate_runs(self):
        """Catches (finding 6): TERM to a handoff-queue.sh worker mid-handoff orphaned its child gate; the worker is listed but
        kept while it has a live child that is real work."""
        ps = (" 101 /bin/zsh /w/proj/bin/fleet-loops.sh\n"
              " 102 /bin/zsh /w/proj/bin/handoff-queue.sh w1 /w/proj/out/handoff-queue\n")
        kills = []
        def fake_run(cmd, **k):
            if cmd[:1] == ["pgrep"]: return 0, ("999\n" if cmd[-1] == "102" else ""), ""
            if cmd[:1] == ["ps"]: return 0, "999 bash\n", ""
            kills.append(list(cmd)); return 0, "", ""
        real = janus.run; janus.run = fake_run
        try: out = janus.retire_old_orch(dry=False, retire=True, ps_text=ps, root="/w/proj")
        finally: janus.run = real
        self.assertEqual(kills, [["kill", "-TERM", "101"]], "a mid-handoff worker was TERMed, orphaning its child gate")
        self.assertTrue(any("102" in l and "child" in l for l in out), out)

    def test_retire_stops_an_idle_loop_whose_only_child_is_its_sleep(self):
        """Catches (finding 4): --retire kept EVERY loop with a living child, and a loop that idles in `sleep 30` always has
        one, so --retire never stopped the loops it exists for."""
        ps = " 301 /bin/zsh /w/proj/bin/fleet-loops.sh far\n"
        kills = []
        def fake_run(cmd, **k):
            if cmd[:1] == ["pgrep"]: return 0, ("777\n" if cmd[-1] == "301" else ""), ""
            if cmd[:1] == ["ps"]: return 0, "777 sleep\n", ""
            kills.append(list(cmd)); return 0, "", ""
        real = janus.run; janus.run = fake_run
        try: out = janus.retire_old_orch(dry=False, retire=True, ps_text=ps, root="/w/proj")
        finally: janus.run = real
        self.assertEqual(kills, [["kill", "-TERM", "301"]], "an idle loop with only a sleep child was kept")

    def test_a_wrapped_loop_is_matched_but_not_a_wrapped_waiter(self):
        """Catches (finding 4): a loop started through sudo/nohup/env was not listed at all (over-strict matching)."""
        root = "/Users/x/proj"
        ps = (" 401 sudo /Users/x/proj/bin/fleet-loops.sh far\n"
              " 402 nohup /Users/x/proj/bin/orch-backup.sh 30\n"
              " 403 env FOO=1 /bin/zsh /Users/x/proj/bin/watch-stream.sh\n"
              " 404 env FOO=1 /usr/bin/python3 /x/janus.py run -- wt -- bash /Users/x/proj/bin/handoff-queue.sh\n")
        self.assertEqual([p for p, _ in janus.old_orch_procs(ps, root)], [401, 402, 403])


class Item15ReadyWork(unittest.TestCase):
    def test_only_the_toolkit_log_beads_are_excluded(self):
        """Catches (finding 8): ready_work dropped any bead whose title or label merely contains log ('recorder: log every
        dropped frame', 'api: decision table for retries', an audit-log label), hiding real work."""
        d, cfg = project()
        items = [{"title": "orch: decisions", "labels": ["orch-log"]}, {"title": "janus: decisions", "labels": ["janus-log"]},
                 {"title": "unit:x", "labels": ["unit"]}, {"title": "backlog: A1", "labels": ["backlog"]},
                 {"title": "instr: do it", "labels": ["instruction"]},
                 {"title": "recorder: log every dropped frame", "labels": []},
                 {"title": "station: log rotation is broken", "labels": []},
                 {"title": "api: decision table for retries", "labels": []},
                 {"title": "audit bead", "labels": ["audit-log"]}, {"title": "error bead", "labels": ["error-log"]},
                 {"title": "real work", "labels": []}]
        real = janus.Beads; janus.Beads = type("B", (), {"__init__": lambda s, c: None, "run": lambda s, *a: items})
        try: got = [i["title"] for i in janus.ready_work(cfg)]
        finally: janus.Beads = real
        self.assertEqual(got, ["recorder: log every dropped frame", "station: log rotation is broken",
                               "api: decision table for retries", "audit bead", "error bead", "real work"])


class Item16RelatedRepos(unittest.TestCase):
    def test_related_compares_only_units_in_the_same_repository(self):
        """Catches (item 16a): related:<a>:<b> matched a module name across different repositories (two projects that both change
        a docs module)."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u16a-"))
        def repo(n):
            R = base / n; R.mkdir()
            g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
            g("init", "-q", "-b", "main"); (R / "docs").mkdir(); (R / "docs/a.md").write_text(n + "\n"); g("add", "."); g("commit", "-qm", "a")
            return R, g
        R1, g1 = repo("r1")
        W1 = base / "w1"; g1("worktree", "add", "-q", str(W1), "-b", "u1"); (W1 / "docs/a.md").write_text("y\n")
        W2 = base / "w2"; g1("worktree", "add", "-q", str(W2), "-b", "u2"); (W2 / "docs/a.md").write_text("y2\n")
        R2, _ = repo("r2"); (R2 / "docs/a.md").write_text("z\n")   # a different repository (its own history)
        d, cfg = project()
        units = [{"id": "t-1", "name": "u1", "host": "here", "cwd": str(W1), "state": "working"},
                 {"id": "t-2", "name": "u2", "host": "here", "cwd": str(W2), "state": "working"},
                 {"id": "t-3", "name": "u3", "host": "here", "cwd": str(R2), "state": "working"}]
        saved = {k: getattr(janus, k) for k in ("all_units", "gate_results", "learned_posts")}
        janus.all_units = lambda cfg: {}; janus.gate_results = lambda cfg: {}; janus.learned_posts = lambda cfg: []
        try: cases = janus.related_cases(cfg, units)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertEqual(sorted(cases), [("u1", "u2")], "units in different repositories were called related")


class Item16NewWorktree(unittest.TestCase):
    def test_new_worktree_makes_a_fresh_branch(self):
        """Catches (item 16b): janus launch had no fresh-worktree option (without --cwd it defaulted to the project state root)."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u16b-")); R = base / "repo"; R.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main"); (R / "f").write_text("x\n"); g("add", "."); g("commit", "-qm", "a")
        d, cfg = project(); wt = d / "worktrees/u1"
        janus.new_worktree(R, wt, "u1", "main")
        self.assertTrue((wt / "f").is_file())
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, "u1")


class Item16Budget(unittest.TestCase):
    def test_budget_ignores_tiny_and_verdictless_runs(self):
        """Catches (item 16c): the default budget for implement/normal was $0.50 from a median of $0.09 over 23 runs that ended
        without a verdict, so a real unit started at a cap (the waste cause)."""
        d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 15\nledger = "out/ledger.tsv"\nkey_file = "k"\n[models.m]\nprovider = "api"\nkind = "api"\n')
        mc = cfg.models["m"]; runs = {}; au = {}
        for i in range(23):
            n = f"tiny{i}"; (d / "out" / n).mkdir(parents=True); (d / "out" / n / "REPORT.md").write_text("VERDICT: PENDING\n")
            runs[f"{n}.1"] = {"done": True, "cost": 0.09, "unit": n}; au[n] = {"name": n, "kind": "implement", "tier": "normal", "run": f"{n}.1", "status": "closed"}
        for i in range(3):
            n = f"real{i}"; (d / "out" / n).mkdir(parents=True); (d / "out" / n / "REPORT.md").write_text("VERDICT: DONE\ndone\n")
            runs[f"{n}.1"] = {"done": True, "cost": 2.0, "unit": n}; au[n] = {"name": n, "kind": "implement", "tier": "normal", "run": f"{n}.1", "status": "closed"}
        cfg.run.all_units = au
        real = janus.ledger_read; janus.ledger_read = lambda cfg, pv: (runs, [], [])
        try: b, why = janus.budget_for(cfg, mc, "implement", "normal")
        finally: janus.ledger_read = real
        self.assertGreaterEqual(b, 2.0, f"the tiny/verdictless runs skewed the budget: {b} ({why})")


class Item25Soak(unittest.TestCase):
    def test_a_broad_run_passes_a_negative_priority_pueue_accepts(self):
        """Catches (item 25a): `janus run --broad` passed the priority as `-o -5`, which pueue 4 rejects ('to pass -5 as a
        value, use -- -5'), so nothing was queued; the flag is now passed in a form pueue accepts."""
        import argparse, shutil
        d, cfg = project(); src = d / "wt"; src.mkdir()
        a = argparse.Namespace(host="here", lane="jobs", unit="u1", wt=str(src), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=True, command=["--", "true"], broad="a matrix build", needs=None)
        calls = []
        def fake_hrun(h, script, **k):
            calls.append(script)
            return (0, "7", "") if " add " in script else (0, "", "")
        saved = (janus.hrun, janus.tree_guard, janus.ensure_lanes, janus.write_cap)
        janus.hrun = fake_hrun; janus.tree_guard = lambda *a, **k: None
        janus.ensure_lanes = lambda h, cfg: ""; janus.write_cap = lambda *a, **k: (0, "", "")
        try: janus.cmd_run(cfg, a)
        finally: janus.hrun, janus.tree_guard, janus.ensure_lanes, janus.write_cap = saved
        add = [c for c in calls if " add " in c]
        self.assertTrue(add, calls)
        self.assertIn("-o=-5", add[0], "a broad run passed the negative priority in a form pueue rejects")
        if shutil.which("pueue"):
            ok = subprocess.run(["pueue", "add", "-o=-5"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
            bad = subprocess.run(["pueue", "add", "-o", "-5"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertNotIn("to pass", ok.stderr + ok.stdout, "pueue rejects the form janus writes")
            self.assertIn("to pass", bad.stderr + bad.stdout, "pueue now accepts the space form: update janus")

    def test_a_one_package_cargo_run_is_not_broad(self):
        """Catches (item 25b + finding 1): the whole-suite guard matched `--all-targets`/`--all-features` on a one-package cargo
        command, and any ` -p <word>` ANYWHERE on the line switched the guard off (so `mkdir -p logs && cargo test --workspace`
        was not refused, and a project's own first broad_pattern lost its meaning). Each `;`/`&&`/`|` command is judged on its
        own, `--all` never matches `--all-targets`, and the exception holds only for the cargo command itself."""
        d, cfg = project(); h = cfg.hosts["here"]
        saved = (janus.job_class, janus.host_history)
        janus.job_class = lambda *a: "job"; janus.host_history = lambda *a: []
        one_pkg = ["cargo test -p recorder --all-targets", "cargo test --package=recorder --all-targets",
                   "cd crates/x && cargo test --all-targets"]
        broad = ["cargo test --workspace", "cargo test --workspace --exclude big", "cargo test --workspace -p recorder",
                 "mkdir -p logs && cargo test --workspace 2>&1 | tee logs/t.log",
                 "cargo build -p tools && cargo test --workspace", "ssh -p 22 h true; cargo clippy --all"]
        try:
            for c in one_pkg: self.assertIsNone(janus.broad_why(cfg, "here", h, c), f"a one-package run was refused: {c}")
            for c in broad: self.assertTrue(janus.broad_why(cfg, "here", h, c), f"a whole-suite run slipped the guard: {c}")
            cfg.capc = {**cfg.capc, "broad_patterns": [r"pytest\s+tests/?(\s|$)", r"\bmake\b"]}
            self.assertTrue(janus.broad_why(cfg, "here", h, "pytest tests -p no:cacheprovider"),
                            "a stray -p switched off a project's own first broad_pattern")
        finally: janus.job_class, janus.host_history = saved


class Item26GateExec(unittest.TestCase):
    """Item 26: the wrapper `janus gate` queues (`janus gate-exec <name> --timeout-min <lim> -- <cmd>`) must run the gate
    command; its own argument line is parsed by the real CLI, not by a hand-made namespace."""
    def gate_exec(self, d, *argv):
        return subprocess.run([sys.executable, str(JANUS_PY), "gate-exec", *argv], cwd=d, capture_output=True, text=True)

    def test_the_wrapper_runs_the_command_after_the_timeout_option(self):
        """Catches (item 26): with a positional before it, argparse's REMAINDER command swallowed the `--timeout-min` option,
        so the wrapper ran `sh -c '--timeout-min 0 -- <cmd>'`; sh refuses that string ('sh: --: invalid option', exit 2) and no
        gate step ran at all (a real landing gate's whole log was bash's usage text)."""
        d, _ = project()
        r = self.gate_exec(d, "land-u1-x", "--timeout-min", "0", "--", "printf ran > out/marker")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual((d / "out/marker").read_text(), "ran")

    def test_the_wrapper_honours_the_timeout_option_from_the_command_line(self):
        """Catches (item 26, the silent half): the same REMAINDER swallowed `--timeout-min` itself, so `a.timeout_min` stayed 0
        and a gate's limit was never applied; the CLI must hand the limit to the wrapper (a 0.6 s limit stops a 30 s step)."""
        d, _ = project()
        t0 = time.time()
        r = self.gate_exec(d, "land-u2-x", "--timeout-min", "0.01", "--", "sleep 30")
        self.assertEqual(r.returncode, 124, r.stderr + r.stdout)
        self.assertLess(time.time() - t0, 15)
        self.assertIn("land-u2-x EXIT_timeout=124 RESULT: FAIL", (d / "out/gates.log").read_text())

    def test_a_gate_command_that_starts_with_a_dash_runs_intact(self):
        """Catches (finding 8): the wrapper ran `sh -c <cmd>`; when the command string starts with `-` (`-v x=1 true`), sh read
        it as its OWN option and died with 'sh: - : invalid option' (exit 2) -- no gate step ran at all."""
        d, _ = project()
        r = self.gate_exec(d, "land-u3-x", "--timeout-min", "0", "--", "-v x=1 true; printf ran > out/marker")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual((d / "out/marker").read_text(), "ran")


class FakeBeads:
    """A beads stand-in for a launch fixture: no database, no unit ever recorded."""
    def __init__(self, cfg): pass
    def units(self): return []
    def create(self, title, *a): return title
    def run(self, *a): return []
    def save(self, u): pass
    def comment(self, *a): pass


def launch_cfg(extra=""):
    d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 1\nledger = "out/ledger.tsv"\nkey_file = "k"\n'
                     '[models.m]\nprovider = "api"\nkind = "api"\napi_id = "x"\n' + extra)
    return d, cfg


def git_repo(prefix):
    base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-" + prefix + "-")); R = base / "repo"; R.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
    g("init", "-q", "-b", "main"); (R / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
    return base, R, g


class Item2VerdictEnding(unittest.TestCase):
    def test_a_report_ending_waiting_is_not_finished(self):
        """Catches (finding 2): units.verdict returned an earlier VERDICT: DONE although the report's last VERDICT line was
        WAITING, so the unit read as finished and its jobs became orphans (cancelled)."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True)
        (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nVERDICT: DONE - items 1-6\n(more work)\nWAITING here:9\nVERDICT: WAITING\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        (d / "out/u1/report.json").write_text(json.dumps({"verdict": "DONE"}))   # a stale json beside the prose
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u.get("_park"), ["here:9"]); self.assertNotIn("unit:u1:finished", [k for k, _ in F])


class Item1ParkedAfterDone(unittest.TestCase):
    def test_a_park_line_after_a_done_verdict_means_parked(self):
        """Catches (finding 1, high): units.verdict read the last VERDICT: DONE while a plain WAITING park line followed it, so a
        unit sent more work (janus resume --message) was judged finished and its still-running job was cancelled as an orphan."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True)
        (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\n## phase 1\nVERDICT: DONE - items 1-6\n## phase 2\nmore work\nWAITING here:9\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}},
                                               "9": {"status": {"Running": {}}, "label": "unit:u1@" + janus.project_id(cfg)}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u.get("_park"), ["here:9"]); self.assertEqual(u.get("state"), "parked")
        self.assertNotIn("unit:u1:finished", [k for k, _ in F])
        self.assertEqual([r for r, _ in janus.orphan_jobs(cfg, hs, [u])], [], "the parked unit's job was orphaned (cancelled)")

    def test_a_park_with_its_job_ids_on_the_following_line_parks_the_unit(self):
        """Catches (finding 1, high; fix-install6): verdict read the park word per line (the ids had to be on the SAME line) while
        waiting_line_jobs read the whole text (WAIT_RX's `\\s*` spans the line break), so the two readers disagreed: a report that
        ended with a bare `WAITING` line and its job ids on the next line (or after a `WAITING:`) was judged finished and its
        still-running job became an orphan the watcher cancels."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True); p = d / "out/u1/REPORT.md"
        head = "VERDICT: PENDING\n## phase 1\nVERDICT: DONE - items 1-6\n## phase 2\nmore work\n"
        for tail, want in (("WAITING\n- here:9\n", ["here:9"]), ("WAITING:\nhere:9 here:10\n", ["here:9", "here:10"])):
            p.write_text(head + tail)
            self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}), tail)
            self.assertEqual(janus.waiting_line_jobs(cfg, {"name": "u1", "host": "here", "started": 0}), want, tail)
        p.write_text(head + "WAITING\n- here:9\n")
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}},
                                               "9": {"status": {"Running": {}}, "label": "unit:u1@" + janus.project_id(cfg)}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u.get("_park"), ["here:9"]); self.assertEqual(u.get("state"), "parked")
        self.assertEqual([r for r, _ in janus.orphan_jobs(cfg, hs, [u])], [], "the parked unit's job was orphaned (cancelled)")

    def test_the_last_of_verdict_line_and_park_line_decides(self):
        """Catches (finding 1, high): the rule is 'the last of {VERDICT line, WAITING line} decides' for EVERY unit; a closing
        VERDICT: DONE after an earlier park line is finished (no false park), and a park line after it is parked."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True); p = d / "out/u1/REPORT.md"
        p.write_text("VERDICT: PENDING\nWAITING here:9\nVERDICT: DONE - all items\n")
        self.assertEqual(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}), "VERDICT: DONE - all items")
        p.write_text("VERDICT: DONE - all items\nWAITING here:9\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        p.write_text("VERDICT: DONE - all items\nWAITING here:9\nVERDICT: DONE - all done\n")
        self.assertEqual(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}), "VERDICT: DONE - all done")

    def test_a_park_word_line_with_ids_janus_cannot_read_is_not_a_park(self):
        """Catches (finding 5): ANY line starting with the park word counted as a park, so a finished report with a prose
        `**WAITING** jobs: none.` line, or a park line naming a host janus does not know (`mac:9`) or a bare `9`, lost its
        verdict -- and a capped unit was resumed once for a wrap-up it did not need."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True); p = d / "out/u1/REPORT.md"
        for tail in ("**WAITING** jobs: none.\n", "- WAITING mac:9 (resumed, exit 0)\n", "WAITING 9\n"):
            p.write_text("VERDICT: PENDING\nall done\nVERDICT: DONE - all items\n" + tail)
            self.assertEqual(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}), "VERDICT: DONE - all items", tail)
        p.write_text("VERDICT: PENDING\nVERDICT: DONE - all items\nWAITING here:9\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        self.assertIn("last line", janus.PARK_RULE, "the brief header must say the closing VERDICT line is the last line")


class Item2ResumeGuard(unittest.TestCase):
    """Finding 2 (medium-high): `janus resume <u> --message ...` had no guard: it started a second agent beside a running one
    and marked a still-running job handled (so it became an orphan, cancelled, when the new run parked again)."""

    def setUp(self):
        self.d, self.cfg = project()
        self.calls = []
        self.saved = {k: getattr(janus, k) for k in ("Beads", "probe_all", "herdr_agents", "unit_state", "job_state", "resume_unit", "waiting_line_jobs")}

    def tearDown(self):
        for k, v in self.saved.items(): setattr(janus, k, v)

    def _run(self, u, **kw):
        janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: kw.get("units", [u]),
                                     "comment": lambda s, *a: None, "save": lambda s, x: None})
        janus.probe_all = lambda cfg: {"here": probe_of()}
        janus.herdr_agents = lambda cfg: {}
        janus.unit_state = lambda x, hs, ag: kw.get("state", "gone")
        janus.job_state = lambda hs, j: (kw.get("job", "done"), "")
        janus.waiting_line_jobs = lambda cfg, x: kw.get("jobs", ["here:9"])
        janus.resume_unit = lambda cfg, x, msg, budget=None: (self.calls.append(("resume", msg, budget)), "resumed")[1]
        a = type("A", (), {"unit": u["name"], "message": kw.get("message", "go"), "force": kw.get("force", False),
                           "budget": kw.get("budget"), "runtime": None, "here": False})()
        janus.cmd_resume(self.cfg, a)
        return u

    def test_message_refuses_while_the_agent_runs(self):
        """Catches (finding 2): `resume --message` with a Running agent task started a second agent on the same session in the
        same tree; and the old refusal offered --force -- the one path that still started the second agent beside it."""
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(self.d / "wt")}
        with self.assertRaises(janus.Err) as e: self._run(u, state="working", job="running", message="go")
        self.assertIn("still working", str(e.exception)); self.assertNotIn("--force", str(e.exception))
        self.assertEqual(self.calls, [], "a second agent was started beside the running one")
        with self.assertRaises(janus.Err): self._run(u, state="working", job="running", message="go", force=True)
        self.assertEqual(self.calls, [], "--force started a second agent beside the running one")

    def test_message_refuses_while_its_jobs_are_unfinished(self):
        """Catches: the jobs guard was skipped entirely with --message, so a unit whose job still ran was resumed."""
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(self.d / "wt")}
        with self.assertRaises(janus.Err) as e: self._run(u, state="gone", job="running", message="go")
        self.assertIn("here:9", str(e.exception)); self.assertIn("not finished", str(e.exception)); self.assertEqual(self.calls, [])

    def test_a_forced_resume_never_marks_a_running_job_handled(self):
        """Catches: resume added every park-line job to handled_jobs even while it ran, so the next park line's same job was an
        orphan (cancelled)."""
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(self.d / "wt")}
        self._run(u, state="gone", job="running", message="go", force=True)
        self.assertEqual(len(self.calls), 1, "the forced resume did not run")
        self.assertNotIn("here:9", u.get("handled_jobs", []), "a still-running job was marked handled")

    def test_resume_refuses_the_tree_of_another_open_unit(self):
        """Catches: resume had no own-tree check, so a predecessor whose handover was refused was put back into the tree its
        successor now works in."""
        W = self.d / "wt"; W.mkdir()
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(W)}
        u2 = {"id": "t-2", "name": "u2", "host": "here", "cwd": str(W)}
        with self.assertRaises(janus.Err) as e: self._run(u, units=[u, u2], state="gone", job="done", message="go")
        self.assertIn("u2", str(e.exception)); self.assertEqual(self.calls, [])

    def test_hand_resume_works_for_tmux_a_blocked_herdr_unit_and_a_review_unit(self):
        """Catches (finding 2): a tmux unit read as 'working' for as long as its session existed, a herdr unit blocked on a
        question could not be answered, and a review unit that shares the tree it reviews was refused -- so the routine answer
        became --force, the one path that double-starts an agent."""
        W = self.d / "wt"; W.mkdir()
        tm = {"id": "t-1", "name": "u1", "host": "here", "via": "tmux", "model": "m", "started": 0, "cwd": str(W)}
        self._run(tm, state="working", job="done", message="continue")
        self.assertEqual(len(self.calls), 1, "a tmux unit's hand resume was refused"); self.calls.clear()
        hd = {"id": "t-2", "name": "u2", "host": "here", "via": "herdr", "pane": "p", "model": "m", "started": 0, "cwd": str(W)}
        self._run(hd, state="blocked", job="done", message="answer")
        self.assertEqual(len(self.calls), 1, "a blocked herdr unit's answer was refused"); self.calls.clear()
        rv = {"id": "t-3", "name": "u3", "host": "here", "via": "pueue", "task": 7, "model": "m", "started": 0, "cwd": str(W), "kind": "review"}
        other = {"id": "t-4", "name": "u4", "host": "here", "cwd": str(W)}
        self._run(rv, units=[rv, other], state="gone", job="done", message="go")
        self.assertEqual(len(self.calls), 1, "a review unit could not resume in the tree it reviews")


class Item3Handover(unittest.TestCase):
    def _close(self, cfg):
        return janus.cmd_close(cfg, type("A", (), {"unit": "u1", "force": True, "handover": True, "reason": "handover"})())

    def _beads(self, u):
        return type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u], "comment": lambda s, *a: None,
                              "run": lambda s, *a: [], "save": lambda s, x: None, "close": lambda s, *a, **k: None})

    def test_handover_refuses_when_the_remote_copy_has_uncollected_work(self):
        """Catches (finding 3): launch --resume-of closed a remote predecessor with force, skipping the uncollected guard, and
        deleted its tree and output (its dirty tree was its copy on the host, not the successor's)."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "cwd": "/w/u1"}
        saved = {k: getattr(janus, k) for k in ("Beads", "uncollected", "X", "save_task_log", "board", "note_event", "herdr")}
        janus.Beads = self._beads(u)
        janus.uncollected = lambda cfg, u: "its copy on far has changes or output not collected yet"
        calls = []; janus.X = lambda cfg, h, cmd, **k: (calls.append(cmd), (0, "", ""))[1]
        janus.save_task_log = lambda *a, **k: None; janus.board = lambda *a, **k: None; janus.note_event = lambda *a, **k: None
        janus.herdr = lambda *a, **k: None
        try:
            with self.assertRaises(janus.Err) as e: self._close(cfg)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn("collect", str(e.exception))
        self.assertFalse(any("rm -rf" in c for c in calls), "the remote tree was deleted")

    def test_handover_refuses_a_predecessor_whose_agent_still_runs(self):
        """Catches (finding 3): nothing checked that the predecessor's agent had ended, so a running predecessor was closed in
        beads and kept running unowned in the tree the successor now works in."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "cwd": "/w/u1"}
        saved = {k: getattr(janus, k) for k in ("Beads", "uncollected", "probe_all", "herdr_agents", "unit_state", "X")}
        janus.Beads = self._beads(u)
        janus.uncollected = lambda cfg, u: None
        janus.probe_all = lambda cfg: {}; janus.herdr_agents = lambda cfg: {}; janus.unit_state = lambda u, hs, ag: "working"
        calls = []; janus.X = lambda cfg, h, cmd, **k: (calls.append(cmd), (0, "", ""))[1]
        try:
            with self.assertRaises(janus.Err) as e: self._close(cfg)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn("still", str(e.exception)); self.assertFalse(any("rm -rf" in c for c in calls))


class Item3AdmitClock(unittest.TestCase):
    def test_the_give_up_clock_starts_when_the_job_is_blocked_not_at_enqueue(self):
        """Catches (finding 3): the give-up clock read JANUS_TQ (the enqueue time), so a job that had queued long behind other
        work was dropped on its first blocked poll (rc 124, the same as a command's own timeout) without ever running. The clock
        now counts only the time spent blocked, and a never-admitted job exits 121, distinct from a timed-out command (124)."""
        import argparse
        d, cfg = project('\n[capacity]\nmin_disk_gb = 99999\n')
        h = cfg.hosts["here"]; janus.write_cap(cfg, "here", h)
        r = argparse.Namespace(host="here", lane="jobs", unit=None, wt=str(d), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=False, command=["--", "true"], broad=None)
        old = os.environ.get("JANUS_TQ"); os.environ["JANUS_TQ"] = str(int(time.time()) - 7300)   # the job queued 7300 s
        try: js = janus.job_script(cfg, "here", h, r, "u", "true", "j-1")
        finally:
            if old is None: os.environ.pop("JANUS_TQ", None)
            else: os.environ["JANUS_TQ"] = old
        t0 = time.time()
        p = subprocess.run(["bash"], input=js, text=True, capture_output=True, timeout=60,
                           env={**os.environ, "JANUS_POLL_S": "1", "JANUS_ADMIT_MAX_S": "3", "JANUS_ADMIT_REPORT_S": "1"})
        self.assertNotEqual(p.returncode, 124, "a queued job was dropped as if its command had timed out: " + p.stdout + p.stderr)
        self.assertEqual(p.returncode, 121, p.stdout + p.stderr)
        self.assertGreaterEqual(time.time() - t0, 2.0, "it was dropped before its blocked clock ran out: " + p.stdout)
        # finding 7: assert the BEHAVIOUR (clock ran out, exit 121), not a wall-clock string. The script's clock is whole
        # seconds and one poll can straddle a second, so the count is 3, 4, ... -- never the queued 7300 s.
        m = re.search(r"not admitted after (\d+)s blocked \(limit 3s", p.stdout)
        self.assertIsNotNone(m, "the give-up message did not say it was not admitted after Ns blocked: " + p.stdout)
        self.assertGreaterEqual(int(m.group(1)), 3, p.stdout)
        self.assertLess(int(m.group(1)), 60, "the clock counted the 7300 s queue wait, not the time blocked: " + p.stdout)
        self.assertNotIn("after 73", p.stdout, "the clock started at enqueue, not at the first blocked poll: " + p.stdout)


class Item4Localtest(unittest.TestCase):
    def test_the_reviews_case_table(self):
        """Catches (finding 4): test_command judged only a segment's first word, so a real test/build run was missed (a pytest
        console script ps shows as the interpreter, `python -m pytest`, uv/poetry run, rustup/cargo-nextest, a shell -c,
        xargs/arch/flock, node npm), while a word pytest inside a quoted argument (an ack or a commit message) was flagged."""
        caught = [
            "/opt/homebrew/Cellar/python/3.12/Frameworks/Python.app/Contents/MacOS/Python fake/bin/pytest -q tests",
            "python3 -m pytest tests/calibration", "uv run pytest -q", "poetry run pytest", "node /opt/homebrew/bin/npm test",
            "cargo-nextest nextest run", "rustup run stable cargo test", "bash -lc 'cargo test'", "xargs -n1 pytest",
            "arch -arm64 pytest", "flock /tmp/l cargo build", "zsh -c -l source ~/.zshrc && eval 'pytest -q tests'",
            "pytest -q", "npm test", "cargo +nightly test", "time cargo build", "nice -n 10 /usr/bin/pytest tests/",
            # finding 3: the runner's own options, hatch's env:cmd, cargo's own options, and the joined python -mpytest
            "uv run --frozen pytest -q", "uv run --with pytest-xdist pytest", "uv run -q pytest", "uv run --project p pytest tests",
            "poetry run -q pytest", "hatch run test:pytest", "cargo -q test", "cargo --locked test -p x", "cargo --offline build",
            "python3 -mpytest",
        ]
        missed = [c for c in caught if not janus.test_command(c)]
        self.assertEqual(missed, [], f"a real test/build run was not judged one: {missed}")
        not_test = ["git commit -m 'run (pytest -q) before push'", "janus ack unit:x 'gate failed; pytest tests/a exit 1'",
                    "cargo run --bin build", "janus ack unit:x 'cargo test failed'",
                    # finding 3: a later word naming pytest after a Python interpreter is not a run
                    "python3 -m pip install pytest", "python3 -m venv pytest", "python3 tool.py --runner pytest"]
        false_pos = [c for c in not_test if janus.test_command(c)]
        self.assertEqual(false_pos, [], f"a command that only mentions a test was judged one: {false_pos}")


class Item5HandoverOrder(unittest.TestCase):
    def test_a_handover_is_refused_before_the_successor_starts(self):
        """Catches (finding 5): launch accepted the predecessor's tree, started the successor, and only then refused to close the
        predecessor (its Err became a 'note:'), so two agents ran in one tree or the successor worked from a stale base."""
        base, R, g = git_repo("i5a")
        W = base / "wt"; g("worktree", "add", "-q", str(W), "-b", "u1")
        d, cfg = launch_cfg(); cfg.run.dry = True
        b = d / "briefs/u2.md"; b.write_text("TIER: NORMAL\nSCOPE: x\nREPORT.md\nDONE-WHEN: done\n")
        u1 = {"id": "t-1", "name": "u1", "host": "here", "cwd": str(W), "state": "working"}
        calls = []
        saved = {k: getattr(janus, k) for k in ("Beads", "herdr", "choose", "model_ok", "read_secret", "budget_for", "reserve_budget",
                                                "X", "waste_block", "task_status", "confirm_started", "board")}
        janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u1], "create": lambda s, *a: "t-9",
                                     "run": lambda s, *a: [], "save": lambda s, x: None, "comment": lambda s, *a: None})
        janus.waste_block = lambda cfg: None; janus.choose = lambda *a, **k: ("here", "here", "ok")
        janus.model_ok = lambda cfg, m: (True, ""); janus.read_secret = lambda pv: None
        janus.budget_for = lambda *a, **k: (1.0, "stub"); janus.herdr = lambda *a, **k: None; janus.board = lambda *a, **k: None
        janus.reserve_budget = lambda *a, **k: (calls.append("reserve"), "u2.r1")[1]
        janus.X = lambda cfg, h, cmd, **k: (calls.append(str(cmd)[:30]), (0, "", ""))[1]
        janus.task_status = lambda h, t: {"status": {"Running": {}}}; janus.confirm_started = lambda *a, **k: ("running", "")
        import argparse
        ns = argparse.Namespace(unit="u2", kind="implement", tier="normal", host="here", model="m", brief=str(b), cwd=str(W),
                                budget=None, test_host=None, allow_main=True, of=None, report=None, resume_of="u1", item=None,
                                new_worktree=None, from_ref=None, probes={"here": probe_of()}, share_with=None)
        try:
            with self.assertRaises(janus.Err) as e: janus.cmd_launch(cfg, ns)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn("u1", str(e.exception)); self.assertIn("still working", str(e.exception))
        self.assertEqual(calls, [], "the successor was started before the handover was refused")

    def test_close_refuses_a_handover_whose_state_cannot_be_told(self):
        """Catches (finding 5): a handover closed a predecessor whose state was 'unknown' (its pueue status or probe failed), so
        it might still be running in the tree the successor works in."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "cwd": "/w/u1"}
        saved = {k: getattr(janus, k) for k in ("Beads", "uncollected", "probe_all", "herdr_agents", "unit_state", "X")}
        janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u], "comment": lambda s, *a: None,
                                     "run": lambda s, *a: [], "save": lambda s, x: None, "close": lambda s, *a, **k: None})
        janus.uncollected = lambda cfg, u: None
        janus.probe_all = lambda cfg: {}; janus.herdr_agents = lambda cfg: {}; janus.unit_state = lambda u, hs, ag: "unknown"
        calls = []; janus.X = lambda cfg, h, cmd, **k: (calls.append(cmd), (0, "", ""))[1]
        try:
            with self.assertRaises(janus.Err) as e:
                janus.cmd_close(cfg, type("A", (), {"unit": "u1", "force": True, "handover": True, "reason": "handover"})())
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn("unknown", str(e.exception)); self.assertFalse(any("rm -rf" in c for c in calls))


class Item5ReportSync(unittest.TestCase):
    def _remote(self, state="done", via="pueue", **kw):
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        (d / "out/u1").mkdir(parents=True); (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\n")
        u = {"id": "t-1", "name": "u1", "host": "far", "via": via, "task": 5, "model": "m", "started": 0, "state": state, **kw}
        return d, cfg, u

    def _bd(self): return type("B", (), {"save": lambda self, u: None})()

    def test_a_failed_report_sync_never_orphans_a_remote_unit(self):
        """Catches (finding 5): when the report sync failed, the orphan decision was taken on the stale local report and a
        parked remote unit's jobs were cancelled."""
        d, cfg, u = self._remote()
        hs = {"far": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}},
                                               "9": {"status": {"Running": {}}, "label": "unit:u1@t"}}})}
        saved = janus.save_output, janus.remote_patch
        janus.remote_patch = lambda *a, **k: None; janus.save_output = lambda cfg, u: False
        try:
            janus.sync_remote_outputs(cfg, [u], self._bd())
            got = [r for r, _ in janus.orphan_jobs(cfg, hs, [u])]
        finally: janus.save_output, janus.remote_patch = saved
        self.assertEqual(got, [], "a failed sync let a remote unit's jobs be cancelled")

    def test_the_end_of_turn_backup_patch_is_written_by_the_sync(self):
        """Catches (finding 5): the sync marked the unit saved before refresh_remote wrote the end-of-turn backup.patch, so it
        was not written until the periodic backup."""
        d, cfg, u = self._remote()
        calls = []; saved = janus.save_output, janus.remote_patch
        janus.remote_patch = lambda cfg, h, n, f: calls.append((n, f)); janus.save_output = lambda cfg, u: True
        try: janus.sync_remote_outputs(cfg, [u], self._bd())
        finally: janus.save_output, janus.remote_patch = saved
        self.assertIn(("u1", "backup.patch"), calls)

    def test_a_remote_tmux_unit_is_synced_too(self):
        """Catches (finding 5): a remote tmux unit has no pueue task, so `saved == task` was None == None and it was never
        synced by this path."""
        d, cfg, u = self._remote(via="tmux"); u.pop("task")
        got = []; saved = janus.save_output, janus.remote_patch
        janus.remote_patch = lambda *a, **k: None; janus.save_output = lambda cfg, u: (got.append(u["name"]), True)[1]
        try: janus.sync_remote_outputs(cfg, [u], self._bd())
        finally: janus.save_output, janus.remote_patch = saved
        self.assertEqual(got, ["u1"])


class Item9LandFloor(unittest.TestCase):
    def test_the_land_lane_floor_honours_the_host_and_defaults_to_five(self):
        """Catches (finding 9): the land lane's disk floor default went from its own 5 GB to admission's 10, so a landing waits
        on a Mac with 5-9 GB free; the host's `[hosts.<h>] min_disk_gb` (the Mac's 5) must win, then `[capacity]`, then 5."""
        import argparse
        d, cfg = project(); h = cfg.hosts["here"]
        r = argparse.Namespace(host="here", lane="land", unit=None, wt=str(d), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=False, command=["--", "true"], broad=None)
        js = janus.job_script(cfg, "here", h, r, "u", "true", "j-1")
        self.assertIn('[ "$d" -ge 5 ]', js, "the land lane no longer defaults to its own 5 GB floor")
        h["min_disk_gb"] = 7
        self.assertIn('[ "$d" -ge 7 ]', janus.job_script(cfg, "here", h, r, "u", "true", "j-2"), "the host's own floor was ignored")
        h.pop("min_disk_gb"); cfg.capc["min_disk_gb"] = 9
        self.assertIn('[ "$d" -ge 9 ]', janus.job_script(cfg, "here", h, r, "u", "true", "j-3"), "[capacity] min_disk_gb was ignored")

    def test_the_land_lane_gives_up_with_a_message(self):
        """Catches (finding 9): the land lane's disk wait had no bound and printed once, so a nearly-full volume hung the landing
        silently (item 21's bound covers ADMIT, not this wait)."""
        import argparse
        d, cfg = project('\n[capacity]\nmin_disk_gb = 99999\n')
        h = cfg.hosts["here"]; janus.write_cap(cfg, "here", h)
        r = argparse.Namespace(host="here", lane="land", unit=None, wt=str(d), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=False, command=["--", "true"], broad=None)
        js = janus.job_script(cfg, "here", h, r, "u", "true", "j-1")
        p = subprocess.run(["bash"], input=js, text=True, capture_output=True, timeout=60,
                           env={**os.environ, "JANUS_LAND_MAX_S": "3", "JANUS_LAND_REPORT_S": "1", "JANUS_LAND_POLL_S": "1"})
        self.assertEqual(p.returncode, 121, p.stdout + p.stderr)
        self.assertIn("land lane waiting", p.stdout); self.assertIn("giving up", p.stdout); self.assertIn("floor 99999", p.stdout)


class Item9BudgetHistory(unittest.TestCase):
    def test_budget_counts_capped_cheap_and_tidied_runs(self):
        """Catches (finding 9): capped runs (which show the budget was too small), a finished run whose report was tidied, and
        cheap finished runs were all dropped from the history."""
        d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 15\nledger = "out/ledger.tsv"\nkey_file = "k"\n'
                         '[models.m]\nprovider = "api"\nkind = "api"\n')
        mc = cfg.models["m"]; runs = {}; au = {}
        def add(n, cost, kind, report=None, capped=False):
            (d / "out" / n).mkdir(parents=True)
            if report is not None: (d / "out" / n / "REPORT.md").write_text(report)
            if capped: (d / "out" / n / "api.json").write_text(json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": cost}))
            runs[f"{n}.1"] = {"done": True, "cost": cost, "unit": n, "budget": cost, "how": "done rc=0"}
            au[n] = {"name": n, "kind": kind, "tier": "normal", "run": f"{n}.1", "status": "closed"}
        for i in range(3): add(f"cap{i}", 5.0, "implement", "VERDICT: PENDING\n", capped=True)
        for i, c in enumerate((1.1, 1.2, 1.4)): add(f"fin{i}", c, "implement", "VERDICT: DONE\ndone\n")
        add("tidied", 3.0, "implement")   # its out/ folder was tidied: no REPORT.md at all
        for i, c in enumerate((0.30, 0.40, 0.45)): add(f"rev{i}", c, "review", "VERDICT: DONE\ndone\n")
        cfg.run.all_units = au
        real = janus.ledger_read; janus.ledger_read = lambda cfg, pv: (runs, [], [])
        try:
            b, why = janus.budget_for(cfg, mc, "implement", "normal")
            br, whyr = janus.budget_for(cfg, mc, "review", "normal")
        finally: janus.ledger_read = real
        self.assertGreaterEqual(b, 3.0, f"capped/tidied runs were dropped: {b} ({why})")
        self.assertLess(br, 15, f"the cheap review runs never taught it: {br} ({whyr})")


class Item10Residues(unittest.TestCase):
    def test_migrate_rewrites_only_what_it_renames(self):
        """Catches (finding 10, old 1): migrate rewrote a kit folder (/opt/orchkit -> /opt/janus) and a remote state folder
        (/home/dev/orch/.orch -> .../.janus) although it renames neither, pointing config values at paths that do not exist."""
        d = Path(tempfile.mkdtemp(prefix="janus-mig-")); p = d / "janus.toml"
        p.write_text('[project]\nname = "t"\nstate = ".orch"\n'
                     '[hosts.far]\nroot = "/home/dev/orch"\nkit = "/opt/orchkit/bin"\nstate_dir = "/home/dev/orch/.orch"\n'
                     f'[paths]\nledger = "{d}/.orch/ledger.tsv"\n')
        janus.rewrite_config(p, dry=False)
        t = p.read_text()
        self.assertIn('state = ".janus"', t)                              # the project's own state folder is renamed
        self.assertIn(f'ledger = "{d}/.janus/ledger.tsv"', t)             # ... and a path under it
        self.assertIn('kit = "/opt/orchkit/bin"', t, "a kit folder migrate does not rename was rewritten")
        self.assertIn('state_dir = "/home/dev/orch/.orch"', t, "a remote state folder was rewritten")
        self.assertIn('root = "/home/dev/orch"', t)

    def test_a_job_in_a_unit_tree_outside_the_project_root_is_adopted(self):
        """Catches (finding 10, old 6): migrate adopted a job only when its path was under the project root, so a unit's own
        worktree outside the root (as every unit's is here) was left as machine state, never adopted."""
        d = Path(tempfile.mkdtemp(prefix="janus-mig-")); (d / "janus.toml").write_text('[project]\nname = "t"\nid = "proj"\n')
        tree = Path(tempfile.mkdtemp(prefix="janus-unit-"))
        tasks = {"1": {"label": "unit:u1", "path": str(tree), "status": {"Queued": {}}, "command": "true"}}
        def fake_sh(cmd, **k):
            if list(cmd)[-2:] == ["status", "--json"]: return 0, json.dumps({"tasks": tasks}), ""
            if list(cmd)[:2] == ["tmux", "list-sessions"]: return 1, "", "no server"
            return 0, "", ""
        saved = (janus.sh, janus._local_pueue, janus.all_units)
        janus.sh = fake_sh; janus._local_pueue = lambda view: ["pueue"]
        janus.all_units = lambda cfg: {"u1": {"cwd": str(tree)}}
        try: out = "\n".join(janus.adopt_items(d, dry=True, config=d / "janus.toml"))
        finally: janus.sh, janus._local_pueue, janus.all_units = saved
        self.assertIn("job 1: unit:u1 -> unit:u1@proj", out); self.assertNotIn("left alone", out)

    def test_a_placeholder_already_quoted_in_the_config_is_not_quoted_twice(self):
        """Catches (finding 6 = review finding 10's follow-up): a config that quotes the placeholder (`--wt "{wt}"`) must give a
        single shell-quoted path, not the raw value inside the config's quotes (which ran `$(...)`)."""
        self.assertEqual(janus.final_gate_cmd({"final_gate": 'gate.sh --wt "{wt}" --branch {branch}'}, "u", "/Users/me/My Code/wt", "b"),
                         "gate.sh --wt '/Users/me/My Code/wt' --branch b")

    def test_an_earlier_run_of_a_capped_unit_is_not_counted_as_capped(self):
        """Catches (finding 10, old 9): capped was read from the unit's latest api.json, so an earlier tiny run of a unit whose
        LAST run was capped counted as a capped run and taught the budget as if a cap proved the budget was too small."""
        d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 15\nledger = "out/ledger.tsv"\nkey_file = "k"\n'
                         '[models.m]\nprovider = "api"\nkind = "api"\n')
        od = d / "out/c"; od.mkdir(parents=True); (od / "REPORT.md").write_text("VERDICT: PENDING\n")
        (od / "api.json").write_text(json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": 5.0}))
        (od / "api-c.2.json").write_text(json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": 5.0}))
        (od / "api-c.1.json").write_text(json.dumps({"subtype": "success", "total_cost_usd": 0.01}))
        runs = {"c.1": {"done": True, "cost": 0.01, "unit": "c", "budget": 0.01, "how": "done rc=0"},
                "c.2": {"done": True, "cost": 5.0, "unit": "c", "budget": 5.0, "how": "capped"}}
        cfg.run.all_units = {"c": {"name": "c", "kind": "implement", "tier": "normal", "run": "c.2", "status": "open"}}
        real = janus.ledger_read; janus.ledger_read = lambda cfg, pv: (runs, [], [])
        try: h = janus.cost_history(cfg, "api")
        finally: janus.ledger_read = real
        self.assertEqual(h.get(("implement", "normal")), [5.0], "the earlier tiny run counted as capped")

    def test_a_second_vanished_file_does_not_fail_the_commit(self):
        """Catches (finding 10, old 11): when a second file vanished between listing and add, the retry's failure was an error,
        so under a busy monitor commit-state failed where item 7 wanted it to carry on."""
        d = Path(tempfile.mkdtemp(prefix="janus-fix-")); (d / "janus.toml").write_text('[project]\nname = "t"\n'); (d / "out").mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True, check=True)
        g("init", "-q"); (d / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a"); (d / "b").write_text("2\n")
        cfg = janus.Cfg(d)
        real_sh, real_push = janus.sh, janus.push_cmd; n = [0]
        def sh_stub(cmd, **k):
            c = list(cmd)
            if c[:3] == ["git", "add", "-A"]:
                n[0] += 1
                if n[0] <= 2: return 128, "", f"fatal: unable to stat gone{n[0]}.tmp: No such file or directory"
            return real_sh(cmd, **k)
        janus.sh = sh_stub; janus.push_cmd = lambda repo, cfg=None: ["true"]
        try: janus.cmd_commit_state(cfg, type("A", (), {"message": "state"})())
        finally: janus.sh, janus.push_cmd = real_sh, real_push
        self.assertEqual(g("log", "-1", "--format=%s").stdout.strip(), "state")

    def test_a_staged_merge_of_a_landing_branch_that_moved_on_still_uses_its_side(self):
        """Catches (finding 10, old 14): a staged merge whose landing branch advanced after `git merge` fell back to the merge
        base, so the landing branch's files counted as the unit's and it overlapped every other such unit."""
        base, R, g = git_repo("u10m")
        g("checkout", "-q", "-b", "unit", "main"); (R / "u").write_text("u\n"); g("add", "."); g("commit", "-qm", "unit")
        g("checkout", "-q", "main"); (R / "c").write_text("c\n"); g("add", "."); g("commit", "-qm", "main adds c")
        g("checkout", "-q", "unit"); g("merge", "--no-commit", "--no-ff", "main")   # staged: MERGE_HEAD = main at c
        W = base / "mainwt"; g("worktree", "add", "-q", str(W), "main")             # main moves on in its own worktree
        (W / "d").write_text("d\n")
        subprocess.run(["git", "-C", str(W), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(W), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "main moves on"], capture_output=True, check=True)
        d, cfg = project(); cfg.c["landing"] = {"branch": "main"}
        u = {"id": "t-1", "name": "u1", "host": "here", "cwd": str(R)}
        br, fs = janus.unit_changes(cfg, u, [u])
        self.assertIn("u", fs); self.assertNotIn("c", fs, "the landing branch's own file counted as the unit's change")

    def test_a_shallow_clone_is_related_to_a_clone_of_the_same_repository(self):
        """Catches (finding 10, old 10): repo identity was the root commit, so a shallow clone (whose root is only its own
        boundary) was never related to a clone of the same repository, and the whole-history walk ran on every check."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u10s-")); R = base / "r"; R.mkdir()
        g = lambda c, *a: subprocess.run(["git", "-C", str(c), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g(R, "init", "-q", "-b", "main"); (R / "docs").mkdir(); (R / "docs/a.md").write_text("x\n"); g(R, "add", "."); g(R, "commit", "-qm", "a")
        (R / "docs/b.md").write_text("y\n"); g(R, "add", "."); g(R, "commit", "-qm", "b")
        C1 = base / "c1"; g(base, "clone", "-q", str(R), str(C1))
        C2 = base / "c2"; g(base, "clone", "-q", "--depth", "1", "file://" + str(R), str(C2))
        W1 = base / "w1"; g(R, "worktree", "add", "-q", str(W1), "-b", "u1"); (W1 / "docs/a.md").write_text("y\n")
        W2 = base / "w2"; g(C2, "worktree", "add", "-q", str(W2), "-b", "u2"); (W2 / "docs/a.md").write_text("y2\n")
        d, cfg = project()
        units = [{"id": "t-1", "name": "u1", "host": "here", "cwd": str(W1), "state": "working"},
                 {"id": "t-2", "name": "u2", "host": "here", "cwd": str(W2), "state": "working"}]
        saved = {k: getattr(janus, k) for k in ("all_units", "gate_results", "learned_posts")}
        janus.all_units = lambda cfg: {}; janus.gate_results = lambda cfg: {}; janus.learned_posts = lambda cfg: []
        try: cases = janus.related_cases(cfg, units)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn(("u1", "u2"), cases, "a shallow clone of the same repository was not related")


class Item10Clones(unittest.TestCase):
    def test_two_clones_of_one_repo_are_related(self):
        """Catches (finding 10): two clones of one repository compared different (git common dir), so units in separate clones
        were never related."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u10c-")); R = base / "r"; R.mkdir()
        g = lambda c, *a: subprocess.run(["git", "-C", str(c), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g(R, "init", "-q", "-b", "main"); (R / "docs").mkdir(); (R / "docs/a.md").write_text("x\n"); g(R, "add", "."); g(R, "commit", "-qm", "a")
        C = base / "clone"; g(base, "clone", "-q", str(R), str(C))
        W1 = base / "w1"; g(R, "worktree", "add", "-q", str(W1), "-b", "u1"); (W1 / "docs/a.md").write_text("y\n")
        W2 = base / "w2"; g(C, "worktree", "add", "-q", str(W2), "-b", "u2"); (W2 / "docs/a.md").write_text("y2\n")
        d, cfg = project()
        units = [{"id": "t-1", "name": "u1", "host": "here", "cwd": str(W1), "state": "working"},
                 {"id": "t-2", "name": "u2", "host": "here", "cwd": str(W2), "state": "working"}]
        saved = {k: getattr(janus, k) for k in ("all_units", "gate_results", "learned_posts")}
        janus.all_units = lambda cfg: {}; janus.gate_results = lambda cfg: {}; janus.learned_posts = lambda cfg: []
        try: cases = janus.related_cases(cfg, units)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn(("u1", "u2"), cases, "two clones of one repository were not related")


class Item11CommitStateAdd(unittest.TestCase):
    def test_a_file_that_fails_to_add_is_an_error(self):
        """Catches (finding 11): commit-state reported success ('committing what was staged') while a file that failed to add
        every time was left out."""
        d = Path(tempfile.mkdtemp(prefix="janus-fix-")); (d / "janus.toml").write_text('[project]\nname = "t"\n'); (d / "out").mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True, check=True)
        g("init", "-q"); (d / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
        (d / "b").write_text("2\n")
        cfg = janus.Cfg(d)
        real_sh, real_push = janus.sh, janus.push_cmd
        def sh_stub(cmd, **k):
            if cmd[:3] == ["git", "add", "-A"]: return 128, "", 'error: open("b"): Permission denied'
            return real_sh(cmd, **k)
        janus.sh = sh_stub; janus.push_cmd = lambda repo, cfg=None: ["true"]
        try:
            with self.assertRaises(janus.Err): janus.cmd_commit_state(cfg, type("A", (), {"message": "state"})())
        finally: janus.sh, janus.push_cmd = real_sh, real_push


class Item12NewWorktree(unittest.TestCase):
    def _ns(self, unit, **kw):
        import argparse
        base = dict(unit=unit, kind="implement", tier="normal", host="here", model="m", brief=None, cwd=None, budget=None,
                    test_host=None, allow_main=True, of=None, report=None, resume_of=None, item=None,
                    probes={"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "100", "load": "0"}})
        base.update(kw); return argparse.Namespace(**base)

    def test_a_refused_launch_leaves_no_worktree_or_branch(self):
        """Catches (finding 12): --new-worktree created the tree and branch before the launch could still be refused (the brief
        lint), leaving them behind so the same command then failed with git's 'a branch named ... already exists'."""
        base, R, g = git_repo("u12a")
        d, cfg = launch_cfg('\n[quality]\nbrief_lint = true\n')
        b = d / "briefs/u1.md"; b.write_text("just some words with no markers\n")
        saved = janus.Beads, janus.herdr; janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None
        try:
            with self.assertRaises(janus.Err): janus.cmd_launch(cfg, self._ns("u1", brief=str(b), new_worktree=str(R)))
        finally: janus.Beads, janus.herdr = saved
        self.assertFalse((d / "worktrees/u1").exists(), "a refused launch left the worktree behind")
        self.assertNotEqual(g("rev-parse", "-q", "--verify", "u1").returncode, 0, "a refused launch left the branch behind")

    def test_cwd_with_new_worktree_is_an_error(self):
        """Catches (finding 12): --cwd given with --new-worktree was silently ignored."""
        base, R, g = git_repo("u12b")
        d, cfg = launch_cfg('\n[quality]\nbrief_lint = true\n')
        b = d / "briefs/u1.md"; b.write_text("just some words with no markers\n")
        saved = janus.Beads, janus.herdr; janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None
        try:
            with self.assertRaises(janus.Err) as e: janus.cmd_launch(cfg, self._ns("u1", brief=str(b), cwd=str(d), new_worktree=str(R)))
        finally: janus.Beads, janus.herdr = saved
        self.assertIn("--cwd", str(e.exception))

    def test_from_defaults_to_the_landing_branch(self):
        """Catches (finding 12): --from defaulted to main, not [landing] branch."""
        base, R, g = git_repo("u12c")
        g("checkout", "-q", "-b", "develop"); (R / "d").write_text("d\n"); g("add", "."); g("commit", "-qm", "develop"); g("checkout", "-q", "main")
        d, cfg = launch_cfg('\n[landing]\nbranch = "develop"\n[models.c]\nprovider = "api"\nkind = "claude"\n'); cfg.run.dry = True
        b = d / "briefs/u1.md"; b.write_text("TIER: NORMAL\nSCOPE: x\nREPORT.md\nDONE-WHEN: done\n")
        saved = janus.Beads, janus.herdr; janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                try: janus.cmd_launch(cfg, self._ns("u1", model="c", brief=str(b), new_worktree=str(R)))
                except janus.Err: pass
        finally: janus.Beads, janus.herdr = saved
        self.assertIn("worktree add -b u1", out.getvalue())
        self.assertIn("develop", out.getvalue(), "the new worktree did not start from [landing] branch")


class Item13ForceClose(unittest.TestCase):
    def test_force_close_approves_only_the_unit_being_closed(self):
        """Catches (finding 13): force_close_ok approved on any word that named a unit (a --reason value), not the unit being
        closed."""
        base, R, g = git_repo("h13b")
        W = base / "wt"; g("worktree", "add", "-q", str(W), "-b", "u1")
        d, cfg = project()
        u1 = {"id": "t-1", "name": "u1", "cwd": str(W)}
        u2 = {"id": "t-2", "name": "u2", "cwd": str(d)}   # the project root: shared, not its own tree
        real = janus.Beads; janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u1, u2]})
        try:
            self.assertTrue(janus.force_close_ok(cfg, ["close", "u1", "--force"]))
            self.assertFalse(janus.force_close_ok(cfg, ["close", "u2", "--force", "--reason", "handover", "from", "u1"]),
                             "a unit named elsewhere approved a close of another unit")
        finally: janus.Beads = real


class Item14UnitChanges(unittest.TestCase):
    def test_a_staged_merge_of_another_branch_does_not_use_the_landing_base(self):
        """Catches (finding 14): a staged merge of a branch other than the landing branch made unit_changes diff against the
        landing branch tip, so the landing branch's later file counted as the unit's change."""
        base, R, g = git_repo("u14")
        g("checkout", "-q", "-b", "z"); (R / "z").write_text("z\n"); g("add", "."); g("commit", "-qm", "z")
        g("checkout", "-q", "main"); (R / "c").write_text("2\n"); g("add", "."); g("commit", "-qm", "main adds c")
        g("checkout", "-q", "-b", "unit", "main~1"); (R / "u").write_text("u\n"); g("add", "."); g("commit", "-qm", "unit")
        g("merge", "--no-commit", "--no-ff", "z")
        d, cfg = project(); cfg.c["landing"] = {"branch": "main"}
        u = {"id": "t-1", "name": "u1", "host": "here", "cwd": str(R)}
        br, fs = janus.unit_changes(cfg, u, [u])
        self.assertIn("u", fs); self.assertNotIn("c", fs, "the landing branch's own file counted as the unit's change")


class Item6Adoption(unittest.TestCase):
    def setUp(self):
        import shutil
        if not shutil.which("pueued") or not shutil.which("pueue"): self.skipTest("pueue not installed")
        self.pd = Path(tempfile.mkdtemp(prefix="ad")); (self.pd / "d").mkdir(); (self.pd / "r").mkdir()
        (self.pd / "p.yml").write_text(f"shared:\n  pueue_directory: {self.pd}/d\n  runtime_directory: {self.pd}/r\n"
                                       f"  use_unix_socket: true\n  unix_socket_path: {self.pd}/s.sock\n")
        subprocess.run(["pueued", "-c", str(self.pd / "p.yml"), "-d"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        for _ in range(50):
            if (self.pd / "s.sock").exists(): break
            time.sleep(0.1)

    def tearDown(self):
        subprocess.run(["pueue", "-c", str(self.pd / "p.yml"), "shutdown"], capture_output=True, timeout=20)

    def _pq(self, *a): return subprocess.run(["pueue", "-c", str(self.pd / "p.yml"), *a], capture_output=True, text=True)
    def _labels(self): return {int(k): (v.get("label") or "") for k, v in json.loads(self._pq("status", "--json").stdout).get("tasks", {}).items()}

    def test_adoption_matches_the_path_and_verifies_the_edit(self):
        """Catches (finding 6): adopt relabelled another project's job by bare unit name, reported adoptions pueue edit never
        made, did not adopt a gate job of this project's unit, and left its editor script in the state dir."""
        d = Path(tempfile.mkdtemp(prefix="janus-adopt-"))
        (d / "janus.toml").write_text(f'[project]\nname = "t"\nid = "proj"\n[hosts.here]\nlocal = true\npueue_config = "{self.pd}/p.yml"\n')
        (d / "out").mkdir(); (d / "briefs").mkdir(); (d / "board/agents").mkdir(parents=True)
        self._pq("add", "-s", "-l", "unit:u1", "-w", str(d), "--", "true")
        self._pq("add", "-s", "-l", "unit:u1", "-w", str(self.pd), "--", "true")
        self._pq("add", "-s", "-l", "gate:land-u1-linux", "-w", str(d), "--", "true")
        ids = sorted(self._labels())
        saved = janus.all_units; janus.all_units = lambda cfg: {"u1": {}}
        try: out = "\n".join(janus.adopt_items(d, dry=False, config=d / "janus.toml"))
        finally: janus.all_units = saved
        labs = self._labels()
        self.assertEqual(labs[ids[0]], "unit:u1@proj", out)
        self.assertEqual(labs[ids[1]], "unit:u1", "another project's job was relabelled: " + out)
        self.assertEqual(labs[ids[2]], "gate:land-u1-linux@proj", "this project's gate job was not adopted: " + out)
        self.assertEqual(list((d / ".janus").glob(".pueue-label-*.py")), [], "the editor script was left behind")

    def test_the_unit_record_follows_a_renamed_tmux_session(self):
        """Catches (finding 6): the tmux session was renamed but the unit record kept no tmux field, so unit_tmux(u) still
        looked for the old name."""
        d = Path(tempfile.mkdtemp(prefix="janus-mig-")); (d / "janus.toml").write_text('[project]\nname = "t"\nid = "proj"\n')
        saved_units = []
        class B:
            def __init__(self, cfg): pass
            def units(self): return [{"id": "t-1", "name": "u1"}]
            def save(self, u): saved_units.append(dict(u))
        def fake_sh(cmd, **k):
            if cmd[:2] == ["pueue", "status"]: return 0, json.dumps({"tasks": {}}), ""
            if cmd[:2] == ["tmux", "list-sessions"]: return 0, "orch-u1\n", ""
            return 0, "", ""
        real_sh, real_beads, real_units = janus.sh, janus.Beads, janus.all_units
        janus.sh = fake_sh; janus.Beads = B; janus.all_units = lambda cfg: {"u1": {}}
        try: janus.adopt_items(d, dry=False, config=d / "janus.toml")
        finally: janus.sh, janus.Beads, janus.all_units = real_sh, real_beads, real_units
        self.assertEqual([u.get("tmux") for u in saved_units], ["janus-u1@proj"])


if __name__ == "__main__": unittest.main()
