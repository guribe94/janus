"""Soak bugs 43, 43b, 44, 45: one test per bug, each naming the failure it catches. Standard library only; no network, no spend.

Run by ./check (it discovers tests/test_*.py)."""
import os, subprocess, time, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project


class FakeBeads:
    units_ = []; closed = []
    def __init__(self, cfg): pass
    def units(self): return self.units_
    def save(self, u): pass
    def comment(self, i, t): pass
    def run(self, *a): self.closed.append(a) if a[0] == "close" else None; return []


def running(task): return {"name": "here", "ok": True, "pueue": {"tasks": {str(task): {"status": "Running"}}}}
GONE = {"name": "here", "ok": True, "pueue": {"tasks": {}}}


class Soak45HoldPaths(unittest.TestCase):
    def test_a_hold_given_as_a_real_path_keeps_a_candidate_named_through_dot_dot(self):
        """Catches (soak 45): `janus tidy hold <real path>` then `janus tidy` removed the worktree, because tidy names its
        candidates <root>/../worktrees/<name> and the hold compared the raw strings."""
        d, cfg = project(); (d / "wt/keep").mkdir(parents=True); (d / "sub").mkdir()
        janus.hold_item(cfg, str((d / "wt/keep").resolve()), "golden tree")
        cand = f"{d}/sub/../wt/keep"
        self.assertIsNotNone(janus.held_by(cfg, cand), "the held worktree would be removed")
        self.assertEqual(janus.apply_holds(cfg, [("worktree", cand, "w", 0), ("worktree", f"{d}/sub/../wt/other", "w", 0)]),
                         [("worktree", f"{d}/sub/../wt/other", "w", 0)])


class Soak44UnitsWorktree(unittest.TestCase):
    def test_tidy_keeps_a_worktree_named_after_an_open_unit_and_launch_says_when_it_makes_none(self):
        """Catches (soak 44): a unit launched without a worktree records cwd = the project root; the tree its agent then made
        (folder or branch named after the unit) was listed by tidy as "no open unit" and would have been removed. And launch
        said nothing about making no worktree."""
        d, cfg = project('\n[paths]\nworktrees = "wt"\n'); repo = d / "repo"; repo.mkdir(); (d / "wt").mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main"); git("commit", "-q", "--allow-empty", "-m", "c")
        for folder, br in (("u1", "zz"), ("tree-a", "u2"), ("free", "free")): git("worktree", "add", "-q", "-b", br, str(d / "wt" / folder))
        units = [{"name": n, "id": n, "cwd": str(d)} for n in ("u1", "u2")]; kept = []
        with mock.patch.object(janus, "landed", lambda cfg, u: True), mock.patch.object(janus, "all_units", lambda cfg: {}):
            P = janus.tidy_plan(cfg, units, ag={}, rows={}, kept=kept)
        self.assertEqual([Path(t).name for k, t, _ in P if k == "worktree"], ["free"], "an open unit's own worktree would be removed")
        self.assertEqual(sorted(Path(p).name for p, why in kept if "open unit" in why), ["tree-a", "u1"])
        a = type("A", (), {"cwd": None, "new_worktree": None})()
        cwd, note = janus.launch_cwd(cfg, a, "u3")
        self.assertEqual(cwd, d.resolve()); self.assertIn("no worktree", note)
        (d / "wt/u3").mkdir(); self.assertEqual(janus.launch_cwd(cfg, a, "u3"), ((d / "wt/u3").resolve(), None))


class Soak43bOwnReport(unittest.TestCase):
    def test_a_unit_that_continues_another_units_report_writes_its_own(self):
        """Catches (soak 43b): `janus launch --resume-of X` recorded X's REPORT.md as the new unit's report, so the new unit and
        `janus collect` of it wrote over out/X/REPORT.md and X's status read "no verdict"."""
        d, cfg = project(); (d / "out/orig").mkdir(parents=True); (d / "out/r").mkdir()
        rep = d / "out/orig/REPORT.md"; old = "VERDICT: BLOCKED\nstate\nWAITING here:9\nVERDICT: BLOCKED - x\n"; rep.write_text(old)
        os.utime(rep, (time.time() - 60, time.time() - 60))
        a = type("A", (), {"report": None, "resume_of": "orig", "unit": "r"})()
        self.assertIsNone(janus.resume_report(cfg, a, None), "the new unit's report is the original's file")
        self.assertEqual(janus.seed_report(cfg, a), rep.resolve())
        own = d / "out/r/REPORT.md"; janus.write_seed(rep, own)
        self.assertEqual(own.read_text(), "VERDICT: PENDING\n" + old.split("\n", 1)[1]); self.assertEqual(rep.read_text(), old)
        u = {"name": "r", "resume": True, "started": int(time.time())}
        self.assertEqual(janus.report_path(cfg, u), own)
        self.assertEqual((janus.verdict(cfg, u), janus.waiting_line_jobs(cfg, u)), (None, []), "the copied report's old lines count")

    def test_collect_never_copies_a_remote_units_report_into_another_units_folder(self):
        d, _ = project(); (d / "janus.toml").write_text((d / "janus.toml").read_text() + '\n[hosts.far]\nssh = "far"\nroot = "/r"\n')
        cfg = janus.Cfg(d); (d / "out/orig").mkdir(parents=True); (d / "out/r").mkdir()
        (d / "out/orig/REPORT.md").write_text("VERDICT: DONE - orig\n"); (d / "out/r/REPORT.md").write_text("VERDICT: PENDING\nr\n")
        u = {"name": "r", "host": "far", "via": "tmux", "report": str(d / "out/orig/REPORT.md"), "resume": True}   # a record made before the fix
        with mock.patch.object(janus, "sh", lambda *a, **k: (0, "", "")): self.assertTrue(janus.save_output(cfg, u))
        self.assertEqual((d / "out/orig/REPORT.md").read_text(), "VERDICT: DONE - orig\n", "collect wrote over the original unit's report")
        self.assertEqual(janus.report_path(cfg, u), d / "out/r/REPORT.md")


