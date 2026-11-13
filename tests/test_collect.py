"""janus collect's patch of a remote unit's changes: it must rebuild the unit's tree exactly (content, modes, symlinks,
names) or fail loudly and leave the copy uncollected. Each test names the failure it catches (review findings F1-F9 of the
difflib builder). Every case runs remote_patch as collect does, applies the patch with git to a checkout of the base and
compares the trees byte for byte. Standard library and git only; no network."""
import os, shutil, stat, subprocess, sys, tempfile, unittest
from pathlib import Path

from janusload import load

janus = load()
SKIP = {"target", ".venv", "node_modules", "__pycache__", ".git"}
X0 = janus.X


def write(root, files):
    """files: rel -> bytes | ("x", bytes) executable | ("link", target)"""
    for rel, v in files.items():
        p = Path(root) / rel; p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(v, tuple) and v[0] == "link": os.symlink(v[1], p)
        else:
            p.write_bytes(v[1] if isinstance(v, tuple) else v); os.chmod(p, 0o755 if isinstance(v, tuple) else 0o644)


def snap(root):
    """rel -> (kind, bytes) of every file and symlink (the excluded folders left out): what a patch must reproduce."""
    S = {}
    for d, dirs, files in os.walk(root):
        links = [x for x in dirs if os.path.islink(os.path.join(d, x))]
        dirs[:] = [x for x in dirs if x not in SKIP and x not in links]
        for f in files + links:
            p = os.path.join(d, f); st = os.lstat(p); r = os.path.relpath(p, root)
            if stat.S_ISLNK(st.st_mode): S[r] = ("link", os.readlink(p).encode())
            elif stat.S_ISREG(st.st_mode): S[r] = ("x" if st.st_mode & 0o100 else "file", Path(p).read_bytes())
    return S


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="janus-collect-")); self.addCleanup(shutil.rmtree, self.root, True)
        self.cfg = janus.Cfg(self._project()); self.h = {"local": True, "root": str(self.root)}
        (self.root / "out/u").mkdir(parents=True); self.patch = self.root / "out/u/changes.patch"
        self.addCleanup(setattr, janus, "X", X0)
    def _project(self):
        d = self.root / "proj"; d.mkdir(); (d / "janus.toml").write_text('[project]\nname = "t"\n[hosts.here]\nlocal = true\n'); return d
    def trees(self, base, wt):
        write(self.root / "base/u", base); write(self.root / "worktrees/u", wt)
        (self.root / "base/u").mkdir(parents=True, exist_ok=True); (self.root / "worktrees/u").mkdir(parents=True, exist_ok=True)
    def build(self, limit=120):
        """remote_patch as collect runs it (on this machine); limit: the seconds a test gives it instead of collect's 300."""
        janus.X = lambda cfg, h, script, **k: X0(cfg, h, script, **{**k, "timeout": limit})
        return janus.remote_patch(self.cfg, self.h, "u", "changes.patch")
    def git(self, *a, **k):
        return subprocess.run(["git", "-C", str(self.root / "chk"), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, **k)
    def checkout(self):
        shutil.copytree(self.root / "base/u", self.root / "chk", symlinks=True)
        self.git("init", "-q", "-b", "main"); self.git("add", "-A", "-f"); self.git("commit", "-qm", "base", "--allow-empty")
    def exact(self, base, wt, how=("apply", "--index")):
        """Build the patch for base -> wt, apply it to a checkout of base, and demand the unit's tree."""
        self.trees(base, wt); r = self.build()
        self.assertEqual(r[0], 0, f"the patch builder failed: {r[2]}")
        self.checkout(); a = self.git(*how, str(self.patch))
        self.assertEqual(a.returncode, 0, f"git {how[0]} refused the patch: {a.stderr.decode(errors='replace')}\n--- patch ---\n{self.patch.read_bytes()[:1500]!r}")
        self.assertEqual(snap(self.root / "chk"), snap(self.root / "worktrees/u"), "the patched checkout is not the unit's tree")
        return a


class F1NoFinalNewline(Case):
    def test_a_modified_file_without_a_final_newline_does_not_corrupt_the_patch(self):
        """Catches F1: difflib wrote no `\\ No newline at end of file` marker, so the next patch line was glued to the file's
        last line and git refused the WHOLE patch (`corrupt patch at line 7`): every other file of the unit was lost with it."""
        self.exact({"f": b"a\nb", "other": b"1\n"}, {"f": b"a\nb\nc", "other": b"2\n"})
    def test_a_new_and_a_deleted_file_without_a_final_newline_arrive_exactly(self):
        """Catches F1 (new/deleted): the next file's `diff --git` header was glued to the last line (`patch failed: a:1`)."""
        self.exact({"gone": b"x\ny", "keep": b"k\n"}, {"new.json": b'{"a": 1}', "keep": b"k2\n", "z": b"z\n"})


class F2UnreadablePath(Case):
    def test_an_unreadable_file_fails_the_build_names_the_path_and_keeps_the_earlier_patch(self):
        """Catches F2: one file the user could not read raised in the builder, changes.patch was left at 0 bytes with exit 1
        ignored, and the unit's other changes were gone from the patch without a word."""
        if os.geteuid() == 0: self.skipTest("root reads every file")
        self.trees({"m": b"1\n"}, {"m": b"2\n", "secret": b"s\n"}); os.chmod(self.root / "worktrees/u/secret", 0)
        self.patch.write_bytes(b"EARLIER WHOLE PATCH\n")
        rc, _, err = self.build()
        self.assertNotEqual(rc, 0, "a patch that leaves a file out was reported as built"); self.assertIn("secret", err)
        self.assertEqual(self.patch.read_bytes(), b"EARLIER WHOLE PATCH\n", "a failed build replaced the earlier whole patch")
        self.assertFalse((self.root / "out/u/changes.patch.tmp").exists())
    def test_collect_with_no_complete_patch_is_an_error_and_the_copy_stays_uncollected(self):
        """Catches F2 (the chain): cmd_collect ignored the builder's exit code and recorded the copy's fingerprint as collected,
        so `janus close` would remove the host's copy with an empty patch as the only copy of the work."""
        if os.geteuid() == 0: self.skipTest("root reads every file")
        self.trees({"m": b"1\n"}, {"m": b"2\n", "secret": b"s\n"}); os.chmod(self.root / "worktrees/u/secret", 0)
        u = {"id": "t-1", "name": "u", "host": "far", "via": "tmux", "cwd": "/w"}; saved = []
        class Bd:
            def save(self, x): saved.append(dict(x))
            def comment(self, *a): saved.append(a)
        self.cfg.hosts["far"] = {"root": str(self.root), "ssh": "far"}
        keep = {k: getattr(janus, k) for k in ("get_unit", "hrun", "save_output", "save_transcript", "fingerprint")}
        for k, v in keep.items(): self.addCleanup(setattr, janus, k, v)
        janus.get_unit = lambda cfg, n: (Bd(), u); janus.hrun = lambda h, script, **k: keep["hrun"]({"local": True}, script, **k)
        janus.save_output = lambda cfg, u: True; janus.save_transcript = lambda cfg, u: True; janus.fingerprint = lambda cfg, u: {"fp": "now"}
        import argparse
        with self.assertRaises(janus.Err) as cm: janus.cmd_collect(self.cfg, argparse.Namespace(unit="u"))
        self.assertIn("secret", str(cm.exception)); self.assertIn("NOT collected", str(cm.exception))
        self.assertNotIn("collected", u, "a copy whose patch could not be made was recorded as collected"); self.assertEqual(saved, [])
    def test_a_dangling_symlink_is_carried_and_takes_no_other_change_with_it(self):
        """Catches F2 (dangling symlink): the builder opened the link's target, raised FileNotFoundError and lost the unit's
        real change to `m` with it."""
        self.exact({"m": b"1\n"}, {"m": b"2\n", "lnk": ("link", "nowhere")})
    def test_a_fifo_does_not_block_the_build_and_is_named(self):
        """Catches F2 (fifo): the builder opened the fifo for reading and blocked until collect's 300 s limit: no patch at all."""
        self.trees({"m": b"1\n"}, {"m": b"2\n"}); ff = self.root / "worktrees/u/pipe"; os.mkfifo(ff)
        def unblock():   # a builder still blocked on the fifo (the old one) gets its end of file
            try: os.close(os.open(ff, os.O_WRONLY | os.O_NONBLOCK))
            except OSError: pass
        self.addCleanup(unblock)
        rc, _, err = self.build(limit=20)
        self.assertEqual(rc, 0, f"the build did not finish: {err}"); self.assertIn("pipe", err, "the path left out of the patch is not named")
        self.assertIn(b"+2", self.patch.read_bytes())


class F3NotUtf8(Case):
    def test_latin1_text_new_and_modified_arrives_byte_exact(self):
        """Catches F3: decode(errors="replace") turned each non-UTF-8 byte into U+FFFD: a new latin-1 file arrived with wrong
        bytes and no error, a modified one made git refuse the whole patch."""
        self.exact({"old.txt": b"caf\xe9\nline\n"}, {"old.txt": b"caf\xe9\nligne\xe8\n", "new.txt": b"na\xefve\n"})


class F4LineSeparators(Case):
    def test_a_line_holding_a_form_feed_or_another_separator_does_not_corrupt_the_patch(self):
        """Catches F4: str.splitlines split on form feed, lone CR, VT, FS/GS/RS, NEL and U+2028/2029 where git splits only on
        LF: `corrupt patch at line 9`."""
        odd = b"a\x0cb\n" + b"c\rd\n" + b"e\x0bf\n" + b"g\x1c\x1d\x1eh\n" + "i\u0085j k l\n".encode()
        self.exact({"f.c": b"top\n" + odd + b"end\n"}, {"f.c": b"top\n" + odd + b"changed\x0c\n", "n.py": odd})


class F5RepetitiveFile(Case):
    def test_a_long_repetitive_file_is_patched_in_seconds(self):
        """Catches F5: difflib is quadratic on repetitive lines: 20,000 lines with every 50th changed took 14 to 32 s, 40,000
        over 60 s, and past collect's 300 s limit there was no patch at all. (The limit here is 20 s; git needs under one.)"""
        a = [b'  {"k": %d, "v": "row"},\n' % (i % 280) for i in range(40000)]
        b = [l if i % 50 else b'  {"k": "changed"},\n' for i, l in enumerate(a)]
        self.trees({"data.json": b"".join(a)}, {"data.json": b"".join(b)}); rc, _, err = self.build(limit=20)
        self.assertEqual(rc, 0, f"no patch within 20 s (exit {rc}): {err}")
        self.checkout(); self.assertEqual(self.git("apply", "--index", str(self.patch)).returncode, 0)
        self.assertEqual(snap(self.root / "chk"), snap(self.root / "worktrees/u"))


class Guard(Case):
    def test_a_patch_that_does_not_rebuild_the_units_tree_is_refused(self):
        """Catches the class, not a case: whatever makes the written patch wrong (here a git whose diff output loses its last
        line), the builder applies it to the base tree, sees another tree than the unit's and fails instead of printing it."""
        self.trees({"f": b"a\nb\n", "g": b"1\n"}, {"f": b"a\nb\nc\n", "g": b"2\n3\n"})
        real = shutil.which("git"); fake = self.root / "bin"; fake.mkdir()
        (fake / "git").write_text(f'#!/bin/sh\ncase " $* " in *" diff-tree "*) {real} "$@" | sed \'$d\';; *) exec {real} "$@";; esac\n'); os.chmod(fake / "git", 0o755)
        p = subprocess.run([sys.executable, "-c", janus.PATCH_PY, "u", "base/u", "worktrees/u"], cwd=self.root, capture_output=True,
                           env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
        self.assertNotEqual(p.returncode, 0, "a patch that loses a line was printed as the unit's changes")
        self.assertEqual(p.stdout, b""); self.assertIn(b"no complete patch", p.stderr)
    def test_excluded_folders_stay_out_and_an_unchanged_tree_gives_an_empty_patch(self):
        """Pins the neighbours: build output (target, .venv, ...) never reaches the patch, and no change is 0 bytes, exit 0."""
        self.trees({"k": b"k\n", "target/o": b"1\n"}, {"k": b"k\n", "target/o": b"2\n", ".venv/x": b"v\n", "sub/__pycache__/c.pyc": b"\0"})
        self.assertEqual(self.build()[0], 0); self.assertEqual(self.patch.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
