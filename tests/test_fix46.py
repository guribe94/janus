"""Soak bugs of the evening run (ISSUES rows 214-225): one test per bug, each naming the failure it catches. Standard library
only; no network, no spend.

Run by ./check (it discovers tests/test_*.py)."""
import argparse, json, os, signal, subprocess, sys, time, unittest
from pathlib import Path
from unittest import mock

from test_janus import JANUS_PY, janus, project

iso = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - ago))
GROUPS = {"jobs": {"status": "Running", "parallel_tasks": 4}, "gates": {"status": "Running", "parallel_tasks": 4}}


def job(tid, jid, enq, start=None, lane="jobs", label="unit:u@t"):
    st = {"Running": {"enqueued_at": iso(enq), "start": iso(start)}} if start is not None else {"Queued": {"enqueued_at": iso(enq)}}
    return {"id": tid, "group": lane, "label": label, "command": f"bash /r/runs/{jid}.sh", "status": st}


def ages(cfg, hs): return sorted(k for k, _ in janus.lane_findings(cfg, hs, []) if k.startswith("age:"))


class FakeBeads:
    units_ = []
    def __init__(self, cfg): pass
    def units(self): return self.units_
    def save(self, u): pass
    def comment(self, i, t): pass


class Bug1AgeByProgress(unittest.TestCase):
    def test_a_running_job_is_stuck_only_when_it_is_over_its_age_from_start_and_shows_no_progress(self):
        """Catches (row 214): a job that waited long in the queue, or runs long while its output still grows, was called over its
        age (counted from enqueue) and the autopilot cancelled it mid-run."""
        d, cfg = project()
        tasks = {"1": job(1, "j1", 5 * 3600, start=600),       # queued 5 h ago, running for 10 min: young
                 "2": job(2, "j2", 5 * 3600, start=4 * 3600),  # running 4 h, output grew 20 s ago: progressing
                 "3": job(3, "j3", 5 * 3600, start=4 * 3600),  # running 4 h, quiet for 2 h: stuck
                 "4": job(4, "j4", 5 * 3600)}                  # still queued after 5 h: over the wait limit
        hs = {"here": {"ok": True, "live": ["j1 5", "j2 20", "j3 7200"], "pueue": {"groups": GROUPS, "tasks": tasks}}}
        self.assertEqual(ages(cfg, hs), ["age:here:3", "age:here:4"])
        (cfg.f("runs") / "wrap").mkdir(parents=True); (cfg.f("runs") / "wrap" / "j3").write_text("here:2 j2\n")
        self.assertEqual(ages(cfg, hs), ["age:here:4"], "a wrapper waiting on its own running job was called stuck")

    def test_the_autopilot_requeues_a_stuck_running_job_and_leaves_a_waiting_one(self):
        """Catches (row 214): the autopilot cancelling and re-queuing a job that only waits in the queue (it loses its place and
        gains nothing), where only a running job without progress is stuck."""
        d, cfg = project("\n[autopilot]\nenabled = true\n")
        tasks = {"3": job(3, "j3", 5 * 3600, start=4 * 3600), "4": job(4, "j4", 5 * 3600)}
        hs = {"here": {"ok": True, "live": ["j3 7200"], "pueue": {"groups": GROUPS, "tasks": tasks}}}
        jd = cfg.f("runs") / "jobs"; jd.mkdir(parents=True)
        for jid, tid in (("j3", 3), ("j4", 4)): (jd / f"{jid}.json").write_text(json.dumps({"host": "here", "task": tid, "wt": str(d), "cmd": "make"}))
        F = [janus.finding(k, t) for k, t in janus.lane_findings(cfg, hs, [])]; acted = []
        with mock.patch.object(janus, "cancel_job", lambda cfg, ref, force=False: acted.append(ref) or ["cancelled"]), \
             mock.patch.object(janus, "requeue", lambda cfg, spec, to: "here:9"), mock.patch.object(janus, "board", lambda *a, **k: (0, "", "")):
            janus.requeue_stuck(cfg, hs, [], F, lambda m: None)
        self.assertEqual(acted, ["here:3"])