class Soak43NoDuplicate(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project("\n[autopilot]\nenabled = true\n"); self.launched = []; FakeBeads.units_ = []; FakeBeads.closed = []
        self.p = [mock.patch.object(janus, "Beads", FakeBeads), mock.patch.object(janus, "cmd_launch", lambda cfg, a: self.launched.append(a)),
                  mock.patch.object(janus, "board", lambda c, *a, **k: (0, "", "")), mock.patch.object(janus, "board_questions", lambda c: []),
                  mock.patch.object(janus, "herdr", lambda *a, **k: None), mock.patch.object(janus, "X", lambda *a, **k: (0, "", "")),
                  mock.patch.object(janus, "save_task_log", lambda *a, **k: None)]
        for x in self.p: x.start()
    def tearDown(self):
        for x in self.p: x.stop()

    def unit(self, **k): return {"name": "u1", "id": "b-1", "host": "here", "state": "done", "via": "pueue", "task": 7, "model": "m", "brief": "b.md", "cwd": str(self.d / "tree"), "run": "u1.1", **k}
    def tend(self, u, probe, hs=None):
        with mock.patch.object(janus, "probe", lambda *a, **k: probe): janus.tend_autopilot(self.cfg, hs or {}, [u], [], lambda m: None)
        return [a.unit for a in self.launched]

    def test_no_relaunch_while_the_units_own_resume_runs_or_its_job_is_live(self):
        """Catches (soak 43): a unit resumed after the watcher pass probed it (state still 'done' in the pass) was judged
        crashed and relaunched as <unit>-r1 while its own resume ran; and a unit whose report parks it on a running job."""
        self.assertEqual(self.tend(self.unit(), running(7)), [], "relaunched while its own session runs")
        (self.d / "out/u1").mkdir(parents=True); (self.d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nWAITING here:9\n")
        self.assertEqual(self.tend(self.unit(), GONE, hs={"here": running(9)}), [], "relaunched while parked on a live job")
        self.assertEqual(self.tend(self.unit(), GONE, hs={"here": GONE}), ["u1-r1"], "a unit that really crashed is not relaunched")
        a = self.launched[0]
        self.assertEqual((a.report, a.seed_report, a.share_with), (None, str(self.d / "out/u1/REPORT.md"), "u1"), "the relaunch writes the original's report")

    def test_a_stopped_duplicate_with_no_verdict_is_closed_without_force(self):
        """Catches (soak 43): `janus close <unit>-r1` refused for ever (no verdict, tree shared so --force refused)."""
        (self.d / "tree").mkdir(); FakeBeads.units_ = [self.unit(relaunched="u1-r1"), self.unit(name="u1-r1", id="b-2", task=8, relaunch_of="u1"), self.unit(name="u9", id="b-9")]
        A = lambda **k: type("A", (), {"unit": "u1-r1", "force": False, "reason": "duplicate", "duplicate_of": None, **k})()
        with mock.patch.object(janus, "probe", lambda *a, **k: running(8)), mock.patch.object(janus, "settle_run", lambda *a, **k: None):
            with self.assertRaisesRegex(janus.Err, "no final VERDICT"): janus.cmd_close(self.cfg, A())
            with self.assertRaisesRegex(janus.Err, "still working"): janus.cmd_close(self.cfg, A(duplicate_of="u1"))
        with mock.patch.object(janus, "probe", lambda *a, **k: GONE), mock.patch.object(janus, "settle_run", lambda *a, **k: None):
            with self.assertRaisesRegex(janus.Err, "not a relaunch of u9"): janus.cmd_close(self.cfg, A(duplicate_of="u9"))
            self.assertEqual(FakeBeads.closed, [])
            janus.cmd_close(self.cfg, A(duplicate_of="u1"))
        self.assertEqual([c[1] for c in FakeBeads.closed], ["b-2"])


if __name__ == "__main__":
    unittest.main()
