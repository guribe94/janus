"""State files survive a crash mid-write (section 7, Step 0 / seam 2).

Cfg.jput writes straight to the file today (janus.py:52-53), so a kill during the write leaves truncated JSON and jget silently
returns {} — every ack, open problem, the 3-strike Stop counter and learned.json reset without a word. This test injects a crash
into the write of a new value and asserts the previous value is still on disk (jput writes a temp file and renames it). The
writer test pins the one writer lock: a second writer process waits, then is refused. Standard library only; no network."""
import json, os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path
from unittest import mock

from janusload import load

janus = load()


def project():
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    (d / "janus.toml").write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        '''))
    return d, janus.Cfg(d)


class State(unittest.TestCase):
    def test_jput_keeps_the_old_value_when_the_write_dies(self):
        d, cfg = project()
        cfg.jput("x.json", {"a": 1})
        p = cfg.f("x.json")
        real = Path.write_text

        def crash(self, data, *a, **k):   # the write of the NEW value dies after a partial write
            if data == '{"a": 2}':
                real(self, '{"a": 2')
                raise OSError("killed mid-write")
            return real(self, data, *a, **k)

        with mock.patch.object(Path, "write_text", crash):
            with self.assertRaises(OSError):
                cfg.jput("x.json", {"a": 2})
        self.assertEqual(json.loads(p.read_text()), {"a": 1}, "a crash mid-write left the state file truncated")
        self.assertEqual([x.name for x in p.parent.iterdir() if x.name.endswith(".tmp")], [], "the dead write left its temp file")

    def test_one_writer_at_a_time(self):
        """Catches: the watcher and the orchestrator's check (or a command) mutating state at once (double resume, lost acks)."""
        d, cfg = project()
        with cfg.writer():
            with cfg.writer(): pass   # re-entrant: the watcher's pass runs the writer check inside its own lock
            code = ("import sys; sys.path.insert(0, %r); from janus.context import Cfg, Err; from pathlib import Path\n"
                    "try:\n with Cfg(Path(%r)).writer(wait=0.5): print('got it')\nexcept Err as e: print('refused', e)") % (str(Path(janus.__file__).parent), str(d))
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
            self.assertIn("refused", out, "a second writer took the state lock while the first held it")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
        self.assertIn("got it", out, "the lock was not released")


class UnitRecord(unittest.TestCase):
    def test_v1_record_migrates_to_v2(self):
        """A v1 record (launch wrote only run; a resume added runs) reads as v2: every run id in runs, the current one last."""
        r = lambda notes: janus.unit_record({"notes": json.dumps(notes) if isinstance(notes, dict) else notes})
        self.assertEqual(r({"run": "u.1"}), {"run": "u.1", "runs": ["u.1"], "v": 2})
        self.assertEqual(r({"run": "u.r2.9", "runs": ["u.1"]})["runs"], ["u.1", "u.r2.9"])
        self.assertEqual(r({"v": 2, "run": "x", "runs": ["x"], "host": "h"}), {"v": 2, "run": "x", "runs": ["x"], "host": "h"})
        self.assertEqual(r("not json"), {})


if __name__ == "__main__":
    unittest.main()
