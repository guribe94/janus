"""imp-autopilot: notes stale only on real events, the context reset at a safe boundary, the autopilot's fixed-rule actions.
Standard library only; no network, no API spend."""
import json, os, subprocess, time, unittest
from unittest import mock

from test_janus import janus, project


class NoBeads:
    def __init__(self, *a, **k): raise janus.Err("no beads in this test")


class Staleness(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project()
        janus.handoff_path(self.cfg).write_text(f"# H\n{janus.LB}\nlive\n{janus.LE}\n{janus.NOTES_H}\nnotes v1\n")
        janus.notes_state(self.cfg); s = self.cfg.jget("notes.json"); s["ts"] -= 3 * self.cfg.cycle; self.cfg.jput("notes.json", s)
        self.cfg.jput("running.json", {"n": 2})

    def ack(self, k, **kw):
        with mock.patch.object(janus, "Beads", NoBeads): janus.cmd_ack(self.cfg, mock.Mock(problem=k, decision="why", decision_class=kw.get("dc", False)))

    def test_notes_stale_only_after_a_real_event(self):
        """Catches: the Stop hook re-firing after every routine ack and by the clock while units run (fixes10 item 8)."""
        self.assertEqual(janus.handoff_stale(self.cfg, True), [], "old notes went stale by the clock alone")
        self.ack("margin:here:disk"); self.ack("idle:here:jobs")
        self.assertEqual(janus.handoff_stale(self.cfg, True), [], "a routine ack made the notes stale")
        self.ack("unit:u1:finished")
        st = janus.handoff_stale(self.cfg, True)
        self.assertTrue(st and "after a decision" in st[0], st)
        janus.handoff_path(self.cfg).write_text(janus.handoff_path(self.cfg).read_text() + "decided x\n")
        self.assertEqual(janus.handoff_stale(self.cfg), [], "updating the notes did not clear the staleness")
        time.sleep(0.01); janus.note_event(self.cfg, "launch")
        self.assertIn("after a launch", janus.handoff_stale(self.cfg)[0])


class ContextReset(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project()
        tr = self.d / "t.jsonl"; self.cfg.f("transcript").write_text(str(tr))
        tr.write_text(json.dumps({"message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": self.cfg.reset_tokens + 5000}}}) + "\n")

    def log(self): return self.cfg.f("watch.log").read_text() if self.cfg.f("watch.log").exists() else ""

    def test_reset_only_at_a_safe_boundary_and_check_flags_a_missed_one(self):
        """Catches: a context over the threshold left to the runtime's lossy auto-compaction, a reset in the middle of
        unrecorded work, and a skipped reset with no reason given."""
        self.assertEqual([k for k, _ in janus.context_findings(self.cfg)], ["context"])
        self.assertIn("not a safe boundary", janus.stop_reset(self.cfg, "notes stale"))
        self.assertIn("skipped at Stop", self.log()); self.assertFalse(self.cfg.f("reset_asked").exists())
        self.assertEqual(janus.stop_reset(self.cfg), "asked"); self.assertIn("context reset at Stop", self.log())
        self.assertEqual(janus.context_findings(self.cfg), [], "check still flags the context after the reset was asked")
        self.assertIn("under 10 min", janus.stop_reset(self.cfg))
        self.cfg.f("transcript").write_text(str(self.d / "small.jsonl")); (self.d / "small.jsonl").write_text("{}\n")
        self.assertEqual(janus.stop_reset(self.cfg), "under the threshold")


