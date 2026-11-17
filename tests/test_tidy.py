"""janus tidy: recorded holds, the safe auto pass, and per-worktree artifacts.

The failures caught: (soak 41) tidy had no way to keep one item, so a clean worktree an open unit still depends on (not as its
cwd) would be removed with the rest and the orchestrator skipped the whole tidy, tidy:<n> firing forever; (keep-clean / S1
keep-build-output) nothing removed a closed worktree's rebuildable output, and nothing ran the safe removals without the
orchestrator. Standard library only.

Run by ./check (it discovers tests/test_*.py)."""
from unittest import mock
import tempfile, unittest
from pathlib import Path

from janusload import conf, load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    conf(d).write_text('[project]\nname = "t"\n[paths]\nworktrees = "wt"\n[hosts.here]\nlocal = true\n' + extra)
    (d / ".janus" / "out").mkdir(); (d / "wt").mkdir()
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
    def setUp(self):
        self.real = (janus.tidy_all, janus.tidy_do)

    def tearDown(self):
        janus.tidy_all, janus.tidy_do = self.real

    def test_auto_removes_only_the_safe_kinds(self):
        d, cfg = project("\n[tidy]\nauto = true\nevery_min = 0\n")
        calls = []
        janus.tidy_all = lambda *a, **k: [("worktree", "/x/w", "why", 0), ("container", "h:c", "why", 0), ("artifact", "/x/w2/target", "why", 0), ("disk", "h", "why", 9)]
        janus.tidy_do = lambda cfg2, kind, t: (calls.append((kind, t)), (0, ""))[1]
        B = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [{"name": "open", "cwd": "/x/open"}]})
        with mock.patch.object(janus, "Beads", B): janus.tend_tidy(cfg, {}, [], lambda m: None)
        self.assertEqual(calls, [("worktree", "/x/w"), ("container", "h:c"), ("artifact", "/x/w2/target")])   # all it found, not only trees and branches (the orchestrator's hooks refuse hand removal); a disk row is a report
        calls.clear(); janus.tidy_all = lambda *a, **k: [("worktree", "/x/open", "why", 0)]
        with mock.patch.object(janus, "Beads", B): janus.tend_tidy(cfg, {}, [], lambda m: None)
        self.assertEqual(calls, [])

    def test_auto_off_removes_nothing(self):
        d, cfg = project("\n[tidy]\nevery_min = 0\n")
        calls = []
        janus.tidy_all = lambda *a, **k: [("worktree", "/x/w", "why", 0)]
        janus.tidy_do = lambda cfg2, kind, t: (calls.append((kind, t)), (0, ""))[1]
        janus.tend_tidy(cfg, {}, [], lambda m: None)
        self.assertEqual(calls, [])


class Upstream(unittest.TestCase):
    def test_landed_on_the_upstream_counts_when_the_local_landing_branch_lags(self):
        """2026-11-17: the pass's fast-forward of main failed, main lagged origin/main by 11 commits, and tidy kept landed
        worktrees and branches as 'not on the landing branch' while the disk filled."""
        import subprocess
        d, cfg = project('\n[landing]\nbranch = "main"\n[tidy]\nbranches = ["*"]\nrepos = ["repo"]\n')
        g = lambda c, *a: subprocess.run(["git", "-C", str(c), *a], capture_output=True, text=True, check=True).stdout
        bare = d / "remote.git"; g(d, "init", "-q", "--bare", "-b", "main", str(bare))
        r = d / "repo"; g(d, "clone", "-q", str(bare), str(r))
        for k, v in (("user.email", "t@t"), ("user.name", "t")): g(r, "config", k, v)
        (r / "f").write_text("0"); g(r, "add", "f"); g(r, "commit", "-qm", "base"); g(r, "push", "-q", "-u", "origin", "main")
        wt = d / "wt/done"; g(r, "worktree", "add", "-q", "-b", "done", str(wt))
        (wt / "f").write_text("1"); g(wt, "commit", "-qam", "work"); g(wt, "push", "-q", "origin", "done:main")   # landed on the remote only
        g(r, "fetch", "-q")
        self.assertNotEqual(g(r, "rev-parse", "main"), g(r, "rev-parse", "origin/main"))                    # local main lags
        with mock.patch.object(janus, "herdr_agents", lambda c: {}), mock.patch.object(janus, "board_rows", lambda c: {}), \
                mock.patch.object(janus, "orphan_tabs", lambda c, n: []):
            self.assertIn(("worktree", str(wt)), [(k, t) for k, t, _ in janus.tidy_plan(cfg, [])])
            g(r, "worktree", "remove", str(wt))
            self.assertIn(("branch", f"{r}:done"), [(k, t) for k, t, _, _ in janus.tidy_branches(cfg, [])])