class Bug2RequeueMark(unittest.TestCase):
    def test_a_requeued_job_s_record_says_so(self):
        """Catches (row 215): the copy the autopilot queued carried no `requeued` mark (requeue dropped it), so a job was
        re-queued again and again (three times in the soak) though the rule says once."""
        d, cfg = project()
        with mock.patch.object(janus, "hrun", lambda *a, **k: (0, "7", "")):
            janus.requeue(cfg, {"host": "here", "task": "5", "lane": "jobs", "wt": str(d), "cmd": "true", "pinned": True, "requeued": "here:5"}, "here")
        specs = [json.loads(f.read_text()) for f in (cfg.f("runs") / "jobs").glob("*.json")]
        self.assertEqual([s.get("requeued") for s in specs], ["here:5"])


class Bug3GateStepsUnit(unittest.TestCase):
    def gate(self, cfg, d, name, **k):
        (d / "out" / name).mkdir(parents=True, exist_ok=True); (d / "out" / name / "tests.sh").write_text("#!/bin/sh\nexit 0\n")
        a = argparse.Namespace(name=name, baseline=None, host=None, timeout_min=None, quick=False, unit=None, allow_unmerged=False, priority=10, wait=False)
        for x, v in k.items(): setattr(a, x, v)
        return a

    def test_the_gate_s_task_carries_its_unit_for_the_steps_it_queues(self):
        """Catches (row 216): a gate's steps (`janus run` inside tests.sh) recorded `unit: null`, so a hold on the unit did not
        protect them and the autopilot's notice about them went to nobody."""
        d, cfg = project(); FakeBeads.units_ = [{"id": "b1", "name": "pico", "cwd": str(d)}]; said = []
        def hrun(h, s, **k): said.append(s); return (0, '{"tasks": {}}' if "status --json" in s else "12", "")
        with mock.patch.object(janus, "Beads", FakeBeads), mock.patch.object(janus, "hrun", hrun):
            janus.cmd_gate(cfg, self.gate(cfg, d, "land-pico-x", unit="pico"))
            add = next(s for s in said if "--print-task-id" in s); self.assertIn("JANUS_UNIT=pico", add); said.clear()
            janus.cmd_gate(cfg, self.gate(cfg, d, "land-pico-y"))   # no --unit: the open unit its name carries
            self.assertIn("JANUS_UNIT=pico", next(s for s in said if "--print-task-id" in s))

    def test_a_step_of_a_live_gate_is_not_an_orphan_of_its_unit(self):
        """Catches (row 216): with the unit on the steps, a gate the orchestrator runs for a unit whose agent has ended would
        have its steps cancelled as orphans while the gate itself is running."""
        d, cfg = project()
        tasks = {"1": {"id": 1, "group": "gates", "label": "gate:g@t", "command": "env JANUS_WRAP=gate-g-1 x", "status": "Running"},
                 "2": {**job(2, "j2", 60, start=30), "label": "unit:gone@t"}, "3": {**job(3, "j3", 60, start=30), "label": "unit:gone@t"}}
        (cfg.f("runs") / "wrap").mkdir(parents=True); (cfg.f("runs") / "wrap" / "gate-g-1").write_text("here:2 j2\n")
        hs = {"here": {"ok": True, "pueue": {"tasks": tasks}}}
        self.assertEqual([r for r, _ in janus.orphan_jobs(cfg, hs, [])], ["here:3"])


