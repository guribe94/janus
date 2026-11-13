"""skill-fix50 (ISSUES row 244): one finding of an unregistered family crashed every janus check. Each test names the failure it
catches. Standard library only; no network, no spend. Run by ./check (it discovers tests/test_*.py)."""
import ast, re, unittest

import janusload
from test_janus import janus

ALERT_KEYS = {"q", "b"}   # board_alerts' told-once keys (q:<n>, b:<card>:<hash>): the same (key, text) shape, never findings


def key_head(e):
    """The literal start of a string or f-string expression; None for anything else."""
    if isinstance(e, ast.Constant) and isinstance(e.value, str): return e.value
    if isinstance(e, ast.JoinedStr) and e.values and isinstance(e.values[0], ast.Constant): return e.values[0].value


class FindingFamilies(unittest.TestCase):
    def test_every_key_the_code_can_emit_has_a_registered_family(self):
        """Catches row 244: lane_findings emitted `blocked:<host>:<lane>` and no family `blocked` was registered, so check died
        with a ValueError on every pass while a job waited in a blocked admission. The tests of that finding read the raw
        (key, text) pair, so only a live check met the registry. Here every (key, text) pair in the package is read from the
        source: a pair whose key starts `<family>:` must name a registered family."""
        seen = {}
        for p in sorted((janusload.JANUS_PY.parent / "janus").rglob("*.py")):
            for n in ast.walk(ast.parse(p.read_text())):
                m = isinstance(n, ast.Tuple) and len(n.elts) == 2 and re.match(r"([a-z][a-z0-9-]*):", key_head(n.elts[0]) or "")
                if m: seen.setdefault(m.group(1), f"{p.name}:{n.lineno}")
        self.assertGreater(len(seen), 40, "the scan found too few finding keys: it no longer reads the package")
        self.assertEqual({f: w for f, w in seen.items() if f not in janus.FAMILIES and f not in ALERT_KEYS}, {},
                         "finding keys of families missing from FAMILIES (janus/findings/__init__.py)")

    def test_a_key_of_an_unregistered_family_is_reported_and_does_not_end_the_check(self):
        """Catches the other half of row 244: one bad key raised in collect_findings, so the pass showed no finding at all (and
        the watcher's wake failed). It must come out as its own urgent finding, with the original key and text kept."""
        f = janus.finding("no-such-family:here:jobs", "2 jobs wait")
        self.assertEqual(f.key, "unregistered:no-such-family:here:jobs")
        self.assertIn("2 jobs wait", f.text)
        self.assertTrue(f.data()["urgent"], "a finding janus cannot classify must not be quiet")


if __name__ == "__main__":
    unittest.main()
