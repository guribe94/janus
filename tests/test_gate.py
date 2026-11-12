"""Gate policy: the baseline job and the failure classification.

The failure caught: a failure the landing branch itself shows on the same host was charged to the unit, so a unit's gate failed
on main's failures (decision 0002). `janus gate --baseline <name>` records a run of the landing branch on the same host, and
gate-judge treats its failures as the branch's, not the unit's. Standard library only.

Run by ./check (it discovers tests/test_*.py)."""
import tempfile, unittest
from pathlib import Path

from janusload import load

janus = load()


def project():
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    (d / "janus.toml").write_text('[project]\nname = "t"\n[hosts.here]\nlocal = true\n')
    (d / "out").mkdir()
    return d, janus.Cfg(d)


class GateBaseline(unittest.TestCase):
    def test_baseline_failures_are_the_branchs_not_the_units(self):
        d, cfg = project()
        (d / "out/base").mkdir(); (d / "out/base/failed.txt").write_text("a\n")          # the landing branch's own run
        (d / "out/g").mkdir(); (d / "out/g/failed.txt").write_text("a\nc\n")
        self.assertEqual(janus.judge_gate(cfg, "g", 1), ("FAIL", ["a", "c"], []))        # no baseline: both charged to the unit
        (d / "out/g/baseline.txt").write_text("base\n")
        self.assertEqual(janus.gate_baseline(cfg, "g"), "base")
        self.assertEqual(janus.judge_gate(cfg, "g", 1), ("FAIL", ["c"], ["a"]))          # 'a' is the baseline's: only 'c' is the unit's
        (d / "out/g/failed.txt").write_text("a\n")
        self.assertEqual(janus.judge_gate(cfg, "g", 1)[0], "PASS")                       # every failure is the baseline's
        self.assertEqual(janus.judge_gate(cfg, "g", 0)[0], "PASS")                       # a clean run still passes

    def test_cmd_gate_records_and_validates_the_baseline(self):
        d, cfg = project()
        (d / "out/g").mkdir(); (d / "out/g/tests.sh").write_text("#!/bin/sh\nexit 0\n")
        a = type("A", (), {"name": "g", "baseline": "missing-run", "dry_run": True})()
        with self.assertRaises(janus.Err) as e: janus.cmd_gate(cfg, a)
        self.assertIn("no recorded gate", str(e.exception))


if __name__ == "__main__":
    unittest.main()
