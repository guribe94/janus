"""skill-fix47 (ISSUES rows 226-230): the result cache of janus run --inputs, the gate-log step logs, the instructions ack,
ready items without briefs (the finding, janus brief draft/accept). One test per failure, each naming what it catches.
Standard library only; no network, no spend. Run by ./check (it discovers tests/test_*.py)."""
import argparse, contextlib, io, json, subprocess, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project


def git(wt, *a): subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)


def tree(d):
    wt = d / "wt"; (wt / "src").mkdir(parents=True); (wt / "other").mkdir()
    (wt / "src/a.txt").write_text("1\n"); (wt / "other/b.txt").write_text("1\n"); (wt / "lock").write_text("L\n")
    git(wt, "init", "-q"); git(wt, "add", "-A"); git(wt, "commit", "-qm", "one")
    return wt


class ResultCache(unittest.TestCase):
    def run_job(self, cfg, wt, exits, no_wait=False, no_cache=False, inputs=("src", "lock"), cmd="make test"):
        """One janus run with pueue faked: (output, jobs queued so far). exits: the exit code each queued job ends with."""
        a = argparse.Namespace(host="here", lane="jobs", unit="u1", wt=str(wt), subdir=None, usb=False, priority=0, no_wait=no_wait,
                               allow_unmerged=True, command=["--", cmd], broad=None, needs=None, inputs=list(inputs), no_cache=no_cache)
        def fake_hrun(h, script, **k):
            if " add " in script: self.queued.append(script); return 0, str(len(self.queued)), ""
            if script.startswith("cat ") and ".rc" in script:   # the job's exit code file on its host
                return (0, str(exits[len(self.queued) - 1]), "") if self.queued else (1, "", "")
            return 0, "", ""
        buf = io.StringIO()
        with mock.patch.object(janus, "hrun", fake_hrun), mock.patch.object(janus, "tree_guard", lambda *a, **k: None), \
             mock.patch.object(janus, "ensure_lanes", lambda h, cfg: ""), mock.patch.object(janus, "write_cap", lambda *a, **k: (0, "", "")), \
             mock.patch.object(janus, "host_facts", lambda cfg, refresh=False: {"here": {"os": "linux", "arch": "x86_64", "features": [], "devices": []}}), \
             mock.patch.object(janus, "finish_job", lambda cfg, hn, h, tid, jid, quiet=False: exits[int(tid) - 1]), \
             contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = janus.cmd_run(cfg, a)
        return rc, buf.getvalue()

    def test_a_job_whose_inputs_already_passed_is_not_run_again_until_an_input_changes(self):
        """Catches: the same command run twice on the same inputs (two gates of one tree ran the same test step twice); and a
        cache that would answer PASS after an input changed, or for a path outside the declared inputs' effect."""
        d, cfg = project(); wt = tree(d); self.queued = []
        rc, out = self.run_job(cfg, wt, [0]); self.assertEqual((rc, len(self.queued)), (0, 1))
        rc, out = self.run_job(cfg, wt, [0])
        self.assertEqual(len(self.queued), 1, "the same job on the same inputs was queued again")
        self.assertEqual(rc, 0); self.assertRegex(out, r"^CACHED PASS [0-9a-f]{16} from here:1 \d{4}-\d\d-\d\d \d\d:\d\d\n$")
        (wt / "other/b.txt").write_text("2\n")   # not an input: still cached
        self.run_job(cfg, wt, [0]); self.assertEqual(len(self.queued), 1, "a file outside the inputs ran the job again")
        (wt / "src/a.txt").write_text("2\n")     # a dirty input: runs again
        self.run_job(cfg, wt, [0, 0]); self.assertEqual(len(self.queued), 2, "a changed input answered CACHED PASS")
        self.run_job(cfg, wt, [0, 0]); self.assertEqual(len(self.queued), 2, "the dirty tree's pass was not kept")
        git(wt, "commit", "-qam", "two")         # committed: a new key (git's tree hash), runs once more
        self.run_job(cfg, wt, [0, 0, 0]); self.assertEqual(len(self.queued), 3)
        self.run_job(cfg, wt, [0, 0, 0, 0], cmd="make other"); self.assertEqual(len(self.queued), 4, "another command answered CACHED PASS")
        self.run_job(cfg, wt, [0, 0, 0, 0, 0], no_cache=True); self.assertEqual(len(self.queued), 5, "--no-cache did not force a run")

    def test_a_failure_is_never_cached_and_a_detached_pass_is(self):
        """Catches: a failed job answered from the cache (a red step turning green unrun), and a detached job (its exit code
        known only on its host, later) never counting as a pass."""
        d, cfg = project(); wt = tree(d); self.queued = []
        self.run_job(cfg, wt, [1]); self.run_job(cfg, wt, [1, 0])
        self.assertEqual(len(self.queued), 2, "a failed job was answered from the cache")
        self.queued = []; (wt / "src/a.txt").write_text("3\n")
        rc, out = self.run_job(cfg, wt, [0], no_wait=True); self.assertEqual(out, "here:1\n")
        rc, out = self.run_job(cfg, wt, [0], no_wait=True)
        self.assertEqual(len(self.queued), 1, "a detached job that passed was run again"); self.assertIn("CACHED PASS", out)
        self.queued = []; (wt / "src/a.txt").write_text("4\n")
        self.run_job(cfg, wt, [7], no_wait=True); self.run_job(cfg, wt, [7, 0], no_wait=True)
        self.assertEqual(len(self.queued), 2, "a detached job that failed was answered from the cache")
        self.assertLessEqual(len(list(cfg.f("cache").glob("*.json"))), 3)

    def test_the_store_is_bounded_and_the_toolchain_is_in_the_key(self):
        """Catches: a cache that grows without bound, and a pass kept across a toolchain change."""
        d, cfg = project("\n[cache]\nmax = 3\ntoolchain = \"rust 1\"\n"); wt = tree(d)
        hh = janus.input_hashes(wt, ["src"]); f = {"os": "linux", "arch": "x86_64"}
        k1 = janus.cache_key(cfg, "here", f, "c", None, hh); cfg.c["cache"]["toolchain"] = "rust 2"
        self.assertNotEqual(k1, janus.cache_key(cfg, "here", f, "c", None, hh))
        self.assertNotEqual(k1, janus.cache_key(cfg, "here", {"os": "darwin", "arch": "arm64"}, "c", None, hh))
        from janus import cache
        for i in range(6): cache.record(cfg, f"k{i}", "here", i, f"j{i}", "c")
        self.assertEqual(len(list(cfg.f("cache").glob("*.json"))), 3)
        self.assertIsNone(janus.input_hashes(d, ["x"]), "a tree that is no git checkout must cache nothing")


