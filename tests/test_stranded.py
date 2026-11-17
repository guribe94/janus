"""A host down for long (or marked gone by the owner): its open units stop covering their backlog items, janus fill relaunches
them on the hosts that answer, seeded from the stranded unit's report and backup.patch, and the two are paired when the host
returns. The owner's marks (janus hosts --mark). Standard library only; bd is a fake, git runs in temporary repositories. Each
test names the failure it catches."""
import argparse, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project, host_probe, JANUS_PY
from test_dispatch import World, CHECK, CONF, out

FAR = '\n[hosts.far]\nssh = "nowhere"\nroot = "/x"\nram_gb = 64\ncores = 16\n'


def git(d, *a): return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project(CONF + FAR); (self.d / "S.md").write_text(CHECK); self.cfg.hosts["here"].update(ram_gb=64, cores=16)
        self.w = World(); self.w.patch(self)
        self.hs = {"here": host_probe(name="here"), "far": {"name": "far", "ok": False, "err": "ssh: connect to host far port 22: Host is down"}}
    def down(self, host, minutes): self.cfg.jput("hosts.json", {**self.cfg.jget("hosts.json"), host: time.time() - minutes * 60})
    def mark(self, host, m): self.cfg.jput("host-marks.json", {**self.cfg.jget("host-marks.json"), host: {"mark": m, "at": time.time()}})
    def brief(self, name, item, extra=""): (self.d / f".janus/briefs/{name}.md").write_text(f"# {name}\nITEM: {item}\n{extra}")
    def stranded(self, name="x1-a", item="X1", host="far", **k):
        """An open unit on host for item, with its brief and its report copied here."""
        self.brief(name, item); (self.d / f".janus/out/{name}").mkdir(parents=True, exist_ok=True)
        (self.d / f".janus/out/{name}/REPORT.md").write_text("VERDICT: DONE\nthe old work\n")
        u = {"id": f"u-{name}", "title": f"unit:{name}", "name": name, "host": host, "item": item, "brief": str(self.d / f".janus/briefs/{name}.md"),
             "started": int(time.time()) - 7200, "kind": "implement", "tier": "normal", "via": "pueue", **k}
        self.w.units.append(u); return u


