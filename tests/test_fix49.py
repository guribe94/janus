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


    def test_a_dispatcher_gate_is_not_held_by_the_coordinators_admission_but_other_work_is(self):
        """Catches the Linux final gate waiting on the Mac: land's `janus gate` dispatcher (landing.gate_is_dispatcher) was admitted
        as a build on the coordinator, so a coordinator under its disk floor held a gate whose steps run on other hosts (exit 121
        never admitted). Real work stays refused there, and a plain gate command is no dispatcher."""
        from janus.actions import landing
        d, cfg = project("\n[capacity]\nmin_disk_gb = 100000000\n")   # the coordinator is below its disk floor
        h = cfg.hosts["here"]; janus.write_cap(cfg, "here", h); wt = tempfile.mkdtemp(prefix="janus-wt-")
        env = {**os.environ, "JANUS_POLL_S": "1", "JANUS_ADMIT_MAX_S": "3", "JANUS_ADMIT_REPORT_S": "1"}
        def run(lane, cmd, **k):
            ra = argparse.Namespace(host="here", lane=lane, unit="u", wt=wt, subdir=None, usb=False, priority=10, no_wait=False, allow_unmerged=False, command=[cmd], **k)
            p = subprocess.run(["bash"], input=janus.job_script(cfg, "here", h, ra, "u", cmd, "u-" + lane), capture_output=True, text=True, timeout=60, env=env)
            return p.returncode, p.stdout + p.stderr
        rc, out = run("gates", "echo GATE-STEP-REACHED", dispatcher=landing.gate_is_dispatcher("janus gate land-u --wait"))
        self.assertEqual(rc, 0, out); self.assertIn("GATE-STEP-REACHED", out)
        self.assertEqual(run("gates", "echo X", dispatcher=landing.gate_is_dispatcher("cargo test"))[0], 121, "a plain gate command was exempted")
        rc, out = run("jobs", "echo X"); self.assertEqual(rc, 121, out); self.assertNotIn("X\n", out.replace("echo X", ""))   # real work still blocked
        self.assertEqual(run("jobs", "echo X", dispatcher=True)[0], 121, "the dispatcher flag exempted a non-gate lane")


if __name__ == "__main__":
    unittest.main()