class GateLogStepLogs(unittest.TestCase):
    def test_a_failing_steps_test_ids_and_first_error_come_from_its_step_log(self):
        """Catches (row 228): gate-log printing the step exits and 'no error-looking line' for a gate whose steps write their
        output to out/<gate>/steps/<step>.log: no failing test id, no error, although both are in the step log."""
        d, cfg = project(); o = d / "out/g"; (o / "steps").mkdir(parents=True)
        (o / "log").write_text("rc_unit=101\nEXIT_unit=1\nEXIT_py=1\nEXIT_lint=0\ngate duration: 5 s\n")
        (o / "steps/unit.log").write_text("test a::a_failed_start_keeps_frames ... ok\ntest a::paces_the_next_pulse ... FAILED\n\nfailures:\n"
                                          "thread 'a::paces_the_next_pulse' panicked at src/a.rs:9:5:\nassertion `left == right` failed\n"
                                          "test result: FAILED. 1 passed; 1 failed\n")
        (o / "steps/py.log").write_text("PASSED tests/t.py::test_failed_read_is_ok\nE   AssertionError: 3 != 4\n"
                                        "FAILED tests/t.py::test_sum - AssertionError: 3 != 4\n")
        (o / "steps/lint.log").write_text("CACHED PASS 0123456789abcdef from here:4 2026-01-01 00:00\n")
        (d / "out/gates.log").write_text("2026-01-01 00:00:00 g EXIT_unit=1 EXIT_py=1 RESULT: FAIL\n")
        rc, L = janus.cmd_gate_log(cfg, type("A", (), {"name": "g", "task": None})()); t = "\n".join(L)
        self.assertEqual(rc, 1)
        self.assertIn("step unit: failed test(s) (1): a::paces_the_next_pulse", t)
        self.assertIn("panicked at src/a.rs:9:5", t)
        self.assertIn("step py: failed test(s) (1): tests/t.py::test_sum", t)
        self.assertIn("AssertionError: 3 != 4", t)
        self.assertNotIn("a_failed_start_keeps_frames", t, "a passing test whose name holds 'failed' was shown as the error")
        self.assertNotIn("no error-looking line", t)
        self.assertIn("step lint: CACHED PASS 0123456789abcdef from here:4", t)


