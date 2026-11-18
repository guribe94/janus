"""Rigor levels (janus/rigor.py): one setting from `ship` (write and run nothing, land on the report) to `thorough` (edge-case
tests, the full suite, several reviews), read by every place that acts on it.

The failures caught: (owner 2026-11-17) "land fast, minimal tests" was asked many times and not followed, because it lived as prose
in several places pulling against each other, and agents wrote many tests and ran whole suites at checkpoints; the opposite owner
wish (reviews before landing) had no mechanism at all (a review's --of was recorded and never read). Standard library only.

Run by ./check (it discovers tests/test_*.py)."""
import contextlib, io, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from janusload import conf, load

janus = load()
from janus.lane import main as lane_main


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    conf(d).write_text('[project]\nname = "t"\n[paths]\nworktrees = "wt"\n[hosts.here]\nlocal = true\n' + extra)
    (d / ".janus" / "out").mkdir(); (d / "wt").mkdir()
    return d, janus.Cfg(d)


class Levels(unittest.TestCase):
    def test_the_level_comes_from_the_unit_then_the_project_then_lean(self):
        """Catches: no level when nothing is set (the strict default is lean), a unit's RIGOR:/--rigor ignored, a typo silently
        meaning the default."""
        d, cfg = project()
        self.assertEqual(janus.project_level(cfg), "lean"); self.assertEqual(janus.level_of(cfg, {"name": "u"}), "lean")
        d, cfg = project('\n[rigor]\nlevel = "careful"\n')
        self.assertEqual(janus.level_of(cfg, {"name": "u"}), "careful"); self.assertEqual(janus.level_of(cfg, {"name": "u", "rigor": "ship"}), "ship")
        d, cfg = project('\n[rigor]\nlevel = "fastest"\n')
        with self.assertRaises(janus.Err) as e: janus.project_level(cfg)
        self.assertIn("ship, quick, lean, careful, thorough", str(e.exception))

    def test_each_level_tells_the_agent_what_to_write_and_run(self):
        """Catches: every unit told the same thing whatever the owner set; lean without its anti-bloat rules."""
        T = {lv: janus.report_block("R.md", lv) for lv in janus.LEVELS}
        self.assertIn("write no tests", T["ship"]); self.assertIn("run no tests", T["ship"])
        self.assertIn("compile or typecheck", T["quick"])
        for s in ("no existing test would fail", "remove the tests your change makes obsolete", "ONE targeted check", "no suites"): self.assertIn(s, T["lean"])
        self.assertIn("each behavior you changed", T["careful"]); self.assertIn("full suite", T["thorough"])
        self.assertTrue(all("report.json" in t and "Rigor:" in t for t in T.values()))
        d, cfg = project('\n[rigor]\nlevel = "careful"\n')
        self.assertIn("Rigor: careful", janus.session_start_text(cfg))


class StopHook(unittest.TestCase):
    def test_only_ship_ends_on_done_without_exit_codes(self):
        """Catches: a ship unit (nothing run) blocked forever for exit codes it cannot have; any other level losing that check."""
        rp = Path(tempfile.mkdtemp(prefix="janus-test-")) / "REPORT.md"; rp.write_text("VERDICT: DONE\nimplemented\n")
        def stop(level):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"JANUS_REPORT": str(rp), "JANUS_RIGOR": level}), contextlib.redirect_stdout(out): janus.agent_hook("Stop", {})
            return out.getvalue()
        self.assertEqual(stop("ship"), "")
        self.assertIn('"decision": "block"', stop("lean"))