class Liveness(Base):
    def test_the_clock_and_the_marks(self):
        """Catches: a host stranded at its first missed probe (a reboot relaunches everything), never stranded however long it is
        down, a gone host waiting for the clock, a returning host relaunched anyway, the per-host setting ignored."""
        L = lambda: {n: s for n, (s, _) in janus.host_liveness(self.cfg).items()}
        self.assertEqual(L(), {"here": "up", "far": "up"})
        self.down("far", 29); self.assertEqual(L()["far"], "down")
        self.down("far", 31); self.assertEqual(L()["far"], "stranded"); self.assertEqual(janus.stranded_hosts(self.cfg), {"far"})
        self.mark("far", "returning"); self.assertEqual(L()["far"], "returning"); self.assertEqual(janus.stranded_hosts(self.cfg), set())
        self.mark("here", "gone"); self.assertEqual(L()["here"], "gone")   # the owner's word: whether it answers or not
        self.cfg.jput("host-marks.json", {}); self.cfg.hosts["far"]["down_after_min"] = 60; self.assertEqual(L()["far"], "down")
        self.cfg.capc["down_after_min"] = 5; del self.cfg.hosts["far"]["down_after_min"]; self.assertEqual(L()["far"], "stranded")

    def test_a_returning_host_that_answers_loses_its_mark(self):
        """Catches: a returning mark that outlives the outage, so the next long outage of that host never relaunches anything."""
        self.mark("far", "returning"); self.mark("here", "gone"); self.down("far", 90)
        with mock.patch.object(janus, "probe", lambda n, h, ro=False, cfg=None: {"name": n, "ok": True}), mock.patch.object(janus, "host_facts", lambda *a, **k: {}):
            janus.probe_all(self.cfg)
        self.assertEqual(self.cfg.jget("host-marks.json").keys(), {"here"}); self.assertEqual(self.cfg.jget("hosts.json"), {})

    def test_mark_from_the_command_line(self):
        """Catches: --mark missing, writing a mark for a host janus does not know, an unknown mark kind, clear not clearing."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("JANUS_")}
        run = lambda *a: subprocess.run([sys.executable, str(JANUS_PY), "hosts", "--mark", *a], cwd=self.d, env=env, capture_output=True, text=True, timeout=60)
        r = run("far", "gone"); self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("far marked gone; now gone", r.stdout)
        self.assertEqual(self.cfg.jget("host-marks.json")["far"]["mark"], "gone")
        r = run("nope", "gone"); self.assertEqual(r.returncode, 1); self.assertIn("no host 'nope'", r.stderr)
        r = run("far", "dead"); self.assertEqual(r.returncode, 1); self.assertIn("gone, returning or clear", r.stderr)
        r = run("far", "clear"); self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(self.cfg.jget("host-marks.json"), {})


class Redirect(Base):
    def test_a_stranded_units_item_is_relaunched_under_a_new_name(self):
        """Catches: an item covered for days by a unit on a dead host (fill never relaunches it), the relaunch reusing the spent unit
        name, the item relaunched while its host is only briefly down or marked returning."""
        self.w.issue("X1"); self.stranded()
        self.assertEqual(janus.fill_candidates(self.cfg, self.w.units), [])   # up: covered
        self.down("far", 10); self.assertEqual(janus.fill_candidates(self.cfg, self.w.units), [])   # briefly down: it waits
        self.down("far", 45)
        c = janus.fill_candidates(self.cfg, self.w.units)
        self.assertEqual([(x[2], x[4], Path(x[3]).name) for x in c], [("X1", "x1-a-r2", "x1-a.md")])
        self.mark("far", "returning"); self.assertEqual(janus.fill_candidates(self.cfg, self.w.units), [])
        self.mark("far", "gone"); self.assertEqual([x[4] for x in janus.fill_candidates(self.cfg, self.w.units)], ["x1-a-r2"])
        F = dict(janus.backlog_findings(self.cfg, self.hs, self.w.units)); self.assertIn("x1-a-r2", F.get("idle-with-backlog", ""))   # the watcher's fill trigger

    def test_relaunch_names(self):
        """Catches: a relaunch name over 32 characters, one already spent, one whose brief the orchestrator already wrote."""
        self.assertEqual(janus.relaunch_name(self.cfg, "a80-session-acceptance-r2", set()), "a80-session-acceptance-r3")
        self.assertEqual(janus.relaunch_name(self.cfg, "q", {"q-r2"}), "q-r3")
        n = janus.relaunch_name(self.cfg, "x" * 32, set()); self.assertLessEqual(len(n), 32); self.assertTrue(n.endswith("-r2"))
        self.brief("q-r2", "Q"); self.assertEqual(janus.relaunch_name(self.cfg, "q", set()), "q-r3")

    def test_a_live_unit_still_covers_and_a_new_brief_wins(self):
        """Catches: an item relaunched although another open unit on a live host covers it; the stranded brief preferred over a brief
        the orchestrator wrote for the item."""
        self.w.issue("X1"); self.stranded(); self.down("far", 45)
        self.w.units.append({"id": "u-l", "title": "unit:live", "name": "live", "host": "here", "item": "X1", "brief": "/none"})
        self.assertEqual(janus.fill_candidates(self.cfg, self.w.units), [])
        self.w.units.pop(); self.brief("x1-new", "X1")
        self.assertEqual([x[4] for x in janus.fill_candidates(self.cfg, self.w.units)], ["x1-new"])

    def test_stranded_units_hold_no_cap_slot(self):
        """Catches: the dead host's units counted against [caps] total, so the hosts that answer get no new work."""
        self.cfg.caps["total"] = 1; units = [{"name": "s", "host": "far"}]
        self.assertEqual(janus.room(self.cfg, "here", self.hs["here"], units)[0], 0)   # far up: the one slot is taken
        self.down("far", 45); self.assertGreaterEqual(janus.room(self.cfg, "here", self.hs["here"], units)[0], 1)
        self.assertNotIn("cap:total", dict(janus.cap_findings(self.cfg, units + [{"name": "t", "host": "here"}])))

    def test_fill_launches_the_relaunch_seeded_from_the_original(self):
        """Catches: the relaunch started from nothing (no --resume-of), without its brief under its own name, or not told why."""
        self.w.issue("X1"); self.stranded(saved_at=int(time.time()) - 3000); self.down("far", 45); launched = []
        with mock.patch.object(janus, "probe_all", lambda cfg: self.hs), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "cmd_launch", lambda cfg, a: launched.append(a)), mock.patch.object(janus, "fill_repo", lambda *a: None):
            r, o = out(janus.cmd_fill, self.cfg, type("A", (), {"max": None})())
        self.assertEqual(r, 0, o); self.assertIn("relaunch of x1-a (far stranded)", o)
        a = launched[0]; self.assertEqual((a.unit, a.resume_of, a.relaunch_of, a.item), ("x1-a-r2", "x1-a", "x1-a", "X1"))
        b = Path(a.brief); self.assertEqual(b, self.d / ".janus/briefs/x1-a-r2.md"); t = b.read_text()
        self.assertTrue(t.startswith("# x1-a\nITEM: X1"), t); self.assertIn("## Relaunch of x1-a", t); self.assertIn("far, which is stranded", t)

    def test_another_brief_for_the_item_is_not_seeded_from_the_stranded_unit(self):
        """Catches: a new brief the orchestrator wrote for an item (other work) started with a stranded unit's report and patch mixed
        into its tree."""
        self.w.issue("X1"); self.stranded(); self.down("far", 45); self.brief("x1-other", "X1"); launched = []
        with mock.patch.object(janus, "probe_all", lambda cfg: self.hs), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "cmd_launch", lambda cfg, a: launched.append(a)), mock.patch.object(janus, "fill_repo", lambda *a: None):
            r, o = out(janus.cmd_fill, self.cfg, type("A", (), {"max": None})())
        a = launched[0]; self.assertEqual((a.unit, a.resume_of, a.relaunch_of), ("x1-other", None, None)); self.assertIn("not seeded from x1-a", o)

    def test_the_work_is_relaunched_before_its_review(self):
        """Catches: a review of stranded work relaunched for the item (reviewing what nobody has) instead of the work itself."""
        self.w.issue("X1"); self.stranded(name="x1-impl", kind="implement"); self.stranded(name="x1-rev", kind="review", started=int(time.time()) - 60)
        self.down("far", 45)
        self.assertEqual([u["name"] for u in janus.stranded_cover(self.cfg, self.w.units)["x1"]], ["x1-impl", "x1-rev"])
        self.assertEqual([x[4] for x in janus.fill_candidates(self.cfg, self.w.units)], ["x1-impl-r2"])

    def test_a_brief_pinned_to_the_down_host_is_not_moved(self):
        """Catches: work pinned to a host (its hardware) silently placed elsewhere, or waiting with nobody told."""
        self.w.issue("X1"); self.stranded(); self.down("far", 45)
        Path(self.w.units[0]["brief"]).write_text("# x1-a\nITEM: X1\nTEST-HOST: far\n")
        plan, stops = janus.fill_plan(self.cfg, self.hs, self.w.units)
        self.assertEqual(plan, []); self.assertIn("pinned to far", " ".join(stops))
        F = dict(janus.backlog_findings(self.cfg, self.hs, self.w.units))
        self.assertIn("is pinned to far, which is down", F["backlog:pinned:x1-a-r2"]); self.assertNotIn("backlog:held", F)