class Tabs(unittest.TestCase):
    def test_a_units_tab_closes_with_it_and_only_januss_own_tabs_are_flagged(self):
        """Issue 267: a unit's tab stayed open after the unit parked or closed (27 tabs piled up). Park closes the tab that only
        shows an ended run, never a herdr agent's own tab; a closed unit's leftover tab is flagged and tidied by its label,
        and no other tab is ever touched (another label, an open unit, a host that is not this project's)."""
        d, cfg = project(); calls = []
        tabs = [{"tab_id": "T1", "label": "old \u00b7 m \u00b7 here"}, {"tab_id": "T2", "label": "notes"}, {"tab_id": "T3", "label": "live \u00b7 m \u00b7 here"},
                {"tab_id": "T4", "label": "old \u00b7 m \u00b7 elsewhere"}, {"tab_id": "T5", "label": "never \u00b7 m \u00b7 here"}]
        def herdr(*a, **k):
            calls.append(a)
            return {"workspaces": [{"label": "t", "workspace_id": "W"}]} if a[:2] == ("workspace", "list") else {"tabs": tabs} if a[:2] == ("tab", "list") else {}
        old = {k: getattr(janus, k) for k in ("herdr", "all_units")}
        janus.herdr = herdr; janus.all_units = lambda cfg: {"old": {"status": "closed"}, "live": {"status": "open"}}
        try:
            self.assertEqual([k for k, _ in janus.tab_findings(cfg, [{"name": "live"}])], ["tab:old:orphan"])
            self.assertEqual([(k, t) for k, t, _ in janus.tidy_plan(cfg, [{"name": "live"}], ag={}, rows={}) if k == "tab"], [("tab", "T1")])
            self.assertEqual(janus.tidy_do(cfg, "tab", "T1"), (0, "")); self.assertEqual(calls[-1], ("tab", "close", "T1"))
            api = {"name": "live", "via": "pueue", "tab": "T3", "pane": "P3"}; own = {"name": "h", "via": "herdr", "tab": "T9", "pane": "P9"}
            del calls[:]; janus.park_tab(cfg, api); janus.park_tab(cfg, own)
            self.assertEqual(calls, [("tab", "close", "T3")]); self.assertNotIn("tab", api); self.assertEqual(own["tab"], "T9")
        finally:
            for k, v in old.items(): setattr(janus, k, v)


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.real = janus.all_units

    def tearDown(self):
        janus.all_units = self.real

    def test_artifacts_of_a_closed_unit_are_removed_open_untouched(self):
        d, cfg = project('\n[tidy]\nartifacts = ["target"]\n')
        cw = d / "wt/closed1"; cw.mkdir(parents=True); (cw / "target").mkdir(); (cw / "target/x").write_text("x")
        ow = d / "wt/open1"; ow.mkdir(parents=True); (ow / "target").mkdir()
        janus.all_units = lambda c: {"closed1": {"status": "closed"}, "open1": {"status": "in_progress"}}
        P = janus.tidy_artifacts(cfg, [{"name": "open1", "cwd": str(ow)}])
        self.assertEqual([(k, t) for k, t, _, _ in P], [("artifact", str(cw / "target"))])   # never the open unit's tree
        rc, e = janus.tidy_do(cfg, "artifact", str(cw / "target"))
        self.assertEqual(rc, 0, e); self.assertFalse((cw / "target").exists())


if __name__ == "__main__":
    unittest.main()
