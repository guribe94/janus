"""janus collect's patch of a remote unit's changes: it must rebuild the unit's tree exactly (content, modes, symlinks,
names) or fail loudly and leave the copy uncollected. Each test names the failure it catches (review findings F1-F9 of the
difflib builder). Every case runs remote_patch as collect does, applies the patch with git to a checkout of the base and
compares the trees byte for byte. Standard library and git only; no network."""
import fcntl, os, shlex, shutil, signal, stat, subprocess, sys, tempfile, threading, time, unittest
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


FAKE_RUN = """#!/bin/sh
# systemd-run --user --scope as remote_patch calls it: records its arguments, registers the scope, execs the command (same pid)
printf '%s\\n' "$@" > "$FAKESD/systemd-run.args"
while [ $# -gt 0 ] && [ "$1" != "--" ]; do case "$1" in --unit=*) u=${1#--unit=};; --description=*) d=${1#--description=};; esac; shift; done
shift; mkdir -p "$FAKESD/u/$u"; echo loaded > "$FAKESD/u/$u/LoadState"; printf '%s\\n' "$d" > "$FAKESD/u/$u/Description"
echo active > "$FAKESD/u/$u/ActiveState"; echo "/$u" > "$FAKESD/u/$u/ControlGroup"
exec "$@"
"""
FAKE_CTL = """#!/bin/sh
# systemctl --user: show -p <prop> --value <unit> from $FAKESD/u/<unit>/<prop>; kill/stop/reset-failed recorded and applied
[ "$1" = --user ] && shift; echo "$*" >> "$FAKESD/systemctl.calls"
case "$1" in
  show) f="$FAKESD/u/$5/$3"; if [ -f "$f" ]; then cat "$f"; else case $3 in LoadState) echo not-found;; Description) echo "$5";; ActiveState) echo inactive;; *) echo;; esac; fi;;
  kill) [ -f "$FAKESD/u/$3/alive" ];;
  stop) rm -rf "$FAKESD/u/$2";;
  reset-failed) [ "$(cat "$FAKESD/u/$2/ActiveState" 2>/dev/null)" = failed ] && rm -rf "$FAKESD/u/$2"; true;;
esac
"""


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="janus-collect-")); self.addCleanup(shutil.rmtree, self.root, True)
        self.cfg = janus.Cfg(self._project()); self.h = {"local": True, "root": str(self.root)}
        (self.root / "out/u").mkdir(parents=True); self.patch = self.root / "out/u/changes.patch"
        self.addCleanup(setattr, janus, "X", X0)
        self.lockdir = self.root / "lk"; self.lockdir.mkdir()   # the host's per-user lock folder, here this test's own
        self.addCleanup(setattr, janus, "PATCH_LOCK_DIR", janus.PATCH_LOCK_DIR); janus.PATCH_LOCK_DIR = shlex.quote(str(self.lockdir))
    def fake_systemd(self, swap="0", mem=None):
        """A remote host here: hrun runs locally with a fake systemd-run/systemctl ahead on PATH and a cgroup tree whose limits
        the scope's guard reads (swap/mem: what memory.swap.max/memory.max say). Returns (host, the fake's state folder)."""
        sd = self.root / "sd"; b = sd / "bin"; b.mkdir(parents=True)
        for n, t in (("systemd-run", FAKE_RUN), ("systemctl", FAKE_CTL)): (b / n).write_text(t); os.chmod(b / n, 0o755)
        h = {"root": str(self.root), "ssh": "far"}; cg = sd / "cg" / janus.patch_scope(h).strip("/"); cg.mkdir(parents=True)
        (cg / "memory.max").write_text(f"{mem or janus.PATCH_MEM_MB * 1048576}\n"); (cg / "memory.swap.max").write_text(swap + "\n")
        hrun0 = janus.hrun; self.addCleanup(setattr, janus, "hrun", hrun0); self.addCleanup(setattr, janus, "CGROUP_ROOT", janus.CGROUP_ROOT)
        janus.CGROUP_ROOT = str(sd / "cg")
        janus.hrun = lambda hh, script, **k: hrun0({"local": True}, script, **{**k, "env": {"PATH": f"{b}:{os.environ['PATH']}", "FAKESD": str(sd)}})
        return h, sd
    def lock_free(self):
        with open(self.lockdir / f"janus-patch-{os.getuid()}.lock", "a") as f:
            try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); return True
            except BlockingIOError: return False
    def _project(self):
        d = self.root / "proj"; d.mkdir(); (d / ".janus").mkdir(); (d / ".janus/janus.toml").write_text('[project]\nname = "t"\n[hosts.here]\nlocal = true\n'); return d
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
        self.cfg.hosts["far"], _ = self.fake_systemd()   # the remote host's whole script (lock, scope, guard) runs here
        keep = {k: getattr(janus, k) for k in ("get_unit", "save_output", "save_transcript", "fingerprint")}
        for k, v in keep.items(): self.addCleanup(setattr, janus, k, v)
        janus.get_unit = lambda cfg, n: (Bd(), u)
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


