"""Checks for sandbox/run: a command run through the sandbox may write in its worktree and its output folder,
may not write in a sibling folder or in $HOME, and may not read a path named in the deny list. The class is
skipped, with the reason, where the tool the sandbox needs is missing (sandbox-exec on macOS, bwrap on Linux).
Standard library only; no network. Run with `python3 -m unittest tests.test_sandbox` (or by discovery)."""
import os, shlex, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
RUN = HERE / "sandbox" / "run"
SECRET = "sandbox-deny-secret"


def sandbox_tool():
    """The tool sandbox/run needs on this OS, or None where there is no sandbox for it."""
    if sys.platform == "darwin":
        return "sandbox-exec"
    if sys.platform.startswith("linux"):
        return "bwrap"
    return None


class Sandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not RUN.is_file():
            raise unittest.SkipTest(f"{RUN} is missing")
        tool = sandbox_tool()
        if tool is None:
            raise unittest.SkipTest(f"no sandbox tool for this OS ({sys.platform})")
        if shutil.which(tool) is None:
            raise unittest.SkipTest(f"{tool} is not installed: cannot exercise the sandbox")
        cls.root = Path(tempfile.mkdtemp(prefix="sandbox-test-"))
        cls.worktree = cls.root / "worktree"
        cls.outdir = cls.root / "out"
        cls.tmp = cls.root / "tmp"
        cls.sibling = cls.root / "sibling"
        cls.deny = cls.root / "deny"
        for d in (cls.worktree, cls.outdir, cls.tmp, cls.sibling, cls.deny):
            d.mkdir()
        cls.secret = cls.deny / "secret.txt"
        cls.secret.write_text(SECRET + "\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "root", None), ignore_errors=True)

    def sandbox(self, script):
        """Run `sh -c script` through sandbox/run. TMPDIR points at our own dir, so the only writable temp is
        ours and the sibling below is outside every writable path; the deny list names our secret file."""
        env = dict(os.environ)
        env["TMPDIR"] = str(self.tmp)
        env["SANDBOX_DENY"] = str(self.secret)
        env.pop("SANDBOX_WRITE", None)
        return subprocess.run([str(RUN), str(self.worktree), str(self.outdir), "--", "sh", "-c", script],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_writes_in_the_worktree_and_output_folder(self):
        for where in (self.worktree, self.outdir):
            target = where / "written.txt"
            r = self.sandbox(f"printf ok > {shlex.quote(str(target))}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(target.read_text(), "ok")

    def test_cannot_write_in_a_sibling_folder(self):
        target = self.sibling / "written.txt"
        r = self.sandbox(f"printf no > {shlex.quote(str(target))}")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(target.exists())

    def test_cannot_write_in_home(self):
        target = Path.home() / f".sandbox-test-{os.getpid()}"
        try:
            r = self.sandbox(f"printf no > {shlex.quote(str(target))}")
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse(target.exists())
        finally:
            try:
                target.unlink()
            except FileNotFoundError:
                pass

    def test_cannot_read_a_denied_file(self):
        r = self.sandbox(f"cat {shlex.quote(str(self.secret))}")
        self.assertNotIn(SECRET, r.stdout)   # macOS denies the read; bwrap hides the file behind /dev/null

    def test_can_read_an_allowed_file(self):
        allowed = self.worktree / "readable.txt"
        allowed.write_text("read me\n")
        r = self.sandbox(f"cat {shlex.quote(str(allowed))}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "read me\n")


if __name__ == "__main__":
    unittest.main()
