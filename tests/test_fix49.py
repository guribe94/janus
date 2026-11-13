"""skill-fix49 (ISSUES row 243): janus land's final gate died before any test. The test names the failure it catches.
Standard library only; no network, no spend. Run by ./check (it discovers tests/test_*.py)."""
import argparse, os, subprocess, sys, tempfile, unittest
from pathlib import Path

import janusload
from test_janus import janus, project


class LandGateJobFindsTheRoot(unittest.TestCase):
    def test_a_janus_command_in_a_job_whose_worktree_is_outside_the_root_finds_the_project(self):
        """Catches row 243: janus land queues its final gate (`janus gate ...`) as a job in the unit's worktree, which lies outside
        the project root; the job's environment named no root, so the gate ended 'no janus.toml here or in a parent' (exit 2)
        after its whole admission wait and no test ran. Every land failed."""
        d, cfg = project("\n[capacity]\nmin_disk_gb = 0\n")
        h = cfg.hosts["here"]; janus.write_cap(cfg, "here", h)
        wt = tempfile.mkdtemp(prefix="janus-wt-")   # the unit's worktree: not under the project root
        gc = f"{sys.executable} {janus.JANUS_SELF} gate land-u --unit u --wait"
        ra = argparse.Namespace(host="here", lane="gates", unit="u", wt=wt, subdir=None, usb=False, priority=10, no_wait=False,
                                allow_unmerged=False, command=[gc])   # the job cmd_land queues
        env = {k: v for k, v in os.environ.items() if k not in ("JANUS_ROOT", "JANUS_UNIT")}   # land runs from the root, with no JANUS_ROOT
        p = subprocess.run(["bash"], input=janus.job_script(cfg, "here", h, ra, "u", gc, "u-1"), capture_output=True, text=True, timeout=60,
                           env={**env, "JANUS_POLL_S": "1", "JANUS_ADMIT_MAX_S": "20"})
        out = p.stdout + p.stderr
        self.assertNotIn("no janus.toml", out, "the gate command found no project root from the unit's worktree")
        self.assertIn(str(d.resolve() / "out" / "land-u" / "tests.sh"), out, "the gate did not look in the project that queued it")   # no such gate here: its own error


if __name__ == "__main__":
    unittest.main()