class Carry(Base):
    def repo(self):
        r = Path(tempfile.mkdtemp(prefix="janus-carry-")); git(r, "init", "-q", "-b", "main")
        (r / "a.txt").write_text("one\ntwo\nthree\n"); (r / "b.txt").write_text("b\n"); git(r, "add", "."); git(r, "commit", "-qm", "base")
        return r
    def patch(self, r, name, edit):
        """out/<name>/backup.patch: the change edit makes to a copy of r, as git diff writes it."""
        c = Path(tempfile.mkdtemp(prefix="janus-copy-")) / "c"; git(r.parent, "clone", "-q", str(r), str(c)); edit(c)
        (self.d / f".janus/out/{name}").mkdir(parents=True, exist_ok=True); (self.d / f".janus/out/{name}/backup.patch").write_text(git(c, "diff"))

    def test_the_patch_applies_or_the_fresh_tree_is_left_clean(self):
        """Catches: the stranded unit's synced changes dropped, a failed merge leaving conflict markers in the new tree, a 3-way merge
        in a tree janus did not make."""
        r = self.repo(); self.patch(r, "old", lambda c: (c / "a.txt").write_text("one\nTWO\nthree\n"))
        self.assertIn("applied", janus.carry_patch(self.cfg, "old", r, fresh=True)); self.assertEqual((r / "a.txt").read_text(), "one\nTWO\nthree\n")
        git(r, "checkout", "-q", "--", "."); (r / "a.txt").write_text("one\n2\nthree\n"); git(r, "commit", "-qam", "moved on")
        msg = janus.carry_patch(self.cfg, "old", r, fresh=True)
        self.assertIn("NOT applied", msg); self.assertIn("backup.patch", msg); self.assertEqual(git(r, "status", "--porcelain"), "")
        (r / "b.txt").write_text("mine\n")
        self.assertIn("NOT applied", janus.carry_patch(self.cfg, "old", r, fresh=False)); self.assertEqual((r / "b.txt").read_text(), "mine\n")   # not ours: untouched
        self.assertIn("no synced changes", janus.carry_patch(self.cfg, "none", r, fresh=True))

    def test_launch_records_the_pair_and_seeds_report_and_tree(self):
        """Catches: the relaunch not recorded as one (close --duplicate-of and the host's return cannot pair them), the original not
        naming its relaunch, the tree without the carried changes, the report without its origin."""
        r = self.repo(); self.w.issue("X1"); self.stranded(name="old", item="X1"); self.down("far", 45)
        self.patch(r, "old", lambda c: (c / "a.txt").write_text("one\nTWO\nthree\n"))
        k = self.d / "key.env"; k.write_text("ANTHROPIC_API_KEY=test-only\n"); k.chmod(0o600)
        a = argparse.Namespace(unit="old-r2", kind="implement", tier="normal", host=None, model="m", brief=str(self.d / ".janus/briefs/old.md"), cwd=None, budget=None,
                               test_host=None, allow_main=False, of=None, report=None, resume_of="old", relaunch_of="old", item="X1", dry_run=False, auto=True,
                               new_worktree=str(r), from_ref="main")
        def nowhere(*x, **y): raise AssertionError("reached a host command")
        with mock.patch.object(janus, "probe_all", lambda cfg: self.hs), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "X", nowhere), mock.patch.object(janus, "hrun", nowhere), mock.patch.object(janus, "herdr", nowhere):
            with self.assertRaises(Exception): out(janus.cmd_launch, self.cfg, a)
        rec = {u["name"]: u for u in self.w.units}
        self.assertEqual(rec["old"].get("relaunched"), "old-r2")
        self.assertIn(("save", "old-r2"), self.w.writes)
        tree = self.cfg.p("worktrees", "worktrees") / "old-r2"; self.assertEqual((tree / "a.txt").read_text(), "one\nTWO\nthree\n")
        rep = (self.d / ".janus/out/old-r2/REPORT.md").read_text()
        self.assertTrue(rep.startswith("VERDICT: PENDING\n"), rep); self.assertIn("the old work", rep); self.assertIn("RELAUNCH: old's changes", rep)


