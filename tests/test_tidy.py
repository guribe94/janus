"""janus tidy: recorded holds, the safe auto pass, and per-worktree artifacts.

The failures caught: (soak 41) tidy had no way to keep one item, so a clean worktree an open unit still depends on (not as its
cwd) would be removed with the rest and the orchestrator skipped the whole tidy, tidy:<n> firing forever; (keep-clean / S1
keep-build-output) nothing removed a closed worktree's rebuildable output, and nothing ran the safe removals without the
orchestrator. Standard library only.

Run by ./check (it discovers tests/test_*.py)."""
import tempfile, unittest
from pathlib import Path

from janusload import load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    (d / "janus.toml").write_text('[project]\nname = "t"\n[paths]\nworktrees = "wt"\n[hosts.here]\nlocal = true\n' + extra)
    (d / "out").mkdir(); (d / "wt").mkdir()
    return d, janus.Cfg(d)


class Holds(unittest.TestCase):
    def test_a_hold_keeps_its_item_out_of_the_plan(self):
        d, cfg = project()
        self.assertIsNone(janus.held_by(cfg, "/x/wt/foo"))
        janus.hold_item(cfg, "/x/wt/foo", "an open unit still depends on it")
        self.assertEqual(janus.held_by(cfg, "/x/wt/foo"), "/x/wt/foo")
        self.assertEqual(janus.held_by(cfg, "/x/wt/foo/sub"), "/x/wt/foo")               # under it
        P = [("worktree", "/x/wt/foo", "w", 0), ("worktree", "/x/wt/bar", "w", 0)]
        self.assertEqual([t for _, t, _, _ in janus.apply_holds(cfg, P)], ["/x/wt/bar"])

    def test_cmd_tidy_hold_list_unhold(self):
        d, cfg = project()
        janus.cmd_tidy(cfg, type("A", (), {"action": "hold", "target": "/x/wt/foo", "why": "dep", "dry_run": False, "run": None})())
        self.assertIn("/x/wt/foo", cfg.jget("tidy-holds.json"))
        rc, L = janus.cmd_tidy(cfg, type("A", (), {"action": "holds", "target": None, "why": None, "dry_run": False, "run": None})())
        self.assertEqual(rc, 0); self.assertTrue(any("/x/wt/foo" in x for x in L), L)
        janus.cmd_tidy(cfg, type("A", (), {"action": "unhold", "target": "/x/wt/foo", "why": None, "dry_run": False, "run": None})())
        self.assertEqual(cfg.jget("tidy-holds.json"), {})


class Auto(unittest.TestCase):
    def test_auto_removes_only_the_safe_kinds(self):
        d, cfg = project("\n[tidy]\nauto = true\nevery_min = 0\n")
        calls = []
        janus.tidy_all = lambda *a, **k: [("worktree", "/x/w", "why", 0), ("container", "h:c", "why", 0)]
        janus.tidy_do = lambda cfg2, kind, t: (calls.append((kind, t)), (0, ""))[1]
        janus.tend_tidy(cfg, {}, [], lambda m: None)
        self.assertEqual(calls, [("worktree", "/x/w")])                                  # the container needs the orchestrator

    def test_auto_off_removes_nothing(self):
        d, cfg = project("\n[tidy]\nevery_min = 0\n")
        calls = []
        janus.tidy_all = lambda *a, **k: [("worktree", "/x/w", "why", 0)]
        janus.tidy_do = lambda cfg2, kind, t: (calls.append((kind, t)), (0, ""))[1]
        janus.tend_tidy(cfg, {}, [], lambda m: None)
        self.assertEqual(calls, [])


class Artifacts(unittest.TestCase):
    def test_artifacts_of_a_closed_unit_are_removed_open_untouched(self):
        d, cfg = project('\n[tidy]\nartifacts = ["target"]\n')
        cw = d / "wt/closed1"; cw.mkdir(parents=True); (cw / "target").mkdir(); (cw / "target/x").write_text("x")
        ow = d / "wt/open1"; ow.mkdir(parents=True); (ow / "target").mkdir()
        janus.all_units = lambda c: {"closed1": {"status": "closed"}, "open1": {"status": "in_progress"}}
        P = janus.tidy_artifacts(cfg, [{"name": "open1", "cwd": str(ow)}])
        self.assertEqual([(k, t) for k, t, _, _ in P], [("artifact", str(cw / "target"))])   # never the open unit's tree
        rc, e = janus.tidy_do(cfg, "artifact", str(cw / "target"))
        self.assertEqual(rc, 0, e); self.assertFalse((cw / "target").exists())


class DiskFree(unittest.TestCase):
    def test_reads_df_free(self):
        d, cfg = project()
        self.assertGreater(janus.disk_free_gb(cfg.root), 0)     # measured with df, not du (APFS clones over-count)


if __name__ == "__main__":
    unittest.main()
