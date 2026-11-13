"""skill-fix56: the known skill issues fixed at the root (project ISSUES rows 83, 93, 94, 59, 133 and the nobrief count).
One test per failure, each naming what it catches. Standard library only; no network, no spend. Run by ./check."""
import argparse, contextlib, io, subprocess, time, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project


class Fill(unittest.TestCase):
    def test_fill_launches_each_unit_in_a_new_worktree(self):
        """Catches (row 83): fill launched with no tree, so the unit got the project root (a main checkout) and launch
        refused it; every fill launch needed a manual janus launch --new-worktree <repo>."""
        d, cfg = project('\n[paths]\nworktrees = "wts"\n'); launched = []
        subprocess.run(["git", "-C", str(d), "init", "-q"], check=True, capture_output=True)
        b = d / "briefs/x1-a.md"; b.write_text("# x1-a\nITEM: X1\n"); b2 = d / "briefs/x2-a.md"; b2.write_text("# x2-a\nITEM: X2\nREPO: ../code\n")
        b3 = d / "briefs/x3-a.md"; b3.write_text("# x3-a\nITEM: X3\n"); (d / "wts/x3-a").mkdir(parents=True)
        plan = [(n, i, "m", "here", "here", 1, "why", "implement", "normal", f) for n, i, f in (("x1-a", "X1", b), ("x2-a", "X2", b2), ("x3-a", "X3", b3))]
        with mock.patch.object(janus, "probe_all", lambda cfg: {}), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "Beads", lambda cfg: mock.Mock(units=lambda: [])), mock.patch.object(janus, "waste_block", lambda cfg: None), \
             mock.patch.object(janus, "fill_plan", lambda *a: (plan, [])), mock.patch.object(janus, "cmd_launch", lambda cfg, a: launched.append(a)), \
             contextlib.redirect_stdout(io.StringIO()):
            janus.cmd_fill(cfg, argparse.Namespace(max=None))
        got = [getattr(a, "new_worktree", "unset") for a in launched]
        self.assertEqual(got, [str(d.resolve()), str((d / "../code").resolve()), None])   # the root's repo; the brief's REPO:; its tree exists


if __name__ == "__main__":
    unittest.main()