class Findings(Base):
    def test_host_findings_say_what_happens_and_pair_on_return(self):
        """Catches: the host finding saying nothing moves while fill relaunches its items; a gone host's units never named for
        closing; the original and its relaunch both running on with nobody told when the host answers again."""
        self.stranded(name="s1", relaunched="s1-r2"); self.stranded(name="s2", item="X2")
        self.w.units.append({"id": "u-r", "title": "unit:s1-r2", "name": "s1-r2", "host": "here", "item": "X1", "brief": "/none"})
        hs = dict(self.hs, here={**self.hs["here"], "ok": True})
        self.down("far", 10); F = dict(janus.host_findings(self.cfg, hs, self.w.units))
        self.assertIn("units there wait", F["host:far"]); self.assertIn("after 30 min", F["host:far"])
        self.down("far", 45); F = dict(janus.host_findings(self.cfg, hs, self.w.units))
        self.assertIn("stranded: its units no longer cover their items; relaunched: s1->s1-r2; not yet: s2", F["host:far"])
        self.mark("far", "gone"); F = dict(janus.host_findings(self.cfg, hs, self.w.units)); self.assertIn("janus close <unit> --force", F["host:far:gone"])
        self.cfg.jput("host-marks.json", {}); self.cfg.jput("hosts.json", {})
        F = dict(janus.host_findings(self.cfg, dict(hs, far=host_probe(name="far")), self.w.units))
        self.assertIn("s1 was relaunched as s1-r2", F["duplicate:s1"]); self.assertNotIn("duplicate:s2", F)
        self.assertTrue(all(janus.FAMILIES.get(k.split(":")[0]) for k in F), F)


if __name__ == "__main__": unittest.main()
