"""A remote unit's copy keeps its repository identity and lands on its own host, and its `lane` goes through the shared queueing
core. Real git and the real generated remote commands; only the transport is replaced (an hrun that runs them in a local shell, an
rsync without the host part): no server, no network. Fresh temp directories only."""
import contextlib, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

from janusload import conf, load

janus = load()
from janus.lane import main as lane_main
real_sh = janus.sh


def git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    if r.returncode: raise AssertionError(f"git {a}: {r.stderr}")
    return r.stdout.strip()


def fake_hrun(h, script, timeout=25, inp=None, env=None):
    r = subprocess.run(["sh", "-c", script], input=inp, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def fake_sh(argv, **k):
    if argv[0] == "rsync":   # the host part of the destination is dropped; ssh options too
        a = [x for i, x in enumerate(argv) if x != "-e" and argv[i - 1] != "-e"]
        return real_sh([*a[:-1], a[-1].split(":", 1)[1]], **k)
    return real_sh(argv, **k)


class RemoteGit(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="janus-test-")); self.root = self.d / "farroot"
        conf(self.d).write_text(f'[project]\nname = "t"\n[hosts.here]\nlocal = true\n[hosts.far]\nssh = "simhost"\nroot = "{self.root}"\n[landing]\nbranch = "main"\n')
        self.cfg = janus.Cfg(self.d); self.cfg.run.ro = False
        self.origin = self.d / "origin.git"; self.src = self.d / "src"; self.wt = self.d / "wt"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.src)], check=True)
        git(self.src, "config", "user.name", "Dev"); git(self.src, "config", "user.email", "dev@example.com")
        (self.src / "a").write_text("1\n2\n3\n"); git(self.src, "add", "a"); git(self.src, "commit", "-qm", "base")
        git(self.src, "remote", "add", "origin", str(self.origin)); git(self.src, "push", "-q", "origin", "main")
        git(self.src, "worktree", "add", "-q", "-b", "wk/u", str(self.wt))
        (self.wt / "dirty").write_text("uncommitted work\n")   # what the snapshot carries besides the commit
        for p in (mock.patch.object(janus, "hrun", fake_hrun), mock.patch.object(janus, "sh", fake_sh)): p.start(); self.addCleanup(p.stop)
        self.h = self.cfg.hosts["far"]; self.rcwd = f"{self.root}/worktrees/u"
        for x in ("run", "worktrees"): (self.root / x).mkdir(parents=True)   # what launch makes before the copy
        subprocess.run(["rsync", "-a", "--exclude", ".git", f"{self.wt}/", self.rcwd + "/"], check=True)

    def test_seed_commit_changed_main_and_nonforce_landing_on_the_host(self):
        """Catches: a remote copy without .git (land refused), a seed that loses the base or the uncommitted files, a landing that
        overwrites a moved main, or one that needs the coordinator."""
        repo, why = janus.seed_repo(self.cfg, self.h, "u", self.wt, self.rcwd)
        self.assertEqual(why, ""); self.assertEqual(repo["branch"], "wk/u")
        self.assertEqual(git(self.rcwd, "symbolic-ref", "--short", "HEAD"), "wk/u")
        self.assertEqual(git(self.rcwd, "rev-parse", "main"), git(self.src, "rev-parse", "main"))   # the landing branch as sent
        self.assertIn("?? dirty", git(self.rcwd, "status", "--porcelain"))                       # uncommitted files stay uncommitted
        self.assertEqual(git(self.rcwd, "remote", "get-url", "origin"), str(self.origin))
        self.assertEqual(git(self.rcwd, "config", "user.email"), "dev@example.com")
        self.assertFalse((self.root / "run/u.bundle").exists())
        # the remote worker commits; meanwhile main moves on origin
        (Path(self.rcwd) / "b").write_text("worker\n"); git(self.rcwd, "add", "b", "dirty"); git(self.rcwd, "commit", "-qm", "worker change")
        (self.src / "m").write_text("other\n"); git(self.src, "add", "m"); git(self.src, "commit", "-qm", "other work"); git(self.src, "push", "-q", "origin", "main")
        u = {"name": "u", "id": "1", "host": "far", "rcwd": self.rcwd, "cwd": str(self.wt), "repo": repo}
        a = type("A", (), {"unit": "u", "push": True, "host": None})()
        out = io.StringIO()
        with mock.patch.object(janus, "Beads", lambda cfg: type("B", (), {"units": lambda s: [u], "comment": lambda s, *x: None})()), contextlib.redirect_stdout(out):
            rc = janus.cmd_land(self.cfg, a)
        self.assertEqual(rc, 0, out.getvalue()); self.assertIn("push refused", out.getvalue())   # refused once because main had moved; the retry on the new tip published
        tree = git(self.origin, "ls-tree", "--name-only", "main").split()
        self.assertEqual(sorted(tree), ["a", "b", "dirty", "m"])                                 # the moved main kept, the worker's work on it
        self.assertEqual(git(self.origin, "log", "--format=%s", "main").count("\n"), 2)         # base, other work, one squash: nothing rewritten
        self.assertIn("main", (git(self.origin, "for-each-ref", "--format=%(refname:short)")))
        self.assertIn("does not prove", out.getvalue())                                          # a clean merge is not called valid

    def test_a_publication_that_stays_refused_fails_and_leaves_the_target_unchanged(self):
        """Catches: land --push reporting success (or closing the unit's work as done) when the remote refused the publication."""
        repo, why = janus.seed_repo(self.cfg, self.h, "u", self.wt, self.rcwd)
        (Path(self.rcwd) / "b").write_text("worker\n"); git(self.rcwd, "add", "b"); git(self.rcwd, "commit", "-qm", "worker change")
        hook = self.origin / "hooks/pre-receive"; hook.write_text("#!/bin/sh\necho protected >&2\nexit 1\n"); hook.chmod(0o755)
        before = git(self.origin, "rev-parse", "main")
        u = {"name": "u", "id": "1", "host": "far", "rcwd": self.rcwd, "cwd": str(self.wt), "repo": repo}
        a = type("A", (), {"unit": "u", "push": True, "host": None})()
        with mock.patch.object(janus, "Beads", lambda cfg: type("B", (), {"units": lambda s: [u], "comment": lambda s, *x: None})()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(janus.Err) as e: janus.cmd_land(self.cfg, a)
        self.assertIn("NOT PUBLISHED", str(e.exception)); self.assertIn("stays on wk/u", str(e.exception))
        self.assertEqual(git(self.origin, "rev-parse", "main"), before)                 # the remote target is untouched
        self.assertEqual(git(self.rcwd, "rev-parse", "main"), before)                   # and the landing branch there did not move
        self.assertEqual(git(self.rcwd, "log", "-1", "--format=%s", "wk/u"), "worker change")   # the work is kept on the unit's branch

    def test_a_non_git_source_stays_a_snapshot_and_says_why(self):
        plain = self.d / "plain"; plain.mkdir()
        repo, why = janus.seed_repo(self.cfg, self.h, "p", plain, str(plain))
        self.assertIsNone(repo); self.assertIn("not a git tree", why)

    def test_lane_guard_queue_and_record_use_the_shared_core(self):
        """Catches: the remote lane queueing without janus run's whole-suite guard or record, or with an environment that stores keys."""
        stub = self.d / "pueue"; log = self.d / "pueue.args"
        stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\ncase "$1" in add) echo 7;; esac\n'); stub.chmod(0o755)
        lsh, lcf = janus.lane_runner(self.cfg, "far", self.h, "u", self.rcwd)
        c = json.loads(lcf); c.update(pueue=str(stub), ensure="true"); cf = self.d / "u.lane.json"; cf.write_text(json.dumps(c))
        self.assertIn("lane.py", lsh)
        self.assertEqual(lane_main([str(cf), "--detach", "cargo test --workspace"]), 2)   # whole suite: refused like janus run
        self.assertFalse(log.exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lane_main([str(cf), "--detach", "cargo", "test", "-p", "x"]), 0)
            self.assertEqual(lane_main([str(cf), "--detach", "--broad", "release check", "make", "check"]), 0)
        self.assertEqual(out.getvalue().split(), ["far:7", "far:7"])
        adds = [l for l in log.read_text().splitlines() if l.startswith("add")]
        self.assertEqual(len(adds), 2); self.assertIn("-l unit:u@t", adds[0]); self.assertIn("-o=-5", adds[1]); self.assertNotIn("-o=", adds[0])
        recs = [json.loads(p.read_text()) for p in sorted((self.root / "runs").glob("*.json"))]
        self.assertEqual({(r["unit"], r["host"], r["task"]) for r in recs}, {("u", "far", "7")})
        self.assertEqual(sorted(r["cmd"] for r in recs), ["cargo test -p x", "make check"])


if __name__ == "__main__":
    unittest.main()
