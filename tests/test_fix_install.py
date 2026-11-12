"""fix-install items 7-14: the issues found installing janus on a live project, each test naming the failure it catches.
Standard library only; no network, no API spend."""
import contextlib, io, json, os, re, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path
from unittest import mock

from janusload import load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-fix-"))
    (d / "janus.toml").write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        ''') + textwrap.dedent(extra))
    (d / "out").mkdir(); (d / "briefs").mkdir()
    return d, janus.Cfg(d)


def probe_of(**k):
    return {"ok": True, "res": [], "hist": [], "sched": [], "direct": [], "pueue": {"tasks": {}}, "cap_pause": "", "cap_admit": "yes", "tmux": [], **k}


class Item11Verdict(unittest.TestCase):
    """Item 11: WAITING/PENDING is never a final verdict; a recorded park wins over a verdict."""
    def test_waiting_and_pending_are_not_final_verdicts(self):
        """Catches: units.verdict/verdict_line excluded only PENDING, so a report ending 'VERDICT: WAITING' read as finished and
        its jobs were cancelled as orphans."""
        d, cfg = project()
        (d / "out/u1").mkdir(parents=True); (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nbody\nWAITING here:1\nVERDICT: WAITING\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        self.assertEqual(janus.verdict_line({"verdict": "WAITING"}), (None, True))
        self.assertEqual(janus.verdict_line({"verdict": "PENDING"}), (None, True))
        self.assertEqual(janus.verdict_line({"verdict": "DONE"}), ("VERDICT: DONE", True))

    def test_a_recorded_park_wins_over_a_verdict(self):
        """Catches: a unit whose run was resumed and then parked was judged finished (its jobs became orphan:job)."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True); (d / "out/u1/REPORT.md").write_text("VERDICT: DONE\nall good\n")
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "parked": {"jobs": ["here:9"]}}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u["state"], "parked"); self.assertEqual(u["_park"], ["here:9"])
        self.assertNotIn("unit:u1:finished", [k for k, _ in F])

    def test_remote_ended_unit_report_is_synced_before_orphan_judging(self):
        """Catches (item 11 addendum): a remote unit's local report has no WAITING line until collect, so check judged it orphaned
        and its jobs were cancelled."""
        d, cfg = project("\n[hosts.far]\nssh = \"far\"\nroot = \"/home/x\"\n")
        (d / "out/u1").mkdir(parents=True); (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\n")
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "started": 0, "state": "done"}
        hs = {"far": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        real = janus.save_output
        janus.save_output = lambda cfg, u: ((d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nWAITING far:9\n"), True)[1]
        try:
            janus.sync_remote_outputs(cfg, [u], type("B", (), {"save": lambda self, u: None})())
            F, _ = janus.unit_findings(cfg, hs, [u], {})
        finally: janus.save_output = real
        self.assertEqual(u["_park"], ["far:9"]); self.assertEqual(u["state"], "parked")
        self.assertNotIn("orphan:job:far:9", [k for k, _ in janus.orphan_job_findings(cfg, hs, [u], set())])
        import inspect
        self.assertIn("sync_remote_outputs(cfg, units, bd)", inspect.getsource(janus.collect_findings))


class Item7CommitState(unittest.TestCase):
    def test_a_file_that_vanishes_mid_commit_does_not_fail_the_commit(self):
        """Catches: janus commit-state failed with 'git add failed: fatal: unable to stat ...' when a monitor deleted a temp file
        between listing and add."""
        d = Path(tempfile.mkdtemp(prefix="janus-fix-")); (d / "janus.toml").write_text('[project]\nname = "t"\n'); (d / "out").mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True, check=True)
        g("init", "-q"); (d / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
        (d / "b").write_text("2\n")   # the file the commit should still make
        cfg = janus.Cfg(d); calls = []
        real_sh, real_push = janus.sh, janus.push_cmd
        def sh_stub(cmd, **k):
            calls.append(list(cmd))
            if cmd == ["git", "add", "-A"]: return 128, "", "fatal: unable to stat b.tmp: No such file or directory"
            return real_sh(cmd, **k)
        janus.sh = sh_stub; janus.push_cmd = lambda repo, cfg=None: ["true"]
        try: janus.cmd_commit_state(cfg, type("A", (), {"message": "state"})())
        finally: janus.sh, janus.push_cmd = real_sh, real_push
        self.assertEqual(g("log", "-1", "--format=%s").stdout.strip(), "state")
        self.assertTrue(any(c[:4] == ["git", "add", "-A", "--ignore-errors"] for c in calls), "the add was not retried")


class Item7WastePause(unittest.TestCase):
    def test_an_ack_of_waste_pause_keeps_launch_unpaused(self):
        """Catches: waste_paused returned None while its ack was live, so the finding was gone and the next check dropped the ack
        ('finding gone') and re-paused launch and fill."""
        d, cfg = project()
        cfg.jput("acks.json", {"waste-pause": {"t": janus.now(), "d": "cause found"}})
        real = janus.waste_state; janus.waste_state = lambda cfg, outs=None: (10.0, 20.0, 50.0)
        try:
            for _ in range(2):
                self.assertTrue(janus.waste_paused(cfg), "waste_paused hid the reason while its ack was live")
                keys = {k for k, _ in janus.report_findings(cfg, [])}
                self.assertIn("waste-pause", keys)
                self.assertIn("waste-pause", janus.live_acks(cfg, keys), "the ack was dropped as 'finding gone'")
                self.assertIsNone(janus.waste_block(cfg), "launch is refused although waste-pause is acked")
        finally: janus.waste_state = real


if __name__ == "__main__": unittest.main()