class InstructionsAck(unittest.TestCase):
    def test_an_ack_cannot_clear_the_instructions_finding(self):
        """Catches (row 229): `janus ack instructions ...` hid the unclassified owner instructions (13 acks in one evening)
        instead of each being closed or remembered; an old ack in acks.json must not hide the finding either."""
        d, cfg = project()
        with self.assertRaises(janus.Err) as e: janus.cmd_ack(cfg, argparse.Namespace(problem="instructions", decision="seen", decision_class=False))
        self.assertIn("bd close", str(e.exception)); self.assertIn("bd remember", str(e.exception))
        cfg.jput("acks.json", {"instructions": {"t": janus.now(), "d": "seen"}})
        self.assertEqual(janus.live_acks(cfg, {"instructions"}), {})


def fake_beads(store):
    """A beads double over a dict of issues: what janus intent, ack and the findings read and write."""
    class FakeBeads:
        def __init__(self, cfg): pass
        def run(self, *a):
            if a[0] == "create":
                i = f"x{len(store) + 1}"; store[i] = {"id": i, "title": a[1], "labels": [a[a.index("-l") + 1]], "status": "open", "notes": ""}
                return {"id": i}
            if a[0] == "update": store[a[1]]["notes"] = a[a.index("--notes") + 1]
            if a[0] == "close": store[a[1]]["status"] = "closed"
            return []
        def issues(self, label, status="open"): return [i for i in store.values() if label in i["labels"] and i["status"] in status.split(",")]
        def create(self, title, label, desc=""): return self.run("create", title, "-l", label, "-d", desc)["id"]
        def units(self): return []
        def comment(self, i, text): pass
    return FakeBeads


