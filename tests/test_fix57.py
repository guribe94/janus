"""skill-fix57: the pipeline never stalls in silence (project ISSUES row 144, decision 0034) and a collected unit's result is
presented. One test per failure, each naming what it catches. Standard library only; no network, no spend. Run by ./check."""
import contextlib, io, os, subprocess, time, unittest
from unittest import mock

from test_janus import janus, project

LIN = '\n[hosts.lin]\nssh = "lin"\nroot = "/r"\n'


def git(d, *a): return subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True, text=True).stdout.strip()


def starved(cfg, d, mins=40, lin_busy=False):
    """A land of u1 whose gate job waits on `here` (admission paused, another gate in front) while `lin` has a landing lane."""
    janus.note_land(cfg, "u1", pid=os.getpid(), t=time.time() - mins * 60, host="here", wt=str(d))
    cfg.jput("hosts-facts.json", {"lin": {"features": ["git"]}})
    q = {"Queued": {"enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - mins * 60))}}
    task = lambda i, unit, st: {"id": i, "group": "gates", "label": janus.tag(cfg, f"unit:{unit}"), "status": st, "command": ""}
    g = {"gates": {"status": "Running", "parallel_tasks": 1}}
    return {"here": {"ok": True, "cap_pause": "PSI 50", "cap_admit": "no", "pueue": {"groups": g, "tasks": {"5": task(5, "a103", {"Running": {}}), "7": task(7, "u1", q)}}},
            "lin": {"ok": True, "cap_admit": "yes", "pueue": {"groups": g, "tasks": {"1": task(1, "zz", q)} if lin_busy else {}}}}


class Stall(unittest.TestCase):
    def test_a_starved_land_is_a_finding_that_names_the_idle_host_and_the_queue(self):
        """Catches (row 144): a land sat ~6 h behind one admission-paused host while two hosts idled and no finding fired; the
        check never said what the paused host's queue held or that an idle host could take the land."""
        d, cfg = project(LIN); F = dict(janus.stall_findings(cfg, starved(cfg, d)))
        t = F.get("land:u1:stalled", "")
        self.assertIn("janus land u1 --host lin --restart", t)        # the one recovery command, on the idle host
        self.assertIn("PSI 50", t); self.assertIn("here:7", t)        # why it is starved, and which job
        self.assertIn("unit:a103", t)                                 # queue transparency: what is in front of it there
        self.assertEqual(janus.stall_findings(cfg, starved(cfg, d, lin_busy=True)), [])   # no idle host, under the wall bound: quiet
        F = dict(janus.stall_findings(cfg, starved(cfg, d, mins=500, lin_busy=True)))     # the wall bound alone ([landing] stall_min)
        self.assertIn("500 min", F.get("land:u1:stalled", ""))
        self.assertEqual(janus.family("land:u1:stalled"), "land"); self.assertTrue(janus.FAMILIES["land"].urgent)

    def test_the_watcher_restarts_a_starved_land_only_when_its_merge_is_committed(self):
        """Catches (row 144): recovery waited for the owner to order it. The watcher re-runs a starved land on the idle host
        when the worktree holds the merge as a commit, reports it as a finding, and never touches a tree with open changes."""
        d, cfg = project(LIN); git(d, "init", "-q"); (d / "f").write_text("1"); git(d, "add", "f"); git(d, "commit", "-qm", "a")
        hs = starved(cfg, d); ran = []; log = []
        with mock.patch.object(janus, "spawn_land", lambda cfg, u, h: ran.append((u, h)) or 4242), mock.patch.object(janus, "land_proc", lambda pid, u: True):
            (d / "f").write_text("2")                                  # an uncommitted change: not provably safe
            janus.tend_stalled_lands(cfg, hs, [], log.append)
            self.assertEqual(ran, []); self.assertIn("uncommitted", dict(janus.stall_findings(cfg, hs))["land:u1:stalled"])
            git(d, "commit", "-qam", "b")
            janus.tend_stalled_lands(cfg, hs, [], log.append); janus.tend_stalled_lands(cfg, hs, [], log.append)
        self.assertEqual(ran, [("u1", "lin")])                         # once, not every pass
        self.assertIn("land:u1:recovered", dict(janus.stall_findings(cfg, hs)))   # never silent


class Install(unittest.TestCase):
    def test_a_pending_release_that_changes_the_blocked_steps_code_is_named(self):
        """Catches (row 144): the published fix for the stalled land stayed uninstalled because reinstall waited for that land
        to end; only a standing note said so."""
        d, cfg = project(LIN); src = d / "src"; (src / "janus/actions").mkdir(parents=True); git(src, "init", "-q")
        (src / "a").write_text("1"); git(src, "add", "a"); git(src, "commit", "-qm", "a"); rev = git(src, "rev-parse", "--short", "HEAD")
        vf = d / "VERSION"; vf.write_text(f"v1\nfrom {src}\n{rev}\n")
        stalled = [("land:u1:stalled", "x")]
        self.assertEqual(janus.install_findings(cfg, stalled, vf), [])            # nothing pending
        (src / "a").write_text("2"); git(src, "commit", "-qam", "docs only")
        self.assertEqual(janus.install_findings(cfg, stalled, vf), [])            # pending, but not the blocked step's code
        (src / "janus/actions/landing.py").write_text("x"); git(src, "add", "."); git(src, "commit", "-qm", "land on any host")
        self.assertEqual(janus.install_findings(cfg, [], vf), [])                 # nothing blocked
        F = dict(janus.install_findings(cfg, stalled, vf))
        self.assertIn("land on any host", F.get("install:blocked-by-blocker", "")); self.assertIn("install --release-only", F["install:blocked-by-blocker"])


class Present(unittest.TestCase):
    def test_a_collected_unit_is_a_finding_until_its_substance_was_presented(self):
        """Catches (owner instruction): units were collected and closed with their results only written to files; the owner
        was never told what was proved, what is left and what is asked of them."""
        d, cfg = project(); janus.note_collected(cfg, "u1", "collect")
        self.assertIn("present:u1", dict(janus.present_findings(cfg)))
        self.assertFalse(janus.ackable("present:u1"))                             # only presenting clears it
        with self.assertRaises(janus.Err): janus.mark_presented(cfg, "u1", "ok")   # a tick is not a presentation
        janus.mark_presented(cfg, "u1", "u1 proved the seal reads each byte once; left: the Mac gate; ask: none")
        janus.note_collected(cfg, "u1", "close")                                  # closing what was presented asks nothing again
        self.assertEqual(janus.present_findings(cfg), [])


if __name__ == "__main__": unittest.main()
