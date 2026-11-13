"""skill-fix56: the known skill issues fixed at the root (project ISSUES rows 83, 93, 94, 59, 133 and the nobrief count).
One test per failure, each naming what it catches. Standard library only; no network, no spend. Run by ./check."""
import argparse, contextlib, io, re, subprocess, time, unittest
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


class Intents(unittest.TestCase):
    def test_an_intent_without_a_moved_time_counts_from_its_issue_dates(self):
        """Catches (row 94): an intent with no t in its notes (made by bd create, or notes edited by hand) counted from epoch 0:
        'has not moved for 498494 h'."""
        d, cfg = project(); iso = lambda h: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - h * 3600))
        iss = [{"id": "i1", "title": "intent: by hand", "updated_at": iso(5), "created_at": iso(9)}, {"id": "i2", "title": "intent: fresh", "notes": '{"t": 0}', "created_at": iso(1)}]
        with mock.patch.object(janus, "Beads", lambda cfg: mock.Mock(issues=lambda *a: iss)):
            F = dict(janus.intent_findings(cfg, []))
        self.assertEqual(list(F), ["intent:i1:stale"]); self.assertIn("not moved for 5.0 h", F["intent:i1:stale"])


class OrchestratorCard(unittest.TestCase):
    def test_the_orchestrators_own_card_is_never_silent(self):
        """Catches (row 59): the orchestrator's status post made a card that was judged like an agent's, so 30 min later check
        told the cycling orchestrator that 'orchestrator' was silent, again after every ack."""
        d, cfg = project(); cfg.run.ro = False; janus.board_upkeep(cfg, []); bd = d / "board"
        for n in ("orchestrator", "a1"):
            janus.board(cfg, "post", n, "status", "cycle"); c = bd / f"agents/{n}.md"; c.write_text(re.sub(r"- updated: .*", "- updated: 2000-01-01 00:00", c.read_text()))
        self.assertEqual({n: r["silent"] for n, r in janus.board_rows(cfg).items()}, {"orchestrator": False, "a1": True})
        silent = lambda cfg: {"orchestrator": {"state": "working", "silent": True, "gone": False, "text": ""}}   # a project's own board script
        with mock.patch.object(janus, "board_rows", silent): self.assertEqual(janus.card_findings(cfg, set()), [])
        self.assertEqual([k for k, _ in janus.card_findings(cfg, {"a1"})], ["board:a1:silent"])


if __name__ == "__main__":
    unittest.main()