class F6Binary(Case):
    def test_new_modified_and_deleted_binary_files_arrive(self):
        """Catches F6: every file with a NUL byte was skipped with exit 0 and no message, so a unit whose only change was a
        binary fixture collected as 0 bytes and was marked collected."""
        blob = bytes(range(256)) * 40
        self.exact({"mod.bin": blob, "del.bin": b"\0\1\2", "t": b"t\n"}, {"mod.bin": blob[:5000] + b"\0NEW\0" + blob[5000:], "new.png": b"\x89PNG\0\0" + blob, "t": b"t\n"})


class F7ModesAndSymlinks(Case):
    def test_an_executable_a_mode_change_and_symlinks_arrive_as_they_are(self):
        """Catches F7: a new executable arrived 100644, chmod +x alone made no patch, and a symlink arrived as a regular file
        holding its target's content."""
        self.exact({"run.sh": b"#!/bin/sh\n", "was.sh": ("x", b"#!/bin/sh\n"), "data": b"d\n", "cur": ("link", "data"), "dir/f": b"f\n"},
                   {"run.sh": ("x", b"#!/bin/sh\n"), "was.sh": b"#!/bin/sh\n", "data": b"d\n", "cur": ("link", "dir/f"), "dir/f": b"f\n",
                    "tool": ("x", b"#!/bin/sh\necho\n"), "ln": ("link", "data"), "dlink": ("link", "dir")})


class F8QuotedNames(Case):
    def test_a_name_with_a_quote_a_tab_or_a_newline_arrives_under_its_own_name(self):
        """Catches F8: names were written unquoted into the headers: `q"t<TAB>x.txt` arrived as a file named `q"t`."""
        self.exact({'old"q\tx.txt': b"1\n"}, {'old"q\tx.txt': b"2\n", 'q"t\tx.txt': b"n\n", "nl\nname": b"n\n", "sp ace/café \\ b": b"s\n"})


class F9ThreeWay(Case):
    def test_git_apply_3way_merges_the_patch_into_a_checkout_that_moved(self):
        """Catches F9: collect's hint says `git apply --3way`, but the patch had no `index` lines, so git could never merge:
        `repository lacks the necessary blob to perform 3-way merge`, and a checkout that had moved refused the patch."""
        lines = [b"line %d\n" % i for i in range(40)]
        self.trees({"f": b"".join(lines)}, {"f": b"".join(lines[:35] + [b"the unit's line\n"] + lines[35:])}); self.assertEqual(self.build()[0], 0)
        self.checkout(); (self.root / "chk/f").write_bytes(b"".join([b"moved on\n"] * 12 + lines)); self.git("commit", "-qam", "moved")
        a = self.git("apply", "--3way", str(self.patch))
        self.assertEqual(a.returncode, 0, a.stderr.decode()); self.assertNotIn(b"lacks the necessary blob", a.stderr)
        got = (self.root / "chk/f").read_bytes(); self.assertIn(b"the unit's line\n", got); self.assertTrue(got.startswith(b"moved on\n"))