class ContextTokens(unittest.TestCase):
    """context_tokens on synthetic Claude and Codex transcripts (the Codex shapes were read from a recorded rollout's keys and numbers)."""
    def setUp(self): self.d, self.cfg = project(); self.tr = self.d / "t.jsonl"

    def put(self, *recs, raw=""):
        self.tr.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in recs) + raw); return janus.context_tokens(self.tr)

    @staticmethod
    def tc(inp, cached=0, total=10**9, info=True):
        u = lambda i, c: {"input_tokens": i, "cached_input_tokens": c, "output_tokens": 5, "total_tokens": i + 5}
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": u(total, total), "last_token_usage": u(inp, cached)} if info else None}}

    @staticmethod
    def rec(inp, cached=0):
        u = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": 5}
        return {"type": "token_usage_record", "payload": {"usage": u, "turn_token_usage": {"input_tokens": 10**8}, "thread_token_usage": {"input_tokens": 10**9}}}

    def test_codex_token_count_is_the_latest_request_not_the_file_size(self):
        """Catches: a Codex rollout falling through to file bytes // 4 (a huge, wrong value that triggers resets)."""
        filler = {"type": "response_item", "payload": {"type": "message", "text": "x" * 4000}}
        self.assertEqual(self.put(filler, filler, self.tc(181673, 180224), filler), 181673)
        self.assertLess(self.put(filler, self.tc(100)), 1000)

    def test_codex_newest_record_wins_even_when_smaller_after_compaction(self):
        """Catches: a max or a sum over the history keeping the context above the threshold after a compaction."""
        self.assertEqual(self.put(self.tc(200000), self.tc(240000), {"type": "compacted", "payload": {}}, self.tc(30000)), 30000)

    def test_codex_cached_inputs_are_a_subset_and_cumulative_totals_are_ignored(self):
        """Catches: cached_input_tokens added to input_tokens, and total_token_usage (the whole session) read as the context."""
        self.assertEqual(self.put(self.tc(184613, 181504, total=99_000_000)), 184613)
        self.assertEqual(self.put(self.rec(184613, 181504)), 184613)

    def test_token_usage_record_and_token_count_newest_of_either(self):
        self.assertEqual(self.put(self.tc(100), self.rec(300)), 300)
        self.assertEqual(self.put(self.rec(300), self.tc(100)), 100)

    def test_codex_null_or_malformed_trailing_usage_falls_back_to_the_previous_valid_one(self):
        """Catches: a crash (the Stop hook dying) or a 0 on a half-written last record."""
        bad = [self.tc(0, info=False), {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": None}}},
               {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": "lots"}}}},
               {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": [1]}}}},
               {"type": "event_msg", "payload": {"type": "token_count", "info": "x"}}, {"type": "token_usage_record", "payload": {"usage": None}},
               {"type": "token_usage_record", "payload": {"usage": {"input_tokens": -5}}}, {"type": "token_usage_record", "payload": []},
               {"type": "token_usage_record"}, [1, 2], None, 7, "usage", '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_']
        self.assertEqual(self.put(self.tc(777), *bad), 777)
        self.assertEqual(self.put(self.tc(777), raw='{"type": "token_usage_record", "payload": {"usage": {"input_tok'), 777)

    def test_claude_semantics_preserved(self):
        """Catches: the Claude sum (input + cache creation + cache read, newest assistant turn) changing."""
        c = lambda **u: {"type": "assistant", "message": {"usage": u}}
        self.assertEqual(self.put(c(input_tokens=1, cache_read_input_tokens=1000), c(input_tokens=3, cache_creation_input_tokens=20, cache_read_input_tokens=500, output_tokens=99)), 523)
        self.assertEqual(self.put(c(input_tokens=50), c(input_tokens=None, cache_read_input_tokens="bad"), {"message": None}, {"message": {"usage": None}}), 50)

    def test_no_usable_usage_falls_back_to_size_and_a_missing_file_is_zero(self):
        self.assertEqual(self.put({"type": "x"}, {"message": {"usage": {}}}, self.tc(0)), self.tr.stat().st_size // 4)
        self.assertEqual(self.put(), 0)
        self.assertEqual(janus.context_tokens(self.d / "absent.jsonl"), 0)

    def test_truncated_first_line_of_the_tail_read_is_skipped(self):
        """Catches: a crash on the partial first line of the 1 MB tail; the newest usage is still found, and a lone partial line falls back."""
        big = {"type": "response_item", "payload": {"text": "y" * 1_200_000}}
        self.assertEqual(self.put(big, self.tc(4242)), 4242)
        self.assertEqual(self.put(big), self.tr.stat().st_size // 4)
        self.tr.write_text(("z" * 1_100_000) + '"usage": {"input_tokens": 9}}\n'); self.assertEqual(janus.context_tokens(self.tr), self.tr.stat().st_size // 4)


class Enforce(unittest.TestCase):
    def test_agent_direct_test_warned_once_then_stopped(self):
        """Catches: an agent running a test by hand on this machine outside the lanes left running after its warning, and a
        repeat offender warned again instead of stopped."""
        d, cfg = project(); posts, stops = [], []
        ps = lambda pid: (" 50     1    50   1.0    09:00 /usr/local/bin/claude --print\n"
                          f"{pid}    50   {pid}  90.0    01:00 cargo test -p x\n")
        def run(pid):
            viol = []; F = janus.proc_findings(cfg, ps(pid), viol=viol); return janus.enforce(cfg, F, viol)
        with mock.patch.object(janus, "env_janus", lambda p: {"JANUS_UNIT": "u1", "JANUS_PROJECT": "t"}), mock.patch.object(janus, "board", lambda c, *a, **k: posts.append(a) or (0, "", "")), \
             mock.patch.object(janus, "hrun", lambda h, script, **k: stops.append(script) or (0, "stopped", "")):
            F = run(60)
            self.assertTrue(any(k == "localtest:60" and "warned u1" in t for k, t in F), F)
            self.assertEqual((len(posts), stops), (1, []))
            run(60); self.assertEqual(stops, [], "stopped before [enforce] grace_min")
            e = cfg.jget("enforce.json"); e["localtest:60"]["warned"] -= 301; cfg.jput("enforce.json", e)
            F = run(60); self.assertEqual(len(stops), 1); self.assertIn("60", stops[0]); self.assertTrue(any("STOPPED" in t for _, t in F))
            run(61); self.assertEqual((len(posts), len(stops)), (1, 2), "a warned unit's next direct test got a second warning, not a stop")


    def test_cpu_burners_acted_on_only_under_this_projects_agents(self):
        """Catches (re-check N3): this project's watcher stopping a long build under another project's agent, or one it cannot
        tell is its own (an orphan without a project id)."""
        d, cfg = project()
        ps = (" 50     1    50   1.0    09:00 /usr/local/bin/claude --print\n 60    50    60  99.0    20:00 /opt/build --all\n"
              " 70     1    70   1.0    09:00 /usr/local/bin/claude --print\n 80    70    80  99.0    20:00 /opt/build --all\n"
              " 90     1    90  99.0    20:00 /opt/miner --all\n")
        env = {50: {"JANUS_UNIT": "x", "JANUS_PROJECT": "other"}, 60: {"JANUS_UNIT": "x", "JANUS_PROJECT": "other"},
               70: {"JANUS_UNIT": "u1", "JANUS_PROJECT": "t"}, 80: {"JANUS_UNIT": "u1", "JANUS_PROJECT": "t"}}
        viol = []
        with mock.patch.object(janus, "env_janus", lambda p: env.get(p, {})):
            F = janus.proc_findings(cfg, ps, viol=viol)
        self.assertEqual(sorted(k for k, _ in F if k.startswith("burner:")), ["burner:80", "burner:90"], "another project's burner shown")
        self.assertEqual([v["key"] for v in viol], ["burner:80"], "a burner not under this project's agents would be stopped")


class FakeBeads:
    units_ = []
    def __init__(self, cfg): pass
    def units(self): return self.units_
    def save(self, u): pass
    def comment(self, i, t): pass


class Autopilot(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project("\n[autopilot]\nenabled = true\nrelaunch_max = 1\n")
        self.launched = []; self.posts = []
        def launch(cfg, a): self.launched.append(a); FakeBeads.units_.append({"name": a.unit}); print(f"launched {a.unit}")
        self.p = [mock.patch.object(janus, "Beads", FakeBeads), mock.patch.object(janus, "cmd_launch", launch),
                  mock.patch.object(janus, "board", lambda c, *a, **k: self.posts.append(a) or (0, "", "")),
                  mock.patch.object(janus, "board_questions", lambda c: []),
                  mock.patch.object(janus, "probe", lambda *a, **k: {"name": "here", "ok": True, "pueue": {"tasks": {}}})]   # the relaunch asks the unit's host now: its task is gone
        for x in self.p: x.start()
    def tearDown(self):
        for x in self.p: x.stop()
        FakeBeads.units_ = []

    def unit(self, **k): return {"name": "u1", "id": "b-1", "host": "here", "state": "done", "via": "pueue", "model": "m", "brief": "b.md", "cwd": str(self.d), "run": "u1.1", **k}

    def test_ending_policy_relaunches_from_the_report_bounded_and_recorded(self):
        """Catches: a crashed or capped unit left dead with no action, relaunched twice for one cause, relaunched past
        relaunch_max, relaunched while the orchestrator holds it, and an action with no record (log + ack with reason)."""
        log = []; u = self.unit()
        janus.tend_autopilot(self.cfg, {}, [u], [], log.append)
        self.assertEqual([a.unit for a in self.launched], ["u1-r1"])
        a = self.launched[0]; self.assertEqual((a.report, a.seed_report, a.share_with, a.brief), (None, str(janus.report_path(self.cfg, u)), "u1", "b.md"))
        self.assertIn("u1-r1", self.cfg.jget("acks.json")["unit:u1:finished"]["d"]); self.assertIn("relaunch:crashed", self.cfg.f("autopilot.log").read_text())
        janus.tend_autopilot(self.cfg, {}, [u], [], log.append); self.assertEqual(len(self.launched), 1, "relaunched twice for one cause")
        r1 = self.unit(name="u1-r1", relaunch_of="u1", relaunches=1, wrapup="u1-r1.r2", run="u1-r1.r2", verdict="VERDICT: BLOCKED")
        self.assertEqual(janus.ending(self.cfg, r1), "capped")
        janus.tend_autopilot(self.cfg, {}, [r1], [], log.append)
        self.assertEqual(len(self.launched), 1, "relaunched past [autopilot] relaunch_max"); self.assertIn("relaunch-limit", self.cfg.f("autopilot.log").read_text())
        u2 = self.unit(name="u2"); self.cfg.jput("acks.json", {"unit:u2:finished": {"t": time.time(), "d": "hold: owner looks first"}})
        janus.tend_autopilot(self.cfg, {}, [u2], [], log.append); self.assertEqual(len(self.launched), 1, "acted on a held unit")
        self.assertIsNone(janus.ending(self.cfg, self.unit(verdict="VERDICT: DONE")))

    def test_a_crashed_remote_unit_is_relaunched_only_with_its_collected_patch_applied(self):
        """Catches (panel-eng F4): a crashed remote unit relaunched from this machine's tree, without the code it wrote (kept
        only in its remote tree and out/<unit>/changes.patch), and the orchestrator not told where that code is. And row 249: a
        remote unit whose collected patch applies to its worktree here stayed dead, where a local one is relaunched."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/tmp/x"\n[autopilot]\nenabled = true\n')
        u = self.unit(host="far", collected_at=time.time() + 60)
        janus.tend_autopilot(cfg, {}, [u], [], lambda m: None)
        self.assertEqual(self.launched, [], "a remote unit was relaunched from this machine's tree")
        F = dict(janus.autopilot_findings(cfg, [u]))
        self.assertIn("out/u1/changes.patch", F.get("unit:u1:relaunch", ""), F)
        g = lambda c, *a: subprocess.run(["git", "-C", str(c), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True).stdout
        r = d / "repo"; r.mkdir(); wt = d / "wt"; g(r, "init", "-q", "-b", "main"); (r / "a").write_text("1\n"); g(r, "add", "a"); g(r, "commit", "-qm", "base")
        g(r, "worktree", "add", "-q", "-b", "wk/u", str(wt)); (wt / "a").write_text("2\n")
        (d / "out/u1").mkdir(parents=True, exist_ok=True); (d / "out/u1/changes.patch").write_text(g(wt, "diff")); g(wt, "checkout", "-q", "a")
        u = self.unit(host="far", collected_at=time.time() + 60, cwd=str(wt), run="u1.2")
        self.assertEqual(janus.autopilot_findings(cfg, [u]), [], "a remote unit whose patch can be carried is still left to the orchestrator")
        janus.tend_autopilot(cfg, {}, [u], [], lambda m: None)
        self.assertEqual([a.unit for a in self.launched], ["u1-r1"]); self.assertEqual((wt / "a").read_text(), "2\n", "relaunched without the code it wrote")

    def test_off_by_default_and_question_answered_only_from_an_exact_memory_subject(self):
        """Catches: the autopilot acting without the owner's opt-in in a project with no spending cap (with a cap it is on by
        default: budget-follow item 8), and answering a question memory does not settle."""
        d, cfg = project(); del cfg.prov["api"]["cap_usd"]
        janus.tend_autopilot(cfg, {}, [self.unit()], [], lambda m: None); self.assertEqual(self.launched, [])
        janus.memory_write(self.cfg, [("Decisions", "2026-11-11", "gate-host", "gates run on the build host", None)])
        qs = [(7, "a1", "", "which host for the [gate-host]?"), (8, "a2", "", "should I gate on another host?")]
        with mock.patch.object(janus, "board_questions", lambda c: qs):
            janus.tend_autopilot(self.cfg, {}, [], [], lambda m: None); janus.tend_autopilot(self.cfg, {}, [], [], lambda m: None)
        ans = [p for p in self.posts if p[0] == "answer"]
        self.assertEqual(len(ans), 1, ans); self.assertEqual(ans[0][1], "7"); self.assertIn("gates run on the build host", ans[0][2])
        self.assertIn("question:7", self.cfg.jget("acks.json"))

    def test_a_question_merely_mentioning_a_memory_key_is_not_answered(self):
        """Catches (panel-eng F7): a question that only mentions a memory key as a word ("may I skip the gate-host check?")
        answered with that entry, so the agent proceeds on a non-answer."""
        janus.memory_write(self.cfg, [("Decisions", "2026-11-11", "gate-host", "gates run on the build host", None)])
        with mock.patch.object(janus, "board_questions", lambda c: [(9, "a3", "", "may I skip the gate-host check for docs?")]):
            janus.tend_autopilot(self.cfg, {}, [], [], lambda m: None)
        self.assertEqual([p for p in self.posts if p[0] == "answer"], [], "answered a question that did not ask for the entry")


class OnceOnly(unittest.TestCase):
    def test_resume_and_wrapup_once_even_when_the_unit_record_save_fails(self):
        """Catches (panel-eng F1): bd.save failing after a resume or a wrap-up, so the next pass derives the same state and
        starts a second agent run on the same session and worktree."""
        import copy
        d, cfg = project(); (d / "out/u").mkdir(parents=True); (d / "out/u/REPORT.md").write_text("VERDICT: PENDING\nWAITING here:7\n")
        rec = {"id": "x-1", "name": "u", "host": "here", "via": "pueue", "model": "m", "started": 0, "parked": {"jobs": ["here:7"], "since": 0, "by": "report"}}
        hs = {"here": {"ok": True, "pueue": {"tasks": {"7": {"status": {"Done": {"result": "Success"}}}}}}}
        calls = []
        def resume(cfg, u, msg, wrapup=False): calls.append(wrapup); u.update(task=99, run="u.r2"); return "queued a new run"
        def bad_save(self, u): raise janus.Err("bd update: database is locked")
        with mock.patch.object(janus, "resume_unit", resume), mock.patch.object(janus.Beads, "save", bad_save), \
             mock.patch.object(janus.Beads, "comment", lambda s, *a: None), mock.patch.object(janus, "wrap_due", lambda c, u: not u.get("_park")):
            for _ in range(2):
                u = copy.deepcopy(rec); u["_park"] = ["here:7"]; janus.tend_parked(cfg, hs, [u], lambda m: None)
            self.assertEqual(calls, [False], "a parked unit was resumed twice")
            for _ in range(2): janus.tend_parked(cfg, hs, [copy.deepcopy(rec)], lambda m: None)
            self.assertEqual(calls, [False, True], "a capped run was wrapped up twice")


class MoveOffDownHost(unittest.TestCase):
    def test_a_moved_job_never_runs_twice_and_device_jobs_stay(self):
        """Catches (panel-eng F3): after a partition the host's old copy of a moved job had started, was not cancelled on its
        return and ran twice; and a job needing a device (--usb) moved to a host without it."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/tmp/x"\n[autopilot]\nenabled = true\n')
        jd = cfg.f("runs") / "jobs"; jd.mkdir(parents=True)
        jd.joinpath("j1.json").write_text(json.dumps({"host": "far", "task": 5, "unit": "u"}))
        jd.joinpath("j2.json").write_text(json.dumps({"host": "far", "task": 6, "unit": "u", "usb": True}))
        cancels, queued = [], []
        q = {"status": "Queued", "label": "unit:u@t"}; r = {"status": "Running", "label": "unit:u@t"}
        with mock.patch.object(janus, "cancel_job", lambda cfg, ref: cancels.append(ref) or ["cancelled"]), \
             mock.patch.object(janus, "requeue", lambda cfg, spec, to: queued.append(spec["task"]) or "here:9"), \
             mock.patch.object(janus, "requeue_host", lambda *a: "here"), mock.patch.object(janus, "board", lambda *a, **k: (0, "", "")):
            janus.move_off_down_hosts(cfg, {"far": {"ok": True, "pueue": {"tasks": {"5": q, "6": q}}}, "here": {"ok": True}}, [], lambda m: None)
            cfg.jput("hosts.json", {"far": time.time() - 3600})
            janus.move_off_down_hosts(cfg, {"far": {"ok": False}, "here": {"ok": True}}, [], lambda m: None)
            self.assertEqual(queued, [5], "the device job was moved, or the plain one was not")
            janus.move_off_down_hosts(cfg, {"far": {"ok": True, "pueue": {"tasks": {"5": r, "6": q}}}, "here": {"ok": True}}, [], lambda m: None)
        self.assertEqual(cancels, ["far:5"], "the old copy that started during the partition was not cancelled")


class TwoProjects(unittest.TestCase):
    def test_project_b_never_touches_project_a_jobs_on_a_shared_queue(self):
        """Catches (cross-project safety): on one queue shared by projects a and b, b's watcher and autopilot calling a's job an
        orphan or stuck and cancelling or re-queuing it; and a job with no project id acted on instead of listed."""
        d, cfg = project("\n[autopilot]\nenabled = true\n"); cfg.c["project"]["id"] = "b"
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3 * 86400))
        job = lambda lab, jid: {"id": int(jid[1:]), "group": "jobs", "label": lab, "command": f"bash /r/runs/{jid}.sh", "status": {"Running": {"enqueued_at": old, "start": old}}}
        hs = {"here": {"ok": True, "live": ["j1 99999", "j2 99999", "j3 99999"], "pueue": {"groups": {"jobs": {"status": "Running", "parallel_tasks": 4}},
                                             "tasks": {"1": job("unit:x@a", "j1"), "2": job("unit:gone@b", "j2"), "3": job("unit:y", "j3")}}}}
        jd = cfg.f("runs") / "jobs"; jd.mkdir(parents=True)
        for jid, tid in (("j1", 1), ("j2", 2), ("j3", 3)):   # b's records even name a's task (an id reused across projects)
            (jd / f"{jid}.json").write_text(json.dumps({"host": "here", "task": tid, "unit": "gone", "wt": str(d), "cmd": "make"}))
        acted = []
        with mock.patch.object(janus, "cancel_job", lambda cfg, ref, force=False: acted.append(ref) or ["cancelled"]), \
             mock.patch.object(janus, "requeue", lambda cfg, spec, to: acted.append(f"requeue {spec['task']}") or "here:9"), \
             mock.patch.object(janus, "Beads", NoBeads), mock.patch.object(janus, "board", lambda *a, **k: (0, "", "")):
            self.assertEqual([r for r, _ in janus.orphan_jobs(cfg, hs, [])], ["here:2"])
            F = [janus.finding(k, t) for k, t in janus.lane_findings(cfg, hs, [])]
            self.assertEqual([f.key for f in F if f.key.startswith("age:")], ["age:here:2"])
            janus.tend_autopilot(cfg, hs, [], F, lambda m: None)
        self.assertEqual(acted, ["here:2", "requeue 2"], "b acted on a job that is not b's")


if __name__ == "__main__":
    unittest.main()
