"""janus migrate: the one-time move from the old nested root to the workspace's .janus. Catches: identity, messages, instructions or
the registry lost in the move; a move while work is queued or a host cannot say; the old root left active."""
import json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

from janusload import load

janus = load()
real_sh = janus.sh


class FakeBeads:
    units_ = [{"name": "u1", "id": "b1", "via": "pueue", "host": "here", "state": "done"}]
    def __init__(self, cfg): pass
    def units(self): return list(self.units_)
    def run(self, *a):   # all_units: closed units too
        return [{"id": "b1", "title": "unit:u1", "created_at": "1", "status": "open", "notes": json.dumps({"cwd": str(FakeBeads.cwd), "host": "here"})},
                {"id": "b2", "title": "unit:old", "created_at": "2", "status": "closed", "notes": json.dumps({"cwd": str(FakeBeads.cwd_closed), "host": "here"})}]
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
        self.memories = (0, self.MEMORIES, "")
        app = self.ws / "app"; subprocess.run(["git", "init", "-q", "-b", "main", str(app)], check=True)   # real git: worktrees of a unit, a closed unit, and one nobody owns
        subprocess.run(["git", "-C", str(app), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        (self.old / "worktrees").mkdir()
        for n in ("u1wt", "oldwt", "other"): subprocess.run(["git", "-C", str(app), "worktree", "add", "-q", "-b", n, str(self.old / "worktrees" / n)], check=True)
        FakeBeads.cwd, FakeBeads.cwd_closed = self.old / "worktrees/u1wt", self.old / "worktrees/oldwt"
        self.patches = [mock.patch.object(janus, "Beads", FakeBeads), mock.patch.object(janus, "probe", probe_ok()), mock.patch.object(janus, "herdr_agents", lambda cfg: {}),
                        mock.patch.object(janus, "session_alive", lambda cfg: None), mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(Path, "home", lambda: self.home),
                        mock.patch.object(janus, "sh", self.fake_sh)]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    MEMORIES = '{\n  "schema_version": 1,\n  "use-pg": "always use postgres 16 for local runs"\n}\n'   # what `bd --readonly memories --json` prints (bd 1.3)

    def fake_sh(self, cmd, **k):   # bd is only mocked (no database is created or written); git is real
        if cmd[:4] == ["bd", "--readonly", "memories", "--json"]: return self.memories
        return real_sh(cmd, **k)

    def run_migrate(self, *a, **k):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): rc = janus.migrate(*a, **k)
        return rc, buf.getvalue()

    def test_dry_run_changes_nothing_and_names_the_plan(self):
        rc, out = self.run_migrate(str(self.old), str(self.ws))
        self.assertEqual(rc, 0, out); self.assertIn("dry run", out); self.assertIn("2 into MEMORY.md", out)
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
        self.assertIn("use sqlite", mem); self.assertIn("Never touch the production database", mem); self.assertIn("postgres 16", mem)
        w = self.ws / ".janus/worktrees"                                  # owned by a unit record, open or closed: moved with its git metadata
        for n in ("u1wt", "oldwt"):
            self.assertEqual(subprocess.run(["git", "-C", str(w / n), "status", "--porcelain"], capture_output=True).returncode, 0)
        self.assertTrue((self.old / "worktrees/other/.git").is_file() and not (w / "other").exists())   # an unowned tree stays where it is
        m = self.ws / ".janus/migration"; sh_ = (m / "bd-path-updates.sh").read_text()
        self.assertIn("bd update b2", sh_); self.assertIn(str(w / "oldwt"), sh_); self.assertIn("NOT RUN", sh_)   # closed units' records are prepared too
        self.assertIn("mv ", (m / "bd-relocate.sh").read_text()); self.assertTrue((m / "PENDING.md").is_file())
        self.assertFalse((self.old / ".beads").exists() or (self.ws / ".janus/.beads").exists())   # the database was neither moved nor created
        self.assertEqual([Path(x).resolve() for x in (self.home / ".config/janus/projects").read_text().split()], [self.ws.resolve()])
        sub = self.ws / "app" / "src"; sub.mkdir(parents=True)
        self.assertEqual(janus.find_root(sub), self.ws.resolve())   # one resolver, from a subdirectory

    def test_unreadable_memories_or_a_blocked_old_root_never_lose_an_instruction(self):
        self.memories = (1, "", "bd: no database")
        rc, out = self.run_migrate(str(self.old), str(self.ws), apply=True)
        self.assertEqual(rc, 1); self.assertIn("bd memories cannot be read", out); self.assertTrue((self.old / "janus.toml").is_file())

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