class Guard(Case):
    def test_a_patch_that_does_not_rebuild_the_units_tree_is_refused(self):
        """Catches the class, not a case: whatever makes the written patch wrong (here a git whose diff output carries another
        line than the unit wrote, in a patch that still applies cleanly), the builder applies it to the base tree, sees
        another tree than the unit's and fails instead of printing it."""
        self.trees({"f": b"a\nb\n", "g": b"1\n"}, {"f": b"a\nb\nc\n", "g": b"2\n3\n"})
        real = shutil.which("git"); fake = self.root / "bin"; fake.mkdir()
        (fake / "git").write_text(f'#!/bin/sh\ncase " $* " in *" diff-tree "*) {real} "$@" | sed \'s/^+c$/+X/\';; *) exec {real} "$@";; esac\n'); os.chmod(fake / "git", 0o755)
        p = subprocess.run([sys.executable, "-c", janus.PATCH_PY, "u", "base/u", "worktrees/u"], cwd=self.root, capture_output=True,
                           env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
        self.assertNotEqual(p.returncode, 0, "a patch that does not give the unit's tree was printed as its changes")
        self.assertEqual(p.stdout, b""); self.assertIn(b"does not rebuild the unit's tree", p.stderr)
    def test_excluded_folders_stay_out_and_an_unchanged_tree_gives_an_empty_patch(self):
        """Pins the neighbours: build output (target, .venv, ...) never reaches the patch, and no change is 0 bytes, exit 0."""
        self.trees({"k": b"k\n", "target/o": b"1\n"}, {"k": b"k\n", "target/o": b"2\n", ".venv/x": b"v\n", "sub/__pycache__/c.pyc": b"\0"})
        self.assertEqual(self.build()[0], 0); self.assertEqual(self.patch.read_bytes(), b"")


class ToolCachesLeftOut(Case):
    def test_tool_caches_and_ignored_new_files_are_not_in_the_patch(self):
        """Catches row 251: a unit that only ran tests came back with a changes.patch of `.pytest_cache`/`.ruff_cache` files, which
        looked like work to land. Cache folders and NEW files the tree's own .gitignore ignores are left out; a file that was
        sent (in the base) is carried even when an ignore rule matches it."""
        base = {"a": b"1\n", ".gitignore": b"build/\n*.log\n", "kept.log": b"old\n"}
        self.trees(base, {**base, "a": b"2\n", "kept.log": b"new\n", ".pytest_cache/v/cache/lastfailed": b"{}", "sub/.ruff_cache/0.1/x": b"c",
                          "build/o.bin": b"\0\1", "run.log": b"l\n"})
        r = self.build(); self.assertEqual(r[0], 0, r[2])
        self.assertEqual([l for l in self.patch.read_text().splitlines() if l.startswith("diff --git ")], ["diff --git a/a b/a", "diff --git a/kept.log b/kept.log"])


class Containment(Case):
    """The patch helper on a shared host (patch-containment): one per host and user, busy at once and nothing queued; a remote
    helper in its own memory scope (512M in all, no swap) or no patch at all; a deadline that ends its whole process group; a
    lock its own processes hold even when the ssh session that started it is gone; the earlier complete patch kept on any
    failure. Each case runs the real helper through the real script; only systemd is a fake (FAKE_RUN/FAKE_CTL) and git, where
    a case needs a helper that hangs, a wrapper around the real one. The timeouts are shortened here only."""
    def setUp(self):
        super().setUp()
        for k in ("PATCH_DEADLINE", "PATCH_KILL"): self.addCleanup(setattr, janus, k, getattr(janus, k))
        self.go = self.root / "go"; self.hung = self.root / "hung"; self.child = self.root / "child"
    def slow_git(self, ignore_term=False):
        """A git that hangs in diff-tree (the patch step) until the test writes `go`; ignore_term: it and a child of its own
        ignore TERM, as a helper stuck in a syscall or a stubborn grandchild would."""
        real = shutil.which("git"); b = self.root / "gitbin"; b.mkdir()
        stub = (f"sh -c 'trap \"\" TERM; sleep 60' & echo $! > {shlex.quote(str(self.child))}; trap '' TERM; " if ignore_term else "")
        (b / "git").write_text(f'#!/bin/sh\ncase " $* " in *" diff-tree "*) {stub}echo $$ > {shlex.quote(str(self.hung))}; '
                               f'while [ ! -f {shlex.quote(str(self.go))} ]; do sleep 0.1; done;; esac\nexec {real} "$@"\n')
        os.chmod(b / "git", 0o755); self.gitpath = f"{b}:{os.environ['PATH']}"   # ahead of clean_env's /opt/homebrew/bin too
        h0 = janus.hrun; self.addCleanup(setattr, janus, "hrun", h0)
        janus.hrun = lambda hh, s, **k: h0(hh, s, **{**k, "env": {**(k.get("env") or {}), "PATH": f"{b}:" + (k.get("env") or {}).get("PATH", os.environ["PATH"])}})
    def wait_for(self, cond, secs=20):
        t = time.time() + secs
        while time.time() < t:
            if cond(): return True
            time.sleep(0.05)
        return False
    def check_applies(self):
        self.checkout(); a = self.git("apply", "--index", str(self.patch))
        self.assertEqual(a.returncode, 0, a.stderr); self.assertEqual(snap(self.root / "chk"), snap(self.root / "worktrees/u"))
    def alive(self, pid):
        try: os.kill(pid, 0); return True
        except ProcessLookupError: return False

    def test_a_remote_helper_runs_in_its_own_memory_scope_and_still_builds_the_exact_patch(self):
        """The memory wrapper: systemd-run --user --scope named for this project's root, MemoryMax=512M and MemorySwapMax=0 for
        the helper and every git it runs, and the patch built inside it is the real one (git applies it, trees equal)."""
        h, sd = self.fake_systemd()
        self.trees({"m": b"1\n", "gone": b"g\n"}, {"m": b"2\n", "new": ("x", b"#!/bin/sh\n"), "ln": ("link", "m")})
        rc, _, err = janus.remote_patch(self.cfg, h, "u", "changes.patch"); self.assertEqual(rc, 0, err)
        args = (sd / "systemd-run.args").read_text().splitlines()
        sc = janus.patch_scope(h); self.assertRegex(sc, r"^janus_patch-[0-9a-f]{16}\.scope$")
        for a in ("--user", "--scope", f"--unit={sc}", f"--description=janus patch {self.root}", "MemoryMax=512M", "MemorySwapMax=0"): self.assertIn(a, args)
        self.assertEqual(args[args.index("MemoryMax=512M") - 1], "-p"); self.assertEqual(args[args.index("MemorySwapMax=0") - 1], "-p")
        self.assertEqual(janus.patch_scope({"root": str(self.root)}), sc, "the scope's name is not a function of the project's root alone")
        self.assertNotEqual(janus.patch_scope({"root": str(self.root) + "x"}), sc)
        self.check_applies()
        self.assertTrue(self.lock_free()); self.assertEqual(list((self.root / "out/u").glob("*.tmp*")), [])

    def test_no_patch_when_the_scope_limits_are_not_in_force_and_the_earlier_one_is_kept(self):
        """Fail closed: a host where systemd accepts the scope but swap is not limited (memory controller not delegated,
        cgroup v1) runs no helper at all; the earlier whole patch stays."""
        for swap, mem in (("max", None), ("0", "max")):
            with self.subTest(swap=swap, mem=mem):
                h, sd = self.fake_systemd(swap=swap, mem=mem); self.trees({"m": b"1\n"}, {"m": b"2\n"}); self.patch.write_bytes(b"EARLIER\n")
                rc, _, err = janus.remote_patch(self.cfg, h, "u", "changes.patch")
                self.assertEqual(rc, janus.PATCH_UNSAFE, err); self.assertIn("not in force", err)
                self.assertEqual(self.patch.read_bytes(), b"EARLIER\n"); self.assertEqual(list((self.root / "out/u").glob("*.tmp*")), [])
                shutil.rmtree(sd); shutil.rmtree(self.root / "base"); shutil.rmtree(self.root / "worktrees")

    def test_no_patch_on_a_remote_host_without_systemd(self):
        """Fail closed: no systemd-run on the host is no patch (never an unprotected helper)."""
        if shutil.which("systemd-run"): self.skipTest("this machine has systemd-run")
        hrun0 = janus.hrun; self.addCleanup(setattr, janus, "hrun", hrun0)
        janus.hrun = lambda hh, script, **k: hrun0({"local": True}, script, **k)
        self.trees({"m": b"1\n"}, {"m": b"2\n"}); self.patch.write_bytes(b"EARLIER\n")
        rc, _, err = janus.remote_patch(self.cfg, {"root": str(self.root), "ssh": "far"}, "u", "changes.patch")
        self.assertEqual(rc, janus.PATCH_UNSAFE); self.assertIn("systemd-run is missing", err); self.assertEqual(self.patch.read_bytes(), b"EARLIER\n")

    def test_a_scope_of_that_name_that_is_not_this_projects_is_left_alone(self):
        """Recovery verifies ownership: a scope with the helper's name whose Description is not this project's root is never
        killed, stopped or reset, and no helper runs."""
        h, sd = self.fake_systemd(); u = sd / "u" / janus.patch_scope(h); u.mkdir(parents=True)
        for k, v in (("LoadState", "loaded"), ("Description", "someone else's"), ("ActiveState", "active")): (u / k).write_text(v + "\n")
        (u / "alive").write_text(""); self.trees({"m": b"1\n"}, {"m": b"2\n"}); self.patch.write_bytes(b"EARLIER\n")
        rc, _, err = janus.remote_patch(self.cfg, h, "u", "changes.patch")
        self.assertEqual(rc, janus.PATCH_UNSAFE, err); self.assertIn("foreign", err)
        calls = (sd / "systemctl.calls").read_text()
        self.assertNotRegex(calls, r"(?m)^(kill|stop|reset-failed)\b", "another's scope was touched"); self.assertFalse((sd / "systemd-run.args").exists())
        self.assertEqual(self.patch.read_bytes(), b"EARLIER\n"); self.assertTrue(u.is_dir())
        self.assertEqual(janus.patch_scope_state(h), ("foreign", "")); self.assertEqual(janus.patch_scope_state(h, "stop"), ("foreign", ""))
        self.assertTrue(u.is_dir(), "patch_scope_state stop removed a foreign scope")

    def test_this_projects_leftover_scope_is_recovered_then_the_patch_is_built(self):
        """Recovery: a failed (or orphaned) scope of this project's, by name and Description, is stopped and reset first."""
        h, sd = self.fake_systemd(); u = sd / "u" / janus.patch_scope(h); u.mkdir(parents=True)
        for k, v in (("LoadState", "loaded"), ("Description", janus.patch_scope_desc(h)), ("ActiveState", "failed")): (u / k).write_text(v + "\n")
        self.trees({"m": b"1\n"}, {"m": b"2\n"})
        rc, _, err = janus.remote_patch(self.cfg, h, "u", "changes.patch"); self.assertEqual(rc, 0, err)
        calls = (sd / "systemctl.calls").read_text().splitlines()
        self.assertIn(f"stop {janus.patch_scope(h)}", calls); self.check_applies()
        # janus thrash's stop of a running scope of this project's (the same shell): ownership read, TERM asked, stopped
        u.mkdir(parents=True, exist_ok=True)
        for k, v in (("LoadState", "loaded"), ("Description", janus.patch_scope_desc(h)), ("ActiveState", "active")): (u / k).write_text(v + "\n")
        self.assertEqual(janus.patch_scope_state(h), ("owned-active", ""))
        self.assertEqual(janus.patch_scope_state(h, "stop"), ("owned-inactive", "")); self.assertFalse(u.exists())
        self.assertIn(f"kill --signal=TERM {janus.patch_scope(h)}", (sd / "systemctl.calls").read_text())

    def test_one_helper_per_host_a_second_is_busy_at_once_and_nothing_is_queued(self):
        """Concurrency: while one helper runs, another call on that host returns PATCH_BUSY at once, writes nothing and leaves
        no waiter behind; the first finishes with its exact patch and the lock is free after."""
        self.slow_git(); self.trees({"m": b"1\n"}, {"m": b"2\n"}); (self.root / "out/v").mkdir()
        got = {}; t = threading.Thread(target=lambda: got.setdefault("r", janus.remote_patch(self.cfg, self.h, "u", "changes.patch"))); t.start()
        self.addCleanup(lambda: (self.go.write_text(""), t.join(30)))
        self.assertTrue(self.wait_for(self.hung.exists), "the first helper never reached git")
        t0 = time.time(); rc, _, err = janus.remote_patch(self.cfg, self.h, "v", "changes.patch")
        self.assertEqual(rc, janus.PATCH_BUSY, err); self.assertIn("busy", err); self.assertLess(time.time() - t0, 5, "the second call waited")
        self.assertEqual(list((self.root / "out/v").iterdir()), [], "a busy call wrote something")
        self.assertFalse(self.lock_free())
        self.go.write_text(""); t.join(30); self.assertEqual(got["r"][0], 0, got["r"][2]); self.check_applies()
        self.assertTrue(self.lock_free(), "the lock outlived the helper")

    def test_the_deadline_ends_the_helpers_whole_process_group_and_keeps_the_earlier_patch(self):
        """Remote timeout: at the deadline the helper's process group gets TERM, and what ignores it (here git and a child of
        its own) is KILLed; no process of it is left, the lock is free, and the earlier whole patch is not replaced."""
        for remote in (False, True):
            with self.subTest(remote=remote):
                for p in (self.go, self.hung, self.child): p.unlink(missing_ok=True)
                h = self.fake_systemd()[0] if remote else self.h
                if not (self.root / "gitbin").exists(): self.slow_git(ignore_term=True)
                janus.PATCH_DEADLINE, janus.PATCH_KILL = 2, 1
                self.trees({"m": b"1\n"}, {"m": b"2\n"}); self.patch.write_bytes(b"EARLIER\n"); (self.root / "out/u/changes.patch.tmp.OTHER1").write_bytes(b"theirs")
                t0 = time.time(); rc, _, err = janus.remote_patch(self.cfg, h, "u", "changes.patch")
                self.assertIn(rc, (124, 137), err); self.assertIn("no patch within 2 s", err); self.assertLess(time.time() - t0, 20)
                gpid, cpid = int(self.hung.read_text()), int(self.child.read_text())
                self.assertTrue(self.wait_for(lambda: not self.alive(gpid) and not self.alive(cpid), 5), "a process of the helper's group outlived the deadline")
                self.assertTrue(self.lock_free()); self.assertEqual(self.patch.read_bytes(), b"EARLIER\n")
                self.assertEqual([p.name for p in (self.root / "out/u").glob("*.tmp*")], ["changes.patch.tmp.OTHER1"], "another invocation's temporary file was removed, or ours was left")
                for d in ("base", "worktrees", "sd"): shutil.rmtree(self.root / d, True)
                (self.root / "out/u/changes.patch.tmp.OTHER1").unlink()

    def test_the_lock_stays_with_the_helper_when_the_ssh_session_is_gone(self):
        """The lock is held by the helper's own processes: the shell ssh started is killed (a dropped connection) while git
        runs, and the host stays busy until the helper itself has ended; that run's half patch never replaces the earlier one."""
        self.slow_git(); self.trees({"m": b"1\n"}, {"m": b"2\n"}); self.patch.write_bytes(b"EARLIER\n")
        p = subprocess.Popen(["sh", "-c", janus.patch_script(self.h, "u", "changes.patch")], env=janus.clean_env({"PATH": self.gitpath}), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self.go.write_text(""), p.poll() is None and p.kill()))
        self.assertTrue(self.wait_for(self.hung.exists), "the helper never reached git")
        p.kill(); p.wait(10)
        self.assertFalse(self.lock_free(), "the lock went with the ssh session while the helper still runs")
        self.assertEqual(janus.remote_patch(self.cfg, self.h, "u", "changes.patch")[0], janus.PATCH_BUSY)
        self.go.write_text(""); self.assertTrue(self.wait_for(self.lock_free), "the lock was never let go after the helper ended")
        self.assertEqual(self.patch.read_bytes(), b"EARLIER\n")

    def test_a_failed_helper_keeps_the_earlier_patch_and_only_its_own_temporary_file_goes(self):
        """Failure preservation: the helper fails (no base tree), the earlier patch is kept, the old fixed .tmp name and another
        invocation's temporary file are left as they are."""
        (self.root / "worktrees/u").mkdir(parents=True); (self.root / "worktrees/u/m").write_bytes(b"2\n"); self.patch.write_bytes(b"EARLIER\n")
        for n in ("changes.patch.tmp", "changes.patch.tmp.AbC123"): (self.root / "out/u" / n).write_bytes(b"other")
        rc, _, err = self.build(); self.assertEqual(rc, 1, err); self.assertIn("base/u is missing", err)
        self.assertEqual(self.patch.read_bytes(), b"EARLIER\n")
        self.assertEqual(sorted(p.name for p in (self.root / "out/u").glob("*.tmp*")), ["changes.patch.tmp", "changes.patch.tmp.AbC123"])


