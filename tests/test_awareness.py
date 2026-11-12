"""imp-awareness: agents aware of each other only when needed.

What each test catches (they fail before the change: an AttributeError, or a direction/finding that never appears):
- janus introduce posts, to BOTH agents, the other unit's report path and its exact `janus transcript <unit> --grep` command;
- a RELATED: line and a predecessor (--resume-of, or a name that extends another's) reach the brief header the same way;
- related:<a>:<b> fires when two running units change the same module, and its fix is the `janus introduce` command;
- board.sh note writes onto another agent's card and check lists it (note:<agent>:<n>);
- the autopilot introduces the same-module pair once (idempotent), exposing the function the watcher may call.
Standard library only; no network, no API spend."""
import os, subprocess, tempfile, textwrap, unittest
from pathlib import Path

from janusload import load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-aware-"))
    (d / "janus.toml").write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        ''') + extra)
    (d / "out").mkdir(); (d / "briefs").mkdir()
    return d, janus.Cfg(d)


def demo_units(d, pairs):
    """Demo units, each a real worktree that has changed the given files (so unit_changes sees them)."""
    g = lambda *a, cwd: subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, check=True)
    repo = d / "repo"; repo.mkdir(exist_ok=True)
    if not (repo / ".git").exists():
        g("init", "-q", "-b", "main", cwd=repo)
        (repo / "README").write_text("x\n"); g("add", ".", cwd=repo)
        g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=repo)
    out = []
    for name, files in pairs:
        w = d / name
        if not w.exists(): g("worktree", "add", "-q", "-b", name, str(w), cwd=repo)
        for f in files:
            (w / f).parent.mkdir(parents=True, exist_ok=True); (w / f).write_text("changed\n")
        out.append({"id": name, "name": name, "host": "here", "model": "m", "cwd": str(w), "state": "working",
                    "started": 0, "report": str(d / "out" / name / "REPORT.md")})
    return out


class Introduce(unittest.TestCase):
    """janus introduce <a> <b> --why: a direction on both cards, each naming the other and how to read it."""
    def setUp(self):
        self.d, self.cfg = project(); self.cfg.run.ro = False; self.cfg.run.all_units = {}
        janus.board_upkeep(self.cfg, []); (janus.board_dir(self.cfg) / ".notify").write_text("true\n")
        self.units = demo_units(self.d, [("alpha", ["pkg/a.py"]), ("beta", ["pkg/b.py"])])
        self.real = janus.Beads.units; janus.Beads.units = lambda s: [dict(u) for u in self.units]

    def tearDown(self): janus.Beads.units = self.real

    def test_direction_names_report_and_transcript_command(self):
        a = type("A", (), {"a": "alpha", "b": "beta", "why": "both change pkg/x.py", "topic": None})()
        self.cfg.run.dry = True
        txt = " ".join(janus.cmd_introduce(self.cfg, a)[1])
        self.assertIn("beta", txt); self.assertIn("beta (host here", txt)
        self.assertIn(str(self.d / "out/beta/REPORT.md"), txt)
        self.assertIn("janus transcript beta --grep", txt)
        self.assertFalse((janus.board_dir(self.cfg) / "to/alpha.md").exists())   # dry run writes nothing
        self.cfg.run.dry = False
        janus.cmd_introduce(self.cfg, a)
        self.assertIn("janus transcript beta --grep", (janus.board_dir(self.cfg) / "to/alpha.md").read_text())
        self.assertIn("janus transcript alpha --grep", (janus.board_dir(self.cfg) / "to/beta.md").read_text())

    def test_topic_defaults_to_the_shared_path(self):
        self.assertEqual(janus.introduce_topic("both change pkg/x.py", "beta"), "pkg/x.py")
        self.assertEqual(janus.introduce_topic("they collide somehow", "beta"), "beta")


class BriefPointers(unittest.TestCase):
    """RELATED: and a predecessor reach the brief header the agent reads."""
    def setUp(self):
        self.d, self.cfg = project()
        self.real = janus.Beads.units; janus.Beads.units = lambda s: []
        self.cfg.run.all_units = {"alpha": {"name": "alpha", "host": "here", "model": "m"},
                                  "orig": {"name": "orig", "host": "here", "model": "m", "report": str(self.d / "out/orig/REPORT.md")}}

    def tearDown(self): janus.Beads.units = self.real

    def test_related_and_predecessor_in_the_header(self):
        hdr = janus.brief_header(self.cfg, "here", self.cfg.hosts["here"], "new-unit", str(self.d), True, None,
                                 str(self.d / "out/new-unit/REPORT.md"), brief_text="do it\nRELATED: alpha\n", pred="orig")
        self.assertIn(str(self.d / "out/alpha/REPORT.md"), hdr)
        self.assertIn("janus transcript alpha --grep", hdr)
        self.assertIn(str(self.d / "out/orig/REPORT.md"), hdr)
        self.assertIn("janus transcript orig --grep", hdr)
        self.assertIn("your predecessor", hdr)

    def test_predecessor_from_name_extension_or_resume_of(self):
        self.assertEqual(janus.predecessor_name(self.cfg, "alpha-finish"), "alpha")
        self.assertEqual(janus.predecessor_name(self.cfg, "alpha-finish", "orig"), "orig")
        self.assertIsNone(janus.predecessor_name(self.cfg, "solo"))
        self.cfg.run.all_units["a74"] = {"name": "a74"}
        self.assertEqual(janus.predecessor_name(self.cfg, "a74-holdout-x"), "a74")   # the longest match wins


class Related(unittest.TestCase):
    """related:<a>:<b> when two running units change the same module, with the introduce fix; never for the same unit."""
    def setUp(self):
        self.d, self.cfg = project(); self.cfg.run.ro = True; self.cfg.run.all_units = {}
        self.units = demo_units(self.d, [("alpha", ["pkg/a.py"]), ("beta", ["pkg/b.py", "other/c.py"])])
        self.bd = janus.board_dir(self.cfg); self.bd.mkdir()
        self.real = janus.Beads.units; janus.Beads.units = lambda s: [dict(u) for u in self.units]

    def tearDown(self): janus.Beads.units = self.real

    def test_same_module_is_a_related_finding_with_the_fix(self):
        F = dict(janus.related_findings(self.cfg, self.units, {}))
        self.assertIn("related:alpha:beta", F)
        self.assertIn("janus introduce alpha beta", F["related:alpha:beta"])
        self.assertIn("pkg", F["related:alpha:beta"])

    def test_different_modules_are_not_related(self):
        units = demo_units(self.d, [("one", ["pkg/a.py"]), ("two", ["other/c.py"])])
        self.assertEqual([k for k, _ in janus.related_findings(self.cfg, units, {})], [])

    def test_learned_post_naming_an_area_is_related(self):
        (self.bd / "log.md").write_text("2026-01-01 00:00 alpha learned: pkg is shared ground\n")
        F = dict(janus.related_findings(self.cfg, self.units, {}))
        self.assertIn("janus introduce alpha beta", F["related:alpha:beta"])

    def test_introduced_pair_does_not_fire_again(self):
        self.cfg.run.ro = False; janus.board_upkeep(self.cfg, []); (self.bd / ".notify").write_text("true\n")
        janus.introduce(self.cfg, "alpha", "beta", "both change the module pkg")
        self.assertEqual([k for k, _ in janus.related_findings(self.cfg, self.units, {})], [])

    def test_a_learned_post_from_a_closed_unit_is_not_related(self):
        """Catches (item 17): related:<a>:<b> fired for a pair where one unit is closed (a learned post from a closed unit
        named a running unit's area)."""
        both = demo_units(self.d, [("alpha", ["pkg/a.py"]), ("gone", ["pkg/b.py"])])
        units = [u for u in both if u["name"] == "alpha"]
        janus.Beads.units = lambda s: [dict(u) for u in units]
        self.cfg.run.all_units = {"gone": {**both[1], "status": "closed"}}
        (self.bd / "log.md").write_text("2026-01-01 00:00 gone learned: pkg is shared ground\n")
        F = dict(janus.related_findings(self.cfg, units, {}))
        self.assertEqual(list(F), [], f"a closed unit was called related: {F}")


class Notes(unittest.TestCase):
    """board.sh note: an agent writes onto another card; check lists it; a bad name or missing author is refused."""
    def setUp(self):
        self.d, self.cfg = project(); self.cfg.run.ro = False; self.cfg.run.all_units = {}
        janus.board_upkeep(self.cfg, []); (janus.board_dir(self.cfg) / ".notify").write_text("true\n")
        janus.board(self.cfg, "post", "alpha", "status", "start")

    def test_note_lands_on_the_other_card_and_check_sees_it(self):
        rc, o, e = janus.board(self.cfg, "note", "alpha", "pkg/a.py: I am editing this", env={"JANUS_UNIT": "beta"})
        self.assertEqual(rc, 0, e)
        card = (janus.board_dir(self.cfg) / "agents/alpha.md").read_text()
        self.assertIn("pkg/a.py: I am editing this", card); self.assertIn("beta", card)
        self.cfg.run.ro = True
        F = dict(janus.note_findings(self.cfg))
        self.assertEqual(list(F), ["note:alpha:1"])
        self.assertIn("pkg/a.py", F["note:alpha:1"])
        self.assertTrue(janus.ackable("note:alpha:1")); self.assertFalse(janus.FAMILIES["note"].urgent)

    def test_bad_target_and_missing_author_are_refused(self):
        rc, o, e = janus.board(self.cfg, "note", "../evil", "x", env={"JANUS_UNIT": "beta"})
        self.assertEqual(rc, 2); self.assertIn("bad agent name", e)
        rc, o, e = janus.board(self.cfg, "note", "alpha", "x", env={"JANUS_UNIT": ""})
        self.assertEqual(rc, 2); self.assertIn("note <other-agent>", e)


class Autopilot(unittest.TestCase):
    """The watcher's function (not wired here): it introduces the same-module pair itself, once."""
    def setUp(self):
        self.d, self.cfg = project(); self.cfg.run.ro = False; self.cfg.run.all_units = {}
        janus.board_upkeep(self.cfg, []); (janus.board_dir(self.cfg) / ".notify").write_text("true\n")
        self.units = demo_units(self.d, [("alpha", ["pkg/a.py"]), ("beta", ["pkg/b.py"])])
        self.real = janus.Beads.units; janus.Beads.units = lambda s: [dict(u) for u in self.units]

    def tearDown(self): janus.Beads.units = self.real

    def test_autopilot_introduces_the_same_module_pair_once(self):
        log = []
        self.assertEqual(janus.tend_related(self.cfg, {}, self.units, log.append), ["alpha:beta"])
        self.assertIn("janus transcript beta --grep", (janus.board_dir(self.cfg) / "to/alpha.md").read_text())
        self.assertEqual(janus.tend_related(self.cfg, {}, self.units, log.append), [])   # introduced: no second direction


if __name__ == "__main__":
    unittest.main()
