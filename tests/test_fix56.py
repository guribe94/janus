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


class RecurringAcks(unittest.TestCase):
    def test_the_detector_never_feeds_on_its_own_findings_or_on_a_hold(self):
        """Catches (row 93 and its meta rows): acks of issue:<key> raised issue:issue:<key> and a 'keeps coming back' row for
        it; issue:<key> stayed after its row was written; and every re-ack of a finding that never cleared (a finished unit
        held open for a chain) counted as a recurrence."""
        d, cfg = project("\n[janus]\nack_issue_n = 3\n"); ack = lambda k: janus.cmd_ack(cfg, argparse.Namespace(problem=k, decision="held for the chain", decision_class=False))
        with mock.patch.object(janus, "Beads", mock.Mock(side_effect=janus.Err("no beads"))), contextlib.redirect_stdout(io.StringIO()):
            for _ in range(4): ack("unit:u:finished")            # the finding stands all along: one episode, one count
            self.assertEqual(cfg.jget("ack-counts.json"), {"unit:u:finished": 1})
            janus.held_after(cfg, [], {"here": {"ok": True}}); ack("unit:u:finished")   # it cleared and came back: a recurrence
            self.assertEqual(cfg.jget("ack-counts.json"), {"unit:u:finished": 2})
        cfg.jput("ack-counts.json", {"overlap:a:b": 3, "issue:overlap:a:b": 5})
        self.assertEqual([k for k, _ in janus.repeated_issue_findings(cfg)], ["issue:overlap:a:b"])
        janus.auto_log_issues(cfg); self.assertNotIn("[issue:", janus.issue_path(cfg).read_text())
        self.assertEqual(janus.repeated_issue_findings(cfg), [])   # its row exists: not raised again


if __name__ == "__main__":
    unittest.main()