class Scoped(Case):
    """janus collect UNIT --subdir PATH (scoped-collect): only one subtree of the copy is walked, on both sides; paths stay relative
    to the copy; the same git-written, verified patch; out/<unit>/changes-scoped.patch, never changes.patch; a partial recovery
    never records the unit collected. Bad paths are refused by the caller and again by the helper on the host."""
    S = "capture-rig/crates"
    def build_scoped(self, sub, h=None):
        janus.X = lambda cfg, h, script, **k: X0(cfg, h, script, **{**k, "timeout": 120})
        return janus.remote_patch(self.cfg, h or self.h, "u", janus.SCOPED_PATCH, sub)
    def scoped(self): return self.root / "out/u" / janus.SCOPED_PATCH
    def expect(self, sub):
        """base outside sub, the unit's tree inside it: what applying the scoped patch to the base must give."""
        inside = lambda r: r == sub or r.startswith(sub + "/")
        return {**{r: v for r, v in snap(self.root / "base/u").items() if not inside(r)}, **{r: v for r, v in snap(self.root / "worktrees/u").items() if inside(r)}}
    def exact_scoped(self, base, wt, sub=S):
        self.trees(base, wt); r = self.build_scoped(sub); self.assertEqual(r[0], 0, r[2])
        self.checkout(); a = self.git("apply", "--index", str(self.scoped()))
        self.assertEqual(a.returncode, 0, a.stderr.decode(errors="replace")); self.assertEqual(snap(self.root / "chk"), self.expect(sub))
        return self.scoped().read_text(errors="replace")
    def heads(self, text): return [l for l in text.splitlines() if l.startswith("diff --git ")]

    def test_only_the_subtree_is_walked_and_paths_stay_relative_to_the_copy(self):
        """Catches: a scoped recovery still walking the whole copy (an unrelated huge or unreadable tree failed or timed out the
        helper), or patch paths cut to the subtree (crates/a/... instead of capture-rig/crates/a/...)."""
        if os.geteuid() == 0: self.skipTest("root reads every folder")
        S = self.S; base = {f"{S}/a/src/lib.rs": b"1\n", "other/x": b"o\n"}
        self.trees(base, {**base, f"{S}/a/src/lib.rs": b"2\n", f"{S}/a/new.rs": b"n\n", "other/x": b"changed\n", "huge/deep/f": b"f\n"})
        locked = self.root / "worktrees/u/huge/deep"; os.chmod(locked, 0); self.addCleanup(os.chmod, locked, 0o755)
        os.mkfifo(self.root / "worktrees/u/huge/pipe")
        rc, _, err = self.build_scoped(S); self.assertEqual(rc, 0, err)
        self.assertNotIn("huge", err, "the helper looked outside the selected subtree")
        self.assertEqual(self.heads(self.scoped().read_text()), [f"diff --git a/{S}/a/new.rs b/{S}/a/new.rs", f"diff --git a/{S}/a/src/lib.rs b/{S}/a/src/lib.rs"])
        self.assertFalse(self.patch.exists(), "a scoped collect wrote changes.patch")
        rc, _, err = self.build(); self.assertNotEqual(rc, 0); self.assertIn("huge/deep", err)   # the full walk does reach it: the scoped one did not

    def test_binaries_modes_and_symlinks_in_the_subtree_arrive_exactly(self):
        S = self.S; blob = bytes(range(256)) * 40
        self.exact_scoped({f"{S}/mod.bin": blob, f"{S}/run.sh": b"#!/bin/sh\n", f"{S}/cur": ("link", "mod.bin"), "out.txt": b"1\n"},
                          {f"{S}/mod.bin": blob[:500] + b"\0X\0" + blob[500:], f"{S}/run.sh": ("x", b"#!/bin/sh\n"), f"{S}/cur": ("link", "run.sh"),
                           f"{S}/new.png": b"\x89PNG\0" + blob, f"{S}/ln": ("link", "../nowhere"), "out.txt": b"2\n"})

    def test_a_subtree_added_or_deleted_as_a_whole(self):
        """The subtree may exist on one side only: all added (new in the copy) or all deleted (removed in the copy)."""
        S = self.S
        t = self.exact_scoped({"keep": b"k\n"}, {"keep": b"k2\n", f"{S}/a/f.rs": b"f\n", f"{S}/b/x": ("x", b"#!/bin/sh\n")})
        self.assertEqual(len(self.heads(t)), 2); self.assertIn("new file mode 100755", t)
        for d in ("base", "worktrees", "chk"): shutil.rmtree(self.root / d)
        t = self.exact_scoped({"keep": b"k\n", f"{S}/a/f.rs": b"f\n", f"{S}/l": ("link", "a")}, {"keep": b"k2\n"})
        self.assertEqual(t.count("deleted file mode"), 2)

    def test_a_subtree_absent_on_both_sides_or_not_a_folder_is_refused(self):
        self.trees({"a": b"1\n", "file": b"f\n"}, {"a": b"2\n", "file": b"f\n"})
        for sub, why in (("no/such", "exists in neither"), ("file", "not a directory")):
            with self.subTest(sub=sub):
                rc, _, err = self.build_scoped(sub); self.assertEqual(rc, 1, err); self.assertIn(why, err); self.assertFalse(self.scoped().exists())

    def test_escaping_paths_are_refused_by_the_caller_and_again_on_the_host(self):
        """Catches: a path outside the copy (absolute, ..), the copy itself or an excluded folder recovered or silently widened to the
        whole copy. The host-side check is reached here directly, without the caller's."""
        self.trees({"a": b"1\n"}, {"a": b"2\n"}); (self.root / "outside").mkdir()
        for sub in ("/etc", "..", "../outside", "a/../../outside", ".", "./", "", "target", "s/.git", "x/node_modules/y"):
            with self.subTest(sub=sub):
                with self.assertRaises(janus.Err): janus.scoped_subdir(sub)
                rc, _, err = self.build_scoped(sub); self.assertEqual(rc, 1, err); self.assertIn("not a relative subdirectory", err)
                self.assertFalse(self.scoped().exists())
        self.assertEqual(janus.scoped_subdir("./capture-rig//crates/"), "capture-rig/crates")

    def test_a_symlink_on_the_selected_path_is_never_followed(self):
        """Catches: --subdir through a symlink (the selected folder or any ancestor, in the copy or the base) reading outside the copy."""
        far = self.root / "elsewhere"; (far / "b").mkdir(parents=True); (far / "b/secret").write_bytes(b"s\n")
        for side, sub, files in (("wt", "s", {"s": ("link", str(far / "b"))}), ("wt", "a/b", {"a": ("link", str(far))}),
                                 ("base", "a/b", {"a": ("link", str(far))})):
            with self.subTest(side=side, sub=sub):
                self.trees(files if side == "base" else {"k": b"1\n"}, files if side == "wt" else {"k": b"1\n"})
                rc, _, err = self.build_scoped(sub); self.assertEqual(rc, 1, err); self.assertIn("symlink", err); self.assertFalse(self.scoped().exists())
                shutil.rmtree(self.root / "base"); shutil.rmtree(self.root / "worktrees")

    def test_a_failure_keeps_the_earlier_patches(self):
        """The earlier scoped patch and the full changes.patch survive a failed scoped build; a good one replaces only its own file."""
        self.trees({"s/a": b"1\n"}, {"s/a": b"2\n", "s/b": ("link", "/etc")}); self.scoped().write_bytes(b"EARLIER SCOPED\n"); self.patch.write_bytes(b"FULL\n")
        rc, _, err = self.build_scoped("nope"); self.assertEqual(rc, 1, err)
        self.assertEqual(self.scoped().read_bytes(), b"EARLIER SCOPED\n"); self.assertEqual(self.patch.read_bytes(), b"FULL\n")
        self.assertEqual(list((self.root / "out/u").glob("*.tmp*")), [])
        self.assertEqual(self.build_scoped("s")[0], 0); self.assertIn(b"+2", self.scoped().read_bytes()); self.assertEqual(self.patch.read_bytes(), b"FULL\n")

    def test_a_remote_scoped_build_runs_in_the_same_containment(self):
        h, sd = self.fake_systemd(); self.trees({"s/a": b"1\n", "o": b"1\n"}, {"s/a": b"2\n", "o": b"2\n"})
        rc, _, err = self.build_scoped("s", h); self.assertEqual(rc, 0, err)
        args = (sd / "systemd-run.args").read_text().splitlines()
        self.assertIn("MemorySwapMax=0", args); self.assertEqual(args[-1], "s"); self.assertEqual(self.heads(self.scoped().read_text()), ["diff --git a/s/a b/s/a"])
        self.assertTrue(self.lock_free())

    # cmd_collect --subdir: a fake remote host (the real script runs here), a fake unit record; bd and every full-collect step forbidden
    def collect(self, unit="u", sub="s", rec=None):
        import argparse, contextlib, io
        u = rec or {"id": "t-1", "name": "u", "host": "far", "via": "tmux", "cwd": "/w"}; self.saved = []; self.fetched = []; t = self
        class Bd:
            def save(self, x): t.saved.append(dict(x))
            def comment(self, *a): t.saved.append(a)
            def run(self, *a): t.saved.append(a)
        def never(name): return lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"a scoped collect called {name}"))
        sh0 = janus.sh
        def sh(cmd, *a, **k):
            if cmd[0] != "rsync": return sh0(cmd, *a, **k)
            src, dst = cmd[-2].split(":", 1)[1], cmd[-1]; self.fetched.append(cmd)
            tmp = Path(dst) / ".fetch.tmp"; shutil.copyfile(src, tmp); os.replace(tmp, Path(dst) / Path(src).name); return 0, "", ""
        keep = {k: getattr(janus, k) for k in ("get_unit", "save_output", "save_transcript", "fingerprint", "collect_state", "collect_job", "sh")}
        for k, v in keep.items(): self.addCleanup(setattr, janus, k, v)
        janus.get_unit = lambda cfg, n: (Bd(), u); janus.sh = sh
        for k in ("save_output", "save_transcript", "fingerprint", "collect_state", "collect_job"): setattr(janus, k, never(k))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): janus.cmd_collect(self.cfg, argparse.Namespace(unit=unit, subdir=sub))
        return u, buf.getvalue()

    def test_collect_subdir_fetches_the_scoped_patch_and_never_records_the_unit_collected(self):
        """Catches: a partial recovery recording the unit's fingerprint (stop and close would then remove a copy whose other changes
        exist only there), writing changes.patch, settling the ledger, or printing no local path."""
        self.cfg.hosts["far"], _ = self.fake_systemd(); self.trees({"s/a": b"1\n", "o": b"1\n"}, {"s/a": b"2\n", "o": b"2\n"})
        u, o = self.collect()
        loc = (self.cfg.p("out", "out") / "u" / janus.SCOPED_PATCH).resolve()
        self.assertEqual(loc.read_bytes(), self.scoped().read_bytes()); self.assertIn(str(loc), o); self.assertIn("scoped patch of s", o)
        self.assertIn("NOT recorded collected", o); self.assertEqual(self.saved, [], "the unit record or bd was written")
        self.assertNotIn("collected", u); self.assertNotIn("collected_at", u)
        self.assertFalse(self.patch.exists()); self.assertFalse((self.cfg.p("out", "out") / "u/changes.patch").exists())
        janus.fingerprint = lambda cfg, u: {"fp": "abc 2|x", "n": "3", "p": "1"}
        self.assertIn("not collected", janus.uncollected(self.cfg, u), "stop and close would no longer refuse")

    def test_collect_subdir_refuses_a_job_a_local_unit_and_a_bad_path_before_running_anything(self):
        janus.X = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a host command ran"))
        for unit, sub, rec, why in (("far:12", "s", None, "not a job"), ("u", "s", {"id": "t", "name": "u", "host": "here", "via": "herdr"}, "local unit"),
                                    ("u", "../x", None, "relative subdirectory"), ("u", "/abs", None, "relative subdirectory"),
                                    ("u", "s", {"id": "t", "name": "u", "host": "far", "via": "tmux", "closing": True}, "being closed")):
            with self.subTest(unit=unit, sub=sub):
                self.cfg.hosts["far"] = {"root": str(self.root), "ssh": "far"}
                with self.assertRaises(janus.Err) as e: self.collect(unit, sub, rec)
                self.assertIn(why, str(e.exception)); self.assertEqual(self.fetched, []); self.assertEqual(self.saved, [])

    def test_collect_subdir_failure_keeps_the_earlier_local_scoped_patch(self):
        self.cfg.hosts["far"], _ = self.fake_systemd(); self.trees({"a": b"1\n"}, {"a": b"2\n"})
        loc = self.cfg.p("out", "out") / "u" / janus.SCOPED_PATCH; loc.parent.mkdir(parents=True, exist_ok=True); loc.write_bytes(b"EARLIER\n")
        with self.assertRaises(janus.Err) as e: self.collect(sub="missing")
        self.assertIn("exists in neither", str(e.exception)); self.assertIn("kept", str(e.exception))
        self.assertEqual(loc.read_bytes(), b"EARLIER\n"); self.assertEqual(self.fetched, []); self.assertEqual(self.saved, [])


if __name__ == "__main__":
    unittest.main()