class Intents(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project(); self.store = {"ins1": {"id": "ins1", "title": "instr: build the retention permit", "labels": ["instruction"], "status": "open"}}
        p = mock.patch.object(janus, "Beads", fake_beads(self.store)); p.start(); self.addCleanup(p.stop)

    def intent(self, action, *args, **k):
        return janus.cmd_intent(self.cfg, argparse.Namespace(action=action, args=list(args), why=k.get("why"), done_means=k.get("done_means"),
                                                             instruction=k.get("instruction"), unit=k.get("unit"), id=k.get("id")))

    def later(self, h): return mock.patch.object(janus, "now", lambda t=janus.now() + h * 3600: t)

    def test_an_intent_is_in_the_live_state_and_tracks_its_instruction_until_it_is_stale_or_done(self):
        """Catches (row 247): work in progress kept only in hand-written notes (a context clear lost an owner request), and
        the instructions finding clearable only by closing the instruction before its work was built."""
        ins = lambda: dict(janus.session_findings(self.cfg, 0, [])).get("instructions", "")
        self.assertIn("janus intent add --instruction", ins())
        self.intent("add", "build the retention permit", done_means="landed on main", instruction="ins1", unit=["u1"])
        i = next(k for k, x in self.store.items() if "intent" in x["labels"])
        live = janus.live_state(self.cfg, {}, [], [])
        for w in ("Intents:", i, "build the retention permit", "landed on main", "ins1", "u1"): self.assertIn(w, live)
        self.assertEqual(ins(), "", "an instruction with a live intent was still unclassified")
        with self.later(3): self.assertIn("build the retention permit", ins(), "a stale intent kept its instruction quiet")
        self.intent("done", i)
        self.assertNotIn("build the retention permit", janus.live_state(self.cfg, {}, [], []))

    def test_an_intent_nothing_works_on_goes_stale_and_is_urgent(self):
        """Catches (row 247): an intent whose unit ended (or never started) sitting unnoticed: after [intents] stale_h (default
        2 h) without a running linked unit or an update, check raises the urgent intent:<id>:stale."""
        self.intent("add", "build the retention permit", unit=["u1"]); i = next(k for k, x in self.store.items() if "intent" in x["labels"])
        key = f"intent:{i}:stale"; keys = lambda units: [k for k, _ in janus.intent_findings(self.cfg, units)]
        self.assertEqual(keys([]), [])
        with self.later(3):
            self.assertEqual(keys([{"name": "u1", "state": "working"}]), [], "stale while its unit runs")
            self.assertEqual(keys([{"name": "u1", "state": "done"}]), [key]); self.assertTrue(janus.finding(key, "x").urgent)
            self.intent("add", id=i, unit=["u2"])   # an update moves it
            self.assertEqual(keys([]), [])


class ReadyItemsWithoutBriefs(unittest.TestCase):
    ISS = {"backlog:A1": {"id": "b1", "title": "backlog: A1 first thing", "status": "open", "labels": ["backlog", "backlog-ready"], "priority": 1},
           "backlog:B2": {"id": "b2", "title": "backlog: B2 second thing", "status": "open", "labels": ["backlog", "backlog-ready"], "priority": 2}}

    def proj(self):
        d, cfg = project("\n[backlog]\nfile = \"LIST.md\"\n"); (d / "briefs").mkdir(exist_ok=True); (d / "LIST.md").write_text("x\n")
        cfg.touch("backlog_imported")
        return d, cfg

    def test_the_finding_names_the_count_the_items_and_the_drafting_command(self):
        """Catches (row 227): hosts with room and ready items without briefs, and check naming neither the item ids nor a way
        to get briefs (so the fleet idled); and an ack of it lasting 4 h (the evening's silence came from one)."""
        d, cfg = self.proj()
        with mock.patch.object(janus, "backlog_issues", lambda cfg: self.ISS), mock.patch.object(janus, "room", lambda *a, **k: (3, "")), \
             mock.patch.object(janus, "all_units", lambda cfg: {}):
            F = dict(janus.backlog_findings(cfg, {"here": {"ok": True}}, []))
        t = F.get("idle-backlog:nobrief", "")
        self.assertIn("2 ready backlog item(s)", t); self.assertIn("janus brief draft A1 B2", t); self.assertIn("first thing", t)
        self.assertLessEqual(janus.ack_ttl("idle-backlog:nobrief"), 3600)
        # rows 29/99: an item held on purpose (janus backlog set A1 hold=..., or HOLD: in its draft) neither counts nor nags
        held = {**self.ISS, "backlog:A1": {**self.ISS["backlog:A1"], "labels": ["backlog", "backlog-ready", "hold:parked-by-owner"]}}
        (d / "briefs" / "drafts").mkdir(); (d / "briefs" / "drafts" / "B2.md").write_text("# draft B2\nHOLD: waits for the owner's answer\n")
        with mock.patch.object(janus, "backlog_issues", lambda cfg: held), mock.patch.object(janus, "room", lambda *a, **k: (3, "")), \
             mock.patch.object(janus, "all_units", lambda cfg: {}):
            self.assertEqual(dict(janus.backlog_findings(cfg, {"here": {"ok": True}}, [])), {})

    def test_a_second_ack_needs_fewer_items_without_a_brief(self):
        """Catches (row 245): idle-backlog:nobrief acked again and again (four re-acks, each hiding it 1 h) while not one brief
        was written, so ready work stayed hidden for hours."""
        d, cfg = self.proj()
        def seen(iss):
            with mock.patch.object(janus, "backlog_issues", lambda cfg: iss), mock.patch.object(janus, "room", lambda *a, **k: (3, "")), \
                 mock.patch.object(janus, "all_units", lambda cfg: {}):
                self.assertIn("idle-backlog:nobrief", dict(janus.backlog_findings(cfg, {"here": {"ok": True}}, [])))
        def ack():
            with mock.patch.object(janus, "Beads", fake_beads({})), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                janus.cmd_ack(cfg, argparse.Namespace(problem="idle-backlog:nobrief", decision="seen", decision_class=False))
        seen(self.ISS); ack(); seen(self.ISS)
        with self.assertRaises(janus.Err) as e: ack()
        self.assertIn("janus brief draft", str(e.exception))
        seen({"backlog:A1": self.ISS["backlog:A1"]}); ack()   # one item fewer: progress

    def test_a_brief_that_mentions_an_item_does_not_cover_it(self):
        """Catches (row 245): every word of a brief's first lines read as an item id, so an open unit whose brief said 'A12'
        hid the ready item A12 from the idle-backlog findings and from fill."""
        d, cfg = self.proj(); b = d / "briefs" / "pump.md"; b.write_text("# Brief: the pump fix follows A12 and B2\nKIND: implement\n")
        self.assertEqual(janus.unit_items(cfg, [{"name": "pump", "brief": str(b)}]), set())
        b.write_text("# Brief: the pump\nITEM: B2\n")
        self.assertEqual(janus.unit_items(cfg, [{"name": "pump", "brief": str(b)}, {"name": "v", "item": "C3"}]), {"B2", "C3"})

    def test_a_draft_launches_one_cheap_unit_and_only_an_accepted_draft_can_be_filled(self):
        """Catches: a drafted brief launched unaccepted (fill reading briefs/drafts), a draft unit started on the critical
        route, and an accepted brief that fill cannot match to its item (no ITEM: line)."""
        d, cfg = self.proj(); cfg.route["draft"] = ["m"]; launched = []
        with mock.patch.object(janus, "backlog_issues", lambda cfg: self.ISS), mock.patch.object(janus, "all_units", lambda cfg: {}), \
             mock.patch.object(janus, "cmd_launch", lambda cfg, a: launched.append(a)), mock.patch.object(janus, "model_ok", lambda cfg, m: (True, "")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(janus.Err): janus.cmd_brief(cfg, argparse.Namespace(action="draft", items=["ZZ"], as_name=None, model=None))
            janus.cmd_brief(cfg, argparse.Namespace(action="draft", items=["A1", "B2"], as_name=None, model=None))
            self.assertEqual(len(launched), 1); a = launched[0]
            self.assertEqual((a.tier, a.model), ("normal", "m"))
            bt = Path(a.brief).read_text(); self.assertIn("A1: first thing", bt); self.assertIn("B2: second thing", bt)
            self.assertIn(str(d / "briefs/drafts"), bt)
            dr = d / "briefs/drafts"; dr.mkdir(); (dr / "A1.md").write_text("# Brief: first-thing: do the first thing\nTIER: NORMAL\n\nSCOPE: x\n")
            (dr / "B2.md").write_text("HOLD: needs the owner\n")
            self.assertEqual([x[2] for x in janus.fill_candidates(cfg, []) if x[2] in ("A1", "B2")], [], "a draft was a fill candidate before it was accepted")
            with self.assertRaises(janus.Err): janus.cmd_brief(cfg, argparse.Namespace(action="accept", items=["B2"], as_name=None, model=None))
            janus.cmd_brief(cfg, argparse.Namespace(action="accept", items=["A1"], as_name=None, model=None))
            self.assertFalse((dr / "A1.md").exists()); self.assertIn("ITEM: A1", (d / "briefs/first-thing.md").read_text().splitlines()[:3])
            self.assertEqual([(x[2], x[4]) for x in janus.fill_candidates(cfg, [])], [("A1", "first-thing")])


if __name__ == "__main__":
    unittest.main()
