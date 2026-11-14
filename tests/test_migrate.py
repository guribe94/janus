"""janus migrate: the one-time move from the old nested root to the workspace's .janus. Catches: identity, messages, instructions or
the registry lost in the move; a move while work is queued or a host cannot say; the old root left active."""
import os, tempfile, unittest
from pathlib import Path
from unittest import mock

from janusload import load

janus = load()


class FakeBeads:
    units_ = [{"name": "u1", "id": "b1", "via": "pueue", "host": "here", "state": "done"}]
    def __init__(self, cfg): pass
    def units(self): return list(self.units_)
    def issues(self, label, status="open"):
        return [{"id": "x9", "title": "instr: never touch prod", "description": "Never touch the production database; ask first."}] if label == "instruction" else []


def probe_ok(tasks=None, pueue=True):
    return lambda n, h, **k: {"name": n, "ok": True, "pueue": {"tasks": tasks or {}} if pueue else None}


class Migrate(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="janus-mig-")); self.old = self.ws / "gate-logs" / "orch-1"; self.old.mkdir(parents=True)
        (self.old / "janus.toml").write_text('[project]\nname = "rig"\nbeads = "."\n[paths]\nbriefs = "briefs"\nout = "out"\n[hosts.here]\nlocal = true\n')
        for d in ("briefs", "out", "board"): (self.old / d).mkdir()
        (self.old / "board/log.md").write_text("2026-11-13 10:00 ann question: [q1] which one?\n"); (self.old / "out/r.md").write_text("result\n")
        (self.old / ".janus").mkdir(); (self.old / ".janus/MEMORY.md").write_text("# m\n\n## Decisions\n- 2026-11-01 [db] use sqlite\n")
        self.home = Path(tempfile.mkdtemp(prefix="janus-home-")); (self.home / ".config/janus").mkdir(parents=True)
        (self.home / ".config/janus/projects").write_text(f"{self.old}\n")
        self.patches = [mock.patch.object(janus, "Beads", FakeBeads), mock.patch.object(janus, "probe", probe_ok()), mock.patch.object(janus, "herdr_agents", lambda cfg: {}),
                        mock.patch.object(janus, "session_alive", lambda cfg: None), mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(Path, "home", lambda: self.home),
                        mock.patch.object(janus, "sh", lambda cmd, **k: (0, "", "") if cmd[:3] == ["bd", "--readonly", "memories"] else (1, "", "n/a"))]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def run_migrate(self, *a, **k):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): rc = janus.migrate(*a, **k)
        return rc, buf.getvalue()

    def test_dry_run_changes_nothing_and_names_the_plan(self):
        rc, out = self.run_migrate(str(self.old), str(self.ws))
        self.assertEqual(rc, 0, out); self.assertIn("dry run", out); self.assertIn("1 into MEMORY.md", out)
        self.assertTrue((self.old / "janus.toml").is_file()); self.assertFalse((self.ws / ".janus").exists())

    def test_relocation_keeps_identity_messages_instructions_and_registry(self):
        rc, out = self.run_migrate(str(self.old), str(self.ws), apply=True)
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.old / "janus.toml").exists(), "the old root is still active")
        t = (self.ws / ".janus/janus.toml").read_text()
        self.assertIn('id = "rig"', t); self.assertIn('beads = "gate-logs/orch-1"', t); self.assertNotIn("briefs =", t)
        self.assertEqual((self.ws / ".janus/board/log.md").read_text().count("which one"), 1)
        self.assertEqual((self.ws / ".janus/out/r.md").read_text(), "result\n")
        self.assertTrue((self.old / "out").is_symlink())   # temporary: records that name the old path still resolve
        mem = (self.ws / ".janus/MEMORY.md").read_text()
        self.assertIn("use sqlite", mem); self.assertIn("Never touch the production database", mem)
        self.assertEqual([Path(x).resolve() for x in (self.home / ".config/janus/projects").read_text().split()], [self.ws.resolve()])
        sub = self.ws / "app" / "src"; sub.mkdir(parents=True)
        self.assertEqual(janus.find_root(sub), self.ws.resolve())   # one resolver, from a subdirectory

    def test_queued_work_or_an_unknown_queue_refuses(self):
        task = {"1": {"id": 1, "label": "unit:u1@rig", "status": "Queued"}}
        with mock.patch.object(janus, "probe", probe_ok(task)):
            rc, out = self.run_migrate(str(self.old), str(self.ws), apply=True)
        self.assertEqual(rc, 1); self.assertIn("is queued", out); self.assertTrue((self.old / "janus.toml").is_file())
        with mock.patch.object(janus, "probe", probe_ok(pueue=False)):
            rc, out = self.run_migrate(str(self.old), str(self.ws), apply=True)
        self.assertEqual(rc, 1); self.assertIn("status unknown", out); self.assertFalse((self.ws / ".janus").exists())

    def test_a_nonempty_local_outbox_refuses(self):
        (self.old / ".janus/board-outbox").mkdir(); (self.old / ".janus/board-outbox/outbox.log").write_text("2026-11-13 10:00 ann done: x\n")
        rc, out = self.run_migrate(str(self.old), str(self.ws), apply=True)
        self.assertEqual(rc, 1); self.assertIn("not synced", out)


if __name__ == "__main__":
    unittest.main()