class Broad(unittest.TestCase):
    def lane(self, policy, *args):
        d = Path(tempfile.mkdtemp(prefix="janus-test-")); stub = d / "pueue"; stub.write_text('#!/bin/sh\ncase "$1" in add) echo 7;; esac\n'); stub.chmod(0o755)
        c = {"host": "far", "unit": "u", "wt": str(d), "label": "unit:u", "rd": str(d / "runs"), "pueue": str(stub), "ensure": "true",
             "script": "true # __JID__", "script_broad": "true # __JID__", "patterns": janus.BROAD_PATTERNS, "broad_min": 30,
             "broad_policy": policy, "rigor": {"refuse": "lean", "reason": "careful", "free": "thorough"}[policy]}
        cf = d / "u.lane.json"; cf.write_text(json.dumps(c)); err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err): rc = lane_main([str(cf), "--detach", *args])
        return rc, err.getvalue()

    def test_a_whole_suite_by_level(self):
        """Catches: an agent at lean running a whole suite at a checkpoint with any --broad reason; thorough still asking for one."""
        rc, e = self.lane("refuse", "--broad", "checkpoint", "cargo test --workspace"); self.assertEqual(rc, 2); self.assertIn("not run at rigor lean", e)
        self.assertEqual(self.lane("refuse", "cargo test -p one")[0], 0)                    # the one targeted check still runs
        self.assertEqual(self.lane("reason", "cargo test --workspace")[0], 2)               # careful: today's rule
        self.assertEqual(self.lane("reason", "--broad", "release", "cargo test --workspace")[0], 0)
        self.assertEqual(self.lane("free", "cargo test --workspace")[0], 0)                 # thorough: no reason needed


class Reviews(unittest.TestCase):
    def setUp(self):
        self.real = janus.all_units
        self.d, self.cfg = project('\n[rigor]\nlevel = "careful"\n')

    def tearDown(self):
        janus.all_units = self.real

    def review(self, name, of, verdict="VERDICT: DONE", vendor="anthropic"):
        (self.d / f".janus/out/{name}").mkdir(parents=True, exist_ok=True); (self.d / f".janus/out/{name}/REPORT.md").write_text(verdict + "\n")
        return {"name": name, "kind": "review", "of": of, "vendor": vendor, "status": "closed"}

    def test_reviews_a_level_requires_before_landing(self):
        """Catches: no level ever requiring a review (the --of link unread); a review still running or blocked counted; a critical
        unit at thorough reviewed only by its own vendor; lean or a normal unit at careful asked for reviews."""
        u = {"name": "w", "kind": "implement", "tier": "critical", "vendor": "anthropic"}
        janus.all_units = lambda c: {}
        self.assertIn("needs 1 DONE review(s)", janus.review_gap(self.cfg, u))
        self.assertEqual(janus.review_gap(self.cfg, {**u, "tier": "normal"}), "")
        self.assertEqual(janus.review_gap(self.cfg, {**u, "rigor": "lean"}), "")
        janus.all_units = lambda c: {"r1": self.review("r1", "w", "VERDICT: PENDING")}
        self.assertIn("has 0", janus.review_gap(self.cfg, u))
        janus.all_units = lambda c: {"r1": self.review("r1", "w")}
        self.assertEqual(janus.review_gap(self.cfg, u), "")
        th = {**u, "rigor": "thorough"}; janus.all_units = lambda c: {"r1": self.review("r1", "w"), "r2": self.review("r2", "w")}
        self.assertIn("vendor other than anthropic", janus.review_gap(self.cfg, th))
        janus.all_units = lambda c: {"r1": self.review("r1", "w"), "r2": self.review("r2", "w", vendor="openai")}
        self.assertEqual(janus.review_gap(self.cfg, th), "")
        self.assertTrue(janus.FAMILIES.get("review"))

    def test_land_refuses_until_reviewed_unless_forced(self):
        """Catches: janus land ignoring the level; no way past it when the owner decides; lean changed."""
        u = {"id": "u-w", "name": "w", "kind": "implement", "tier": "critical", "vendor": "anthropic", "cwd": str(self.d / "wt/none")}
        janus.all_units = lambda c: {"w": u}
        B = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u]})
        a = lambda **k: type("A", (), {"unit": "w", "push": False, "host": None, "dry_run": True, "force_review": False, **k})()
        with mock.patch.object(janus, "Beads", B):
            with self.assertRaises(janus.Err) as e: janus.cmd_land(self.cfg, a())
            self.assertIn("--force-review", str(e.exception))
            with self.assertRaises(janus.Err) as e: janus.cmd_land(self.cfg, a(force_review=True))
            self.assertIn("no worktree of its own", str(e.exception))                     # past the gate, at the next check
            u["rigor"] = "lean"
            with self.assertRaises(janus.Err) as e: janus.cmd_land(self.cfg, a())
            self.assertIn("no worktree of its own", str(e.exception))


if __name__ == "__main__":
    unittest.main()