class Bug4DuplicateGate(unittest.TestCase):
    def test_a_gate_already_queued_or_running_is_refused_and_task_txt_keeps_the_live_task(self):
        """Catches (row 217): a second `janus gate <name>` was queued beside the running one, failed at once, and out/<name>/task.txt
        then named the dead duplicate, so `janus gate-log` read the wrong task."""
        d, cfg = project(); a = Bug3GateStepsUnit().gate(cfg, d, "g"); (d / "out/g/task.txt").write_text("here:4\n"); added = []
        live = json.dumps({"tasks": {"4": {"id": 4, "label": "gate:g@t", "status": {"Running": {}}}, "3": {"id": 3, "label": "gate:g@t", "status": {"Done": {"result": "Success"}}}}})
        def hrun(h, s, **k): added.append(s) if "--print-task-id" in s else None; return (0, live if "status --json" in s else "9", "")
        with mock.patch.object(janus, "hrun", hrun), self.assertRaises(janus.Err) as e: janus.cmd_gate(cfg, a)
        self.assertIn("here:4", str(e.exception)); self.assertEqual(added, [])
        self.assertEqual((d / "out/g/task.txt").read_text(), "here:4\n")


class Bug5QueueLogOneHost(unittest.TestCase):
    def test_queue_log_host_id_prints_that_host_s_task_only(self):
        """Catches (row 218): `janus queue --log <id>` printed task <id>'s log from every host (ids collide between hosts), and
        `<host>:<id>` was refused."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\n'); asked = []
        def hrun(h, s, **k): asked.append(h.get("ssh", "local")); return (0, "out of " + h.get("ssh", "local"), "")
        with mock.patch.object(janus, "hrun", hrun), mock.patch.object(janus, "task_status", lambda h, tid: {"command": "x"}):
            rc, L = janus.cmd_queue(cfg, argparse.Namespace(host=None, log="far:7"))
        self.assertEqual(set(asked), {"far"}); self.assertEqual(L, ["== far", "out of far"])


class Bug10RunLogOfAGoneTask(unittest.TestCase):
    def test_a_cancelled_job_s_result_and_log_are_its_run_log(self):
        """Catches (row 223): a job that was cancelled and removed (the autopilot's re-queue) was read as "a task without a job
        script", so its agent got an empty task-<host>-<id>.log although the run log held all its output."""
        d, cfg = project(); rd = Path(cfg.hosts["here"]["_rd"]); (rd / "jobs").mkdir(parents=True)
        (rd / "jobs" / "j7.json").write_text(json.dumps({"host": "here", "task": "7", "at": 1})); (rd / "j7.log").write_text("58% passed\n")
        hs = {"here": {"ok": True, "pueue": {"tasks": {}}}}
        with mock.patch.object(janus, "hrun", lambda *a, **k: (1, "", "no such task")):
            rc, path = janus.job_result(cfg, hs, "here:7", "here")
            self.assertEqual(Path(path).read_text(), "58% passed\n")
            self.assertIn("58% passed", "\n".join(janus.cmd_queue(cfg, argparse.Namespace(host=None, log="here:7"))[1]))


class Bug6CancelStopsTheGatesGroup(unittest.TestCase):
    def test_a_signalled_gate_wrapper_stops_its_command_s_group_and_writes_no_result(self):
        """Catches (row 219): cancelling a gate killed its wrapper only; the gate's runner and tests.sh (their own process group)
        kept running, queued later steps and wrote a RESULT line after the cancel."""
        d, cfg = project(); late = d / "late"; rd = Path(cfg.hosts["here"]["_rd"])
        p = subprocess.Popen([sys.executable, str(JANUS_PY), "gate-exec", "g", "--", f"(sleep 2; echo RESULT > {late}) & wait"],
                             cwd=d, env={**os.environ, "JANUS_ROOT": str(d), "JANUS_WRAP": "gate-g-1"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while not (rd / "gate-g-1.pgid").is_file() and time.time() - t0 < 10: time.sleep(0.1)
        self.assertTrue((rd / "gate-g-1.pgid").is_file(), "no group id for janus cancel to stop")
        p.send_signal(signal.SIGTERM); self.assertEqual(p.wait(timeout=30), 128 + signal.SIGTERM); time.sleep(3)
        self.assertFalse(late.exists(), "the gate's command outlived the cancel and wrote its result")
        self.assertEqual((d / "out/gates.log").exists(), False)


class Bug7LandingGateHoldsTheHead(unittest.TestCase):
    def test_a_landing_gate_refuses_a_tree_one_commit_behind_the_landing_branch(self):
        """Catches (row 220): a landing gate ran on a branch that lacked the landing branch's newest commit (only "more than
        max_behind" was refused), so its PASS said nothing about the tree that would land."""
        d, cfg = project(); wt = d / "wt"; g = lambda *a, c=d: subprocess.run(["git", "-C", str(c), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
        g("init", "-q", "-b", "main"); (d / "f").write_text("1"); g("add", "f"); g("commit", "-qm", "1")
        g("worktree", "add", "-q", "-b", "u", str(wt)); (d / "f").write_text("2"); g("commit", "-qam", "2")
        self.assertIsNone(janus.tree_guard(cfg, wt), "an ordinary gate is refused only far behind")
        self.assertIn("does not contain the head of main", janus.tree_guard(cfg, wt, head=True) or "")
        FakeBeads.units_ = [{"id": "b1", "name": "u", "cwd": str(wt)}]; a = Bug3GateStepsUnit().gate(cfg, d, "land-u")
        hrun = lambda h, s, **k: (0, '{"tasks": {}}' if "status --json" in s else "12", "")
        with mock.patch.object(janus, "Beads", FakeBeads), mock.patch.object(janus, "hrun", hrun):
            with self.assertRaises(janus.Err) as e: janus.cmd_gate(cfg, a)
            self.assertIn("merge main first", str(e.exception))
            a.allow_unmerged = True; janus.cmd_gate(cfg, a)


def doctor_lines(cfg):
    """janus doctor's lines for a project, with the machine's tools, hooks and watcher stubbed."""
    import contextlib, io, tempfile
    out = io.StringIO()
    with mock.patch.dict(os.environ, {"HOME": tempfile.mkdtemp(prefix="janus-home-")}), mock.patch.object(janus, "sh", lambda *a, **k: (0, "", "")), \
         mock.patch.object(janus, "watcher_loaded", lambda *a: True), contextlib.redirect_stdout(out):
        janus.doctor(cfg, type("A", (), {"no_hosts": True})())
    return out.getvalue().splitlines()


class Bug11IssueRegisterDefault(unittest.TestCase):
    def test_an_issue_is_written_to_the_state_folder_when_no_register_is_configured(self):
        """Catches (row 224): `janus issue` refused in a project with no `[janus] issues_file`, so what an agent met there was
        never written down; and doctor not naming where the rows go."""
        d, cfg = project()
        self.assertNotIn("issues_file", cfg.c.get("janus", {}))
        rc, L = janus.cmd_issue(cfg, type("A", (), {"text": "a gate ran on an old tree", "evidence": "here:7", "key": None})())
        reg = cfg.f("ISSUES.md")
        self.assertEqual(rc, 0, L); self.assertTrue(reg.is_file(), L); self.assertIn("| 1 | a gate ran on an old tree | here:7 |", reg.read_text())
        self.assertIn(str(reg), L[0])
        line = [l for l in doctor_lines(cfg) if "issue register" in l]
        self.assertTrue(line and str(reg) in line[0] and "default" in line[0], line)
        cfg.c.setdefault("janus", {})["issues_file"] = "docs/I.md"   # a configured register still wins
        self.assertEqual(janus.issue_path(cfg), cfg.root / "docs/I.md")


class Bug12AutopilotOffUnlessSet(unittest.TestCase):
    def test_no_autopilot_section_means_the_unattended_loop_is_off_and_doctor_and_check_say_so(self):
        """Catches (row 225): a project with a spending cap and no `[autopilot]` section had the watcher relaunch its units and
        launch its backlog unasked (on by default with a cap); and nothing saying whether the loop is on."""
        d, cfg = project("\n[backlog]\nfile = \"S.md\"\n")
        self.assertTrue(janus.has_cap(cfg)); self.assertNotIn("autopilot", cfg.c)
        self.assertFalse(janus.apc(cfg, "enabled"), "the autopilot is on with no [autopilot] section")
        self.assertFalse(janus.fill_watcher(cfg), "the watcher fills from the backlog with no [autopilot] section")
        F = dict(janus.unattended_findings(cfg, [{"id": "t-1", "name": "u1"}]))
        self.assertIn("unattended:off", F); self.assertIn("[autopilot] enabled = true", F["unattended:off"]); self.assertNotIn("cap_usd", F["unattended:off"])
        line = [l for l in doctor_lines(cfg) if "unattended loop" in l]
        self.assertTrue(line and " off" in line[0] and "[autopilot] enabled" in line[0], line)
        cfg.c["autopilot"] = {"enabled": True}   # the owner's setting turns both on
        self.assertTrue(janus.apc(cfg, "enabled")); self.assertTrue(janus.fill_watcher(cfg))
        self.assertEqual(janus.unattended_findings(cfg, [{"id": "t-1", "name": "u1"}]), [])
        self.assertTrue(any("unattended loop" in l and " on" in l for l in doctor_lines(cfg)))
        cfg.c["fill"] = {"watcher": False}; self.assertFalse(janus.fill_watcher(cfg))


class Bug9ReconciledRunsPayTheirRealCost(unittest.TestCase):
    def test_recost_reads_what_an_agent_gone_run_left_and_keeps_the_bound_only_when_nothing_exists(self):
        """Catches (row 222): a run whose agent was gone stayed booked at its whole budget (`done unknown`) although its json
        output reported what it cost: $3.44 held for a run that cost $0.04, under a cap that then refused work."""
        import contextlib, io
        d, cfg = project(); pv = cfg.prov["api"]; led = d / pv["ledger"]
        at = lambda ago: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - ago))
        rows = []
        def run(unit, rid, budget, cost, how): rows.extend([(at(3600), unit, "running", budget, rid), (at(60), unit, cost, how, rid)])
        def left(unit, name, text, age=0):
            f = d / "out" / unit / name; f.parent.mkdir(exist_ok=True); f.write_text(text); os.utime(f, (time.time() - age,) * 2)
        run("a", "a.7", 5.0, 5.0, "done unknown"); left("a", "api.json", json.dumps({"total_cost_usd": 4.2, "session_id": "A"}))   # a closed unit's only run
        run("b", "b.1.100", 2.0, 0.13, "done rc=resumed"); left("b", "api-b.1.100.json", json.dumps({"total_cost_usd": 0.13, "session_id": "S"}))
        run("b", "b.r2.200", 3.44, 3.44, "done unknown"); left("b", "api-b.r2.200.json", json.dumps({"total_cost_usd": 0.1743, "session_id": "S"}))   # a resumed run: its own part
        run("c", "c.9", 5.0, 5.0, "done unknown"); left("c", "api.json", "")                                           # it left nothing: the bound stays
        run("o", "o.3", 2.0, 2.0, "done unknown"); left("o", "api.json", json.dumps({"total_cost_usd": 0.5}), age=7200)   # a json older than the run: not its own
        run("e", "e.1.100", 1.0, 1.0, "done unknown"); run("e", "e.r2.200", 1.0, 0.2, "done rc=0"); left("e", "api.json", json.dumps({"total_cost_usd": 0.5}))   # the later run's json
        led.write_text("".join("\t".join(map(str, r)) + "\n" for r in rows)); before = led.read_text()
        def recost(dry):
            out = io.StringIO(); cfg.run.dry = dry
            try:
                with contextlib.redirect_stdout(out), mock.patch.object(janus, "refuse_agent", lambda *a, **k: None):
                    janus.cmd_ledger(cfg, argparse.Namespace(action="recost", provider=None))
            finally: cfg.run.dry = False
            return out.getvalue()
        out = recost(dry=True)
        for want in ("a.7", "$5.00", "$4.20", "b.r2.200", "$3.44", "$0.04"): self.assertIn(want, out, "the dry run does not show the change")
        for keep in ("c.9", "o.3", "e.1.100"): self.assertNotIn(keep, out, "a run that left no cost of its own was changed")
        self.assertEqual(led.read_text(), before, "the dry run changed the ledger")
        recost(dry=False)
        runs = janus.ledger_read(cfg, pv)[0]
        self.assertEqual([round(runs[k]["cost"], 4) for k in ("a.7", "b.r2.200", "c.9", "o.3", "e.1.100")], [4.2, 0.0443, 5.0, 2.0, 1.0])
        self.assertEqual([runs[k]["how"] for k in ("a.7", "c.9")], ["done rc=reconciled", "done unknown"], "a recovered run is still shown as counted at its budget")
        self.assertIn("nothing to correct", recost(dry=False))


class Bug8RoomUsesTheWaitingJobsVerdict(unittest.TestCase):
    def test_a_pause_a_waiting_job_just_met_is_the_report_s_verdict_too(self):
        """Catches (row 221): a job held by admission for memory pressure while the check's own reading, taken at another instant,
        saw none and reported free room on that host (launch more there)."""
        d, cfg = project("\n[capacity]\nmin_disk_gb = 0\n")
        h = cfg.hosts["here"]; janus.write_cap(cfg, "here", h); rd = Path(h["_rd"]); rd.mkdir(parents=True, exist_ok=True)
        lib = (rd / "cap.sh").read_text()
        (rd / "cap.sh").write_text(lib + "\ncap_measure() { PSI=50; LOAD=0; DF=50; }\n")   # what the waiting job reads: memory pressure
        r = argparse.Namespace(host="here", lane="jobs", unit=None, wt=str(d), subdir=None, usb=False, priority=0, no_wait=True, allow_unmerged=False, command=["--", "true"], broad=None)
        p = subprocess.run(["bash"], input=janus.job_script(cfg, "here", h, r, "u", "true", "j-8"), capture_output=True, text=True, timeout=60,
                           env={**os.environ, "JANUS_POLL_S": "1", "JANUS_ADMIT_MAX_S": "2", "JANUS_ADMIT_REPORT_S": "1"})
        self.assertEqual(p.returncode, 121, p.stdout[-400:]); self.assertIn("PAUSED: memory PSI", p.stdout)
        def report():   # the check's reading a moment later: no pressure at that instant
            o = subprocess.run(["bash", "-c", f"RD={rd}; CAP_RO=1; . $RD/cap.sh\ncap_measure() {{ PSI=0; LOAD=0; DF=50; }}\ncap_report"], capture_output=True, text=True).stdout
            return dict(l.split("=", 1) for l in o.splitlines() if l.startswith(("cap_pause=", "cap_admit=")))
        got = report()
        self.assertIn("memory PSI", got["cap_pause"], "the report says the host is free while a job there was just held for memory pressure")
        self.assertIn("paused", got["cap_admit"])
        probe = {"ok": True, "cap_pause": got["cap_pause"], "cap_rj": "0", "cap_rc": "0", "cap_rm": "0", "cap_ra": "0", "cap_ram": "0", "pueue": {"tasks": {}}}
        self.assertEqual(janus.room(cfg, "here", probe, [])[0], 0, "room on a host whose admission is paused")
        (rd / "paused").write_text(f"{int(time.time()) - 600} memory PSI avg10 50 over 20\n")   # an old verdict is not kept
        self.assertEqual(report(), {"cap_pause": "", "cap_admit": "yes"})


if __name__ == "__main__":
    unittest.main()
