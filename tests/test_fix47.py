"""skill-fix47 (ISSUES rows 226-230): the result cache of janus run --inputs, the gate-log step logs, the instructions ack,
ready items without briefs (the finding, janus brief draft/accept). One test per failure, each naming what it catches.
Standard library only; no network, no spend. Run by ./check (it discovers tests/test_*.py)."""
import argparse, contextlib, io, json, subprocess, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project


def git(wt, *a): subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)


def tree(d):
    wt = d / "wt"; (wt / "src").mkdir(parents=True); (wt / "other").mkdir()
    (wt / "src/a.txt").write_text("1\n"); (wt / "other/b.txt").write_text("1\n"); (wt / "lock").write_text("L\n")
    git(wt, "init", "-q"); git(wt, "add", "-A"); git(wt, "commit", "-qm", "one")
    return wt


class ResultCache(unittest.TestCase):
    def run_job(self, cfg, wt, exits, no_wait=False, no_cache=False, inputs=("src", "lock"), cmd="make test"):
        """One janus run with pueue faked: (output, jobs queued so far). exits: the exit code each queued job ends with."""
        a = argparse.Namespace(host="here", lane="jobs", unit="u1", wt=str(wt), subdir=None, usb=False, priority=0, no_wait=no_wait,
                               allow_unmerged=True, command=["--", cmd], broad=None, needs=None, inputs=list(inputs), no_cache=no_cache)
        def fake_hrun(h, script, **k):
            if " add " in script: self.queued.append(script); return 0, str(len(self.queued)), ""
            if script.startswith("cat ") and ".rc" in script:   # the job's exit code file on its host
                return (0, str(exits[len(self.queued) - 1]), "") if self.queued else (1, "", "")
            return 0, "", ""
        buf = io.StringIO()
        with mock.patch.object(janus, "hrun", fake_hrun), mock.patch.object(janus, "tree_guard", lambda *a, **k: None), \
             mock.patch.object(janus, "ensure_lanes", lambda h, cfg: ""), mock.patch.object(janus, "write_cap", lambda *a, **k: (0, "", "")), \
             mock.patch.object(janus, "host_facts", lambda cfg, refresh=False: {"here": {"os": "linux", "arch": "x86_64", "features": [], "devices": []}}), \
             mock.patch.object(janus, "finish_job", lambda cfg, hn, h, tid, jid, quiet=False: exits[int(tid) - 1]), \
             contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = janus.cmd_run(cfg, a)
        return rc, buf.getvalue()

    def test_a_job_whose_inputs_already_passed_is_not_run_again_until_an_input_changes(self):
        """Catches: the same command run twice on the same inputs (two gates of one tree ran the same test step twice); and a
        cache that would answer PASS after an input changed, or for a path outside the declared inputs' effect."""
        d, cfg = project(); wt = tree(d); self.queued = []
        rc, out = self.run_job(cfg, wt, [0]); self.assertEqual((rc, len(self.queued)), (0, 1))
        rc, out = self.run_job(cfg, wt, [0])
        self.assertEqual(len(self.queued), 1, "the same job on the same inputs was queued again")
        self.assertEqual(rc, 0); self.assertRegex(out, r"^CACHED PASS [0-9a-f]{16} from here:1 \d{4}-\d\d-\d\d \d\d:\d\d\n$")
        (wt / "other/b.txt").write_text("2\n")   # not an input: still cached
        self.run_job(cfg, wt, [0]); self.assertEqual(len(self.queued), 1, "a file outside the inputs ran the job again")
        (wt / "src/a.txt").write_text("2\n")     # a dirty input: runs again
        self.run_job(cfg, wt, [0, 0]); self.assertEqual(len(self.queued), 2, "a changed input answered CACHED PASS")
        self.run_job(cfg, wt, [0, 0]); self.assertEqual(len(self.queued), 2, "the dirty tree's pass was not kept")
        git(wt, "commit", "-qam", "two")         # committed: a new key (git's tree hash), runs once more
        self.run_job(cfg, wt, [0, 0, 0]); self.assertEqual(len(self.queued), 3)
        self.run_job(cfg, wt, [0, 0, 0, 0], cmd="make other"); self.assertEqual(len(self.queued), 4, "another command answered CACHED PASS")
        self.run_job(cfg, wt, [0, 0, 0, 0, 0], no_cache=True); self.assertEqual(len(self.queued), 5, "--no-cache did not force a run")

    def test_a_failure_is_never_cached_and_a_detached_pass_is(self):
        """Catches: a failed job answered from the cache (a red step turning green unrun), and a detached job (its exit code
        known only on its host, later) never counting as a pass."""
        d, cfg = project(); wt = tree(d); self.queued = []
        self.run_job(cfg, wt, [1]); self.run_job(cfg, wt, [1, 0])
        self.assertEqual(len(self.queued), 2, "a failed job was answered from the cache")
        self.queued = []; (wt / "src/a.txt").write_text("3\n")
        rc, out = self.run_job(cfg, wt, [0], no_wait=True); self.assertEqual(out, "here:1\n")
        rc, out = self.run_job(cfg, wt, [0], no_wait=True)
        self.assertEqual(len(self.queued), 1, "a detached job that passed was run again"); self.assertIn("CACHED PASS", out)
        self.queued = []; (wt / "src/a.txt").write_text("4\n")
        self.run_job(cfg, wt, [7], no_wait=True); self.run_job(cfg, wt, [7, 0], no_wait=True)
        self.assertEqual(len(self.queued), 2, "a detached job that failed was answered from the cache")
        self.assertLessEqual(len(list(cfg.f("cache").glob("*.json"))), 3)

    def test_the_store_is_bounded_and_the_toolchain_is_in_the_key(self):
        """Catches: a cache that grows without bound, and a pass kept across a toolchain change."""
        d, cfg = project("\n[cache]\nmax = 3\ntoolchain = \"rust 1\"\n"); wt = tree(d)
        hh = janus.input_hashes(wt, ["src"]); f = {"os": "linux", "arch": "x86_64"}
        k1 = janus.cache_key(cfg, "here", f, "c", None, hh); cfg.c["cache"]["toolchain"] = "rust 2"
        self.assertNotEqual(k1, janus.cache_key(cfg, "here", f, "c", None, hh))
        self.assertNotEqual(k1, janus.cache_key(cfg, "here", {"os": "darwin", "arch": "arm64"}, "c", None, hh))
        from janus import cache
        for i in range(6): cache.record(cfg, f"k{i}", "here", i, f"j{i}", "c")
        self.assertEqual(len(list(cfg.f("cache").glob("*.json"))), 3)
        self.assertIsNone(janus.input_hashes(d, ["x"]), "a tree that is no git checkout must cache nothing")


if __name__ == "__main__":
    unittest.main()
