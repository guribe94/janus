"""State files survive a crash mid-write (section 7, Step 0 / seam 2).

Cfg.jput writes straight to the file today (orch.py:52-53), so a kill during the write leaves truncated JSON and jget silently
returns {} — every ack, open problem, the 3-strike Stop counter and learned.json reset without a word. This test injects a crash
into the write of a new value and asserts the previous value is still on disk. It is an expected failure until jput writes a temp
file and renames it (the writer-lock/atomic-state seam); then it flips to a real guard. Standard library only; no network."""
import importlib.util, json, os, tempfile, textwrap, unittest
from pathlib import Path
from unittest import mock

ORCH_PY = Path(os.environ.get("ORCH_PY") or Path(__file__).resolve().parent.parent / "orch.py")
spec = importlib.util.spec_from_file_location("orch", ORCH_PY); orch = importlib.util.module_from_spec(spec); spec.loader.exec_module(orch)


def project():
    d = Path(tempfile.mkdtemp(prefix="orch-test-"))
    (d / "orch.toml").write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        '''))
    return d, orch.Cfg(d)


class State(unittest.TestCase):
    @unittest.expectedFailure
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


if __name__ == "__main__":
    unittest.main()
