"""janus issue: log a problem met with the skill, and the checks around it.

The failure caught: the owner rule "log every oddity as an ISSUES row" lived only in docs, so a problem an agent met was
remembered or lost; and an acked finding vanished with no view of what the ack hid, and a key that kept coming back was never
turned into an issue row. Standard library only; no network.

Run by ./check (it discovers tests/test_*.py)."""
import tempfile, unittest
from pathlib import Path

from janusload import load

janus = load()


def project(extra="", issues=True, ack_n=None):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    jan = "[janus]\n" + (f'issues_file = "{d}/ISSUES.md"\n' if issues else "") + (f"ack_issue_n = {ack_n}\n" if ack_n else "")
    (d / "janus.toml").write_text('[project]\nname = "t"\n[hosts.here]\nlocal = true\n' + jan + extra)
    (d / "out").mkdir(); (d / "briefs").mkdir()
    return d, janus.Cfg(d)


class Issue(unittest.TestCase):
    def test_issue_appends_a_numbered_row_once_per_key(self):
        d, cfg = project()
        rc, L = janus.cmd_issue(cfg, type("A", (), {"text": "a job script changed in place", "evidence": "mac:12", "key": None})())
        self.assertEqual(rc, 0, L)
        p = d / "ISSUES.md"
        self.assertTrue(p.is_file()); self.assertIn("| 1 | a job script changed in place | mac:12 |", p.read_text())
        janus.cmd_issue(cfg, type("A", (), {"text": "second", "evidence": "-", "key": None})())
        self.assertIn("| 2 | second | - |", p.read_text())
        janus.cmd_issue(cfg, type("A", (), {"text": "recurring", "evidence": "x", "key": "waste:u"})())
        janus.cmd_issue(cfg, type("A", (), {"text": "recurring again", "evidence": "y", "key": "waste:u"})())
        self.assertEqual(p.read_text().count("[waste:u]"), 1, p.read_text())   # idempotent by key: one row, not two

    def test_issue_refuses_without_a_register(self):
        d, cfg = project(issues=False)
        with self.assertRaises(janus.Err) as e: janus.cmd_issue(cfg, type("A", (), {"text": "x", "evidence": None, "key": None})())
        self.assertIn("issues_file", str(e.exception))

    def test_repeated_acks_earn_an_issue_row(self):
        d, cfg = project(ack_n=3)
        self.assertEqual(janus.repeated_issue_findings(cfg), [])
        cfg.jput("ack-counts.json", {"waste:u": 2})
        self.assertEqual(janus.repeated_issue_findings(cfg), [])
        cfg.jput("ack-counts.json", {"waste:u": 3})
        self.assertEqual([k for k, _ in janus.repeated_issue_findings(cfg)], ["issue:waste:u"])
        janus.auto_log_issues(cfg)                              # as a writer check does: the row is written once
        self.assertIn("[waste:u]", (d / "ISSUES.md").read_text())
        janus.auto_log_issues(cfg)                              # a second pass writes no second row
        self.assertEqual((d / "ISSUES.md").read_text().count("[waste:u]"), 1)


class AckedView(unittest.TestCase):
    def test_acked_lists_what_a_live_ack_hides(self):
        d, cfg = project()
        cfg.jput("acks.json", {"waste:u": {"t": janus.now(), "d": "resumed it with a larger budget"}})
        rc, L = janus.cmd_check(cfg, type("A", (), {"acked": True})())
        self.assertEqual(rc, 0)
        self.assertTrue(any(k.startswith("[waste:u]") for k in L), L)
        self.assertTrue(any("resumed it with a larger budget" in k for k in L), L)

    def test_ack_refuses_a_future_tense_plan_for_idle_or_landing(self):
        d, cfg = project()
        a = type("A", (), {"problem": "landing:u:unlanded", "decision": "I will land it tomorrow", "decision_class": False})()
        with self.assertRaises(janus.Err) as e: janus.cmd_ack(cfg, a)
        self.assertIn("what was DONE", str(e.exception))


if __name__ == "__main__":
    unittest.main()
