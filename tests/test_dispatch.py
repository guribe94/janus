"""The systemic repair: one eligible selection for fill and check (missing briefs named, bd ready membership, fail closed without
beads), briefs validated before any artifact, dispatch findings that wake again, landing accounting (publication on the unit
record, the checklist's evidence cell, explicit DONE, squash close by published containment) and the report totals. Standard
library only; bd is a fake (no database is touched), git runs in temporary repositories. Each test names the failure it catches."""
import contextlib, io, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project, host_probe

M = sys.modules["janus.monitor"]


class World:
    """What the fake bd answers: backlog issues, the ids bd ready lists, open and closed units; every write it was asked for."""
    def __init__(self):
        self.backlog, self.ready, self.units, self.closed, self.writes, self.fail, self.fail_backlog = [], None, [], [], [], False, False
    def issue(self, item, labels=(), status="open", **k):
        i = {"id": f"b-{item}", "title": f"backlog: {item} t", "labels": ["backlog", "backlog-ready", *labels], "status": status,
             "external_ref": f"backlog:{item}", "priority": 2, **k}
        self.backlog.append(i); return i
    def beads(self):
        w = self
        class B:
            def __init__(s, cfg): pass
            def run(s, *a):
                if w.fail or w.fail_backlog and (a[0] == "ready" or "backlog" in a): raise janus.Err(f"bd {a[0]}: database unreachable")
                if a[0] == "list": return [dict(i) for i in (w.backlog if "backlog" in a else [*w.units, *w.closed])]
                if a[0] == "ready": return [dict(i) for i in w.backlog if w.ready is None or i["id"] in w.ready]
                if a[0] == "update" and getattr(w, "fail_update", False): raise janus.Err(f"bd update {a[1]}: database unreachable")
                w.writes.append(a)
                for i in w.backlog if a[0] == "close" else []:
                    if i["id"] == a[1]: i["status"] = "closed"
                for i in w.backlog if a[0] == "update" and "--set-labels" in a else []:   # as bd does: the labels are replaced
                    if i["id"] == a[1]: i["labels"] = [l for l in a[a.index("--set-labels") + 1].split(",") if l]
                if a[0] == "create": return {"id": f"u-{len(w.writes)}"}
                return {}
            def units(s):
                if w.fail: raise janus.Err("bd list: database unreachable")
                return [dict(u) for u in w.units]
            def save(s, u):
                w.writes.append(("save", u.get("name")))
                for x in w.units + w.closed:
                    if x["id"] == u["id"]:   # the record is replaced, as bd update --notes does: a key popped is gone
                        keep = {"id": x["id"], "title": x.get("title")}; x.clear(); x.update(keep)
                        x.update({k: v for k, v in u.items() if k not in ("state", "verdict") and not k.startswith("_")})
            def create(s, title, label, desc=""): w.writes.append(("create", title)); return f"u-{len(w.writes)}"
            def comment(s, *a): pass
        return B
    def patch(self, t):
        p = mock.patch.object(janus, "Beads", self.beads()); p.start(); t.addCleanup(p.stop)
        t.addCleanup(setattr, t, "_w", None)


CHECK = "# Status\nSome prose that must stay.\n\n| # | item | status | evidence |\n|---|---|---|---|\n| X1 | one | READY | - |\n| X2 | two | READY |  |\n| X3 | three | DONE | abc |\n"
CONF = '\n[backlog]\nfile = "S.md"\nevidence_col = "evidence"\n[routing]\nnormal = ["m"]\ncritical = ["m"]\n'


def out(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): r = fn(*a)
    return r, buf.getvalue()


class Selection(unittest.TestCase):
    """fill_candidates, fill_plan and backlog_findings share one eligible selection."""
    def setUp(self):
        self.d, self.cfg = project(CONF); (self.d / "S.md").write_text(CHECK); self.cfg.hosts["here"].update(ram_gb=64, cores=16)
        self.w = World(); self.w.patch(self)
        self.hs = {"here": host_probe(name="here")}
    def brief(self, name, item, extra=""): (self.d / f".janus/briefs/{name}.md").write_text(f"# {name}\nITEM: {item}\n{extra}")

    def test_an_item_whose_units_are_all_done_is_named_for_closing(self):
        """Catches (owner 2026-11-18): items whose every unit had landed DONE stayed in the backlog as 'ready' for days; nothing told
        the orchestrator to close them, and check asked for a brief instead."""
        self.w.issue("X1"); self.w.issue("X2")
        (self.d / ".janus/out/x1-a").mkdir(parents=True); (self.d / ".janus/out/x1-a/REPORT.md").write_text("VERDICT: DONE (landed abc)\n")
        self.w.closed.append({"id": "u-x1a", "title": "unit:x1-a", "status": "closed", "notes": json.dumps({"v": 2, "name": "x1-a", "item": "X1", "host": "here", "started": 100})})
        F = dict(janus.backlog_findings(self.cfg, self.hs, []))
        self.assertIn("backlog:done:X1", F); self.assertIn("janus backlog set X1 status=DONE --unit x1-a", F["backlog:done:X1"])
        self.assertNotIn("X1", F["idle-with-backlog:nobrief"]); self.assertIn("X2", F["idle-with-backlog:nobrief"])
        self.brief("x1-b", "X1")   # a brief waits for what is left: fill launches it, nothing to close yet
        self.assertNotIn("backlog:done:X1", dict(janus.backlog_findings(self.cfg, self.hs, [])))

    def test_missing_brief_is_named_not_silently_omitted(self):
        """Catches: a ready agent item without a usable brief vanishing from fill and check (nobody is told to write its brief)."""
        self.w.issue("X1"); self.w.issue("X2"); self.brief("x1-a", "x1")   # the brief's ITEM is the same id in another case
        F = dict(janus.backlog_findings(self.cfg, self.hs, []))
        self.assertIn("idle-with-backlog:nobrief", F); self.assertIn("X2", F["idle-with-backlog:nobrief"]); self.assertNotIn("X1", F["idle-with-backlog:nobrief"])
        self.assertIn("x1-a", F.get("idle-with-backlog", ""))
        plan, stops = janus.fill_plan(self.cfg, self.hs, [])
        self.assertEqual([x[0] for x in plan], ["x1-a"]); self.assertIn("X2", " ".join(stops))

    def test_held_owner_covered_and_dependency_blocked_items_are_not_launched(self):
        """Catches: a held, owner, covered (an open unit names it, in any case) or dependency-blocked item being filled or
        reported as missing a brief."""
        self.w.issue("X1", labels=["hold:parked"]); self.w.issue("X2", labels=["actor:owner"]); self.w.issue("X4"); self.w.issue("X5")
        self.w.ready = {"b-X1", "b-X2", "b-X4"}   # X5 waits on a beads dependency: bd ready does not list it
        for n, it in (("x1-a", "X1"), ("x2-a", "X2"), ("x4-a", "X4"), ("x5-a", "X5")): self.brief(n, it)
        units = [{"name": "other", "item": "x4", "brief": "/none", "id": "u9"}]
        missing, blocked = [], []
        c = janus.fill_candidates(self.cfg, units, missing=missing, blocked=blocked)
        self.assertEqual(c, []); self.assertEqual(missing, []); self.assertEqual(blocked, ["X5"])
        F = dict(janus.backlog_findings(self.cfg, self.hs, units))
        self.assertNotIn("idle-with-backlog", F); self.assertNotIn("idle-with-backlog:nobrief", F); self.assertIn("X5", F.get("backlog:blocked", ""))

    def test_no_database_fails_closed_for_launch(self):
        """Catches: an unattended fill launching from the unchecked checklist when bd cannot be read (no ready, hold or actor check)."""
        self.brief("x1-a", "X1"); self.w.fail = True
        with self.assertRaises(janus.Err): janus.fill_candidates(self.cfg, [], launch=True)
        plan, stops = janus.fill_plan(self.cfg, self.hs, [], launch=True)
        self.assertEqual(plan, []); self.assertIn("beads", " ".join(stops))
        launched = []
        with mock.patch.object(janus, "probe_all", lambda cfg: self.hs), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "cmd_launch", lambda cfg, a: launched.append(a.unit)):
            with self.assertRaises(janus.Err) as e: out(janus.cmd_fill, self.cfg, type("A", (), {"max": None})())
        self.assertEqual(launched, []); self.assertIn("beads", str(e.exception))

    def test_no_capacity_launches_nothing_and_still_names_missing_briefs(self):
        """Catches: no room hiding why nothing launches, or the missing brief only showing once there is room."""
        self.w.issue("X1"); self.w.issue("X2"); self.brief("x1-a", "X1")
        hs = {"here": host_probe(name="here", cap_pause="memory pressure")}
        plan, stops = janus.fill_plan(self.cfg, hs, [])
        self.assertEqual(plan, []); self.assertIn("x1-a", " ".join(stops))
        F = dict(janus.backlog_findings(self.cfg, hs, []))
        self.assertNotIn("idle-with-backlog", F); self.assertIn("X2", F["idle-with-backlog:nobrief"]); self.assertIn("x1-a", F["backlog:held"])

    def test_repeated_passes_and_a_competing_manual_launch_never_duplicate(self):
        """Catches: a fill launch for an item another launch covered while fill probed the hosts (no lock was held then), and a
        second pass relaunching an item whose unit is open."""
        self.w.issue("X1"); self.brief("x1-a", "X1")
        k = self.d / "key.env"; k.write_text("ANTHROPIC_API_KEY=test-only\n"); k.chmod(0o600)   # a fixture value: the model is usable
        def probes(cfg):   # while the hosts answer, a manual launch records its unit for the same item
            self.w.units.append({"id": "u-m", "title": "unit:manual", "name": "manual", "item": "X1", "host": "here", "brief": "/none"}); return self.hs
        a = type("A", (), {"unit": "x1-a", "kind": "implement", "tier": "normal", "host": None, "model": "m", "brief": str(self.d / ".janus/briefs/x1-a.md"),
                           "cwd": None, "budget": None, "test_host": None, "allow_main": False, "of": None, "report": None, "resume_of": None, "item": "X1",
                           "dry_run": False, "new_worktree": None, "from_ref": None, "auto": True})()
        def nowhere(*a, **k): raise AssertionError("the launch reached a host command (pueue, ssh, tmux)")   # never a real queue
        with mock.patch.object(janus, "probe_all", probes), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "X", nowhere), mock.patch.object(janus, "hrun", nowhere), mock.patch.object(janus, "herdr", nowhere):
            with self.assertRaises(janus.Err) as e: janus.cmd_launch(self.cfg, a)
        self.assertIn("manual", str(e.exception)); self.assertFalse([x for x in self.w.writes if x[0] == "create"])
        self.assertFalse((self.d / ".janus/out/x1-a").exists())
        self.assertEqual(janus.fill_candidates(self.cfg, [{"name": "manual", "item": "x1", "brief": "/none"}]), [])   # the next pass: covered


class BriefFirst(unittest.TestCase):
    """brief_plan runs before any out dir, worktree, reservation or unit record, and during fill planning for the chosen host."""
    def setUp(self):
        self.r = Path(tempfile.mkdtemp(prefix="janus-rem-"))
        self.d, self.cfg = project(CONF + f'\n[hosts.far]\nssh = "nowhere"\nroot = "{self.r}"\nram_gb = 64\ncores = 16\n'
                                   '\n[providers.oc]\nenabled = true\n[models.oc]\nprovider = "oc"\nkind = "opencode"\nmodel = "z"\n')
        (self.d / "S.md").write_text(CHECK); self.w = World(); self.w.patch(self)
        real = janus.hrun; p = mock.patch.object(janus, "hrun", lambda h, script, **k: real({"local": True}, script, **k)); p.start(); self.addCleanup(p.stop)
        self.hs = {"here": host_probe(name="here", cap_pause="full"), "far": host_probe(name="far")}
    def a(self, name, **k):
        return type("A", (), {"unit": name, "kind": "implement", "tier": "normal", "host": "far", "model": "oc", "brief": None, "cwd": None, "budget": None,
                              "test_host": None, "allow_main": False, "of": None, "report": None, "resume_of": None, "item": None, "dry_run": False,
                              "probes": self.hs, "share_with": None, **k})()

    def test_a_refused_brief_leaves_no_out_dir_and_fill_retries_it(self):
        """Catches: launch making out/<unit> before brief_plan refused the brief, after which fill treated the unit as spent forever."""
        (self.d / ".janus/briefs/bad.md").write_text("ITEM: X1\nRead /no/such/path-xyz/f.md first.\n")
        with self.assertRaises(janus.Err) as e: janus.cmd_launch(self.cfg, self.a("bad"))
        self.assertIn("/no/such/path-xyz", str(e.exception))
        self.assertFalse((self.d / ".janus/out/bad").exists(), "a refused launch left its out dir")
        self.assertFalse(self.w.writes, "a refused launch recorded something")
        (self.d / ".janus/briefs/bad.md").write_text("ITEM: X1\nAll fixed.\n"); self.w.issue("X1")
        self.assertEqual([x[4] for x in janus.fill_candidates(self.cfg, [])], ["bad"])   # retried once the brief is fixed
        (self.d / ".janus/out/bad").mkdir()   # an empty out dir an older janus left is no proof the unit ever ran
        self.assertEqual([x[4] for x in janus.fill_candidates(self.cfg, [])], ["bad"])

    def test_a_dry_run_launch_makes_no_out_dir(self):
        """Catches: --dry-run creating out/<unit> (a dry run changes nothing anywhere)."""
        (self.d / ".janus/briefs/dry.md").write_text("ITEM: X1\nok\n"); self.cfg.run.dry = True
        with mock.patch.object(janus, "herdr", lambda *a, **k: None): out(janus.cmd_launch, self.cfg, self.a("dry", host="here", probes={"here": host_probe(name="here")}))
        self.assertFalse((self.d / ".janus/out/dry").exists(), "a dry run made the out dir")

    def test_fill_planning_refuses_an_invalid_brief_and_plans_the_others(self):
        """Catches: fill planning a remote unit whose brief the host cannot satisfy (refused only at launch, after artifacts), and
        one bad brief holding back the rest."""
        self.w.issue("X1"); self.w.issue("X2")
        (self.d / ".janus/briefs/x1-a.md").write_text("ITEM: X1\nTEST-HOST: far\nRead /no/such/path-xyz/f.md.\n")
        (self.d / ".janus/briefs/x2-a.md").write_text("ITEM: X2\nTEST-HOST: far\nok\n")
        self.cfg.route.update(normal=["oc"])
        plan, stops = janus.fill_plan(self.cfg, self.hs, [])
        self.assertEqual([x[0] for x in plan], ["x2-a"]); self.assertIn("x1-a", " ".join(stops)); self.assertIn("/no/such/path-xyz", " ".join(stops))
        self.assertFalse((self.d / ".janus/out/x1-a").exists())


class Reached(Exception):
    """The launch got past every refusal to its first remote command (mkdir on the host): stopped there, nothing reaches a host."""


class RemoteRootRefused(unittest.TestCase):
    """scoped-collect 1: a remote launch never copies the whole project root by default; explicit trees and local launches are unchanged."""
    setUp, a = BriefFirst.setUp, BriefFirst.a
    def launch(self, name, **k):
        (self.d / f".janus/briefs/{name}.md").write_text("ITEM: X1\nok\n"); sent = []
        def rsync_never(cmd, *x, **kw):
            if cmd and cmd[0] == "rsync": sent.append(cmd); raise AssertionError("a copy was started")
            return real_sh(cmd, *x, **kw)
        def host(*x, **kw): raise Reached()
        real_sh = janus.sh
        with mock.patch.object(janus, "X", host), mock.patch.object(janus, "sh", rsync_never), \
             mock.patch.object(janus, "reserve_budget", lambda *x, **kw: (_ for _ in ()).throw(AssertionError("budget reserved"))):
            try: return out(janus.cmd_launch, self.cfg, self.a(name, **k))[1], sent
            finally: self.assertEqual(sent, [])

    def test_a_remote_launch_without_a_tree_is_refused_before_any_effect(self):
        """Catches: a remote launch with no --cwd/--new-worktree copying the whole project root (every repo, build output and cache)
        to the host, recording a unit and making its out dir first."""
        with self.assertRaises(janus.Err) as e: self.launch("r1")
        m = str(e.exception)
        for k in ("refused", "--new-worktree", "--cwd", "far", str(self.d.resolve())): self.assertIn(k, m)
        self.assertEqual(self.w.writes, [], "a refused launch recorded something"); self.assertFalse((self.d / ".janus/out/r1").exists())
        self.assertFalse((self.r / "ctx").exists()); self.assertFalse((self.d / ".janus/worktrees/r1").exists())

    def test_explicit_trees_still_reach_the_host(self):
        """--cwd, an existing <worktrees>/<unit> and --new-worktree are explicit selections: past the refusal, up to the host's mkdir."""
        t = self.d / "tree"; t.mkdir()
        with self.assertRaises(Reached): self.launch("r2", cwd=str(t))
        (self.d / ".janus/worktrees/r3").mkdir(parents=True)
        with self.assertRaises(Reached): self.launch("r3")
        repo = self.d / "repo"; repo.mkdir(); git(repo, "init", "-q", "-b", "main"); git(repo, "commit", "-q", "--allow-empty", "-m", "c")
        with self.assertRaises(Reached): self.launch("r4", new_worktree=str(repo), from_ref="main")
        self.assertTrue((self.d / ".janus/worktrees/r4").is_dir())

    def test_a_local_launch_without_a_tree_is_unchanged(self):
        """The local default (the project root, with the note) is not refused."""
        (self.d / ".janus/briefs/l1.md").write_text("ITEM: X1\nok\n"); self.cfg.run.dry = True
        with mock.patch.object(janus, "herdr", lambda *x, **k: None):
            _, o = out(janus.cmd_launch, self.cfg, self.a("l1", host="here", probes={"here": host_probe(name="here")}))
        self.assertIn("no worktree made for l1", o); self.assertNotIn("refused", o)


class Wakes(unittest.TestCase):
    """Dispatch findings wake an idle orchestrator again after the cooldown; other findings wake once per change."""
    def setUp(self):
        self.d, self.cfg = project(); self.sent = []
        p = mock.patch.object(sys.modules["janus.watch"], "herdr", lambda *a: self.sent.append(a[-1])); p.start(); self.addCleanup(p.stop)
    def age(self, s): os.utime(self.cfg.f("last_wake"), (time.time() - s,) * 2)
    def test_dispatch_findings_recur_and_others_stay_edge_triggered(self):
        F = [janus.finding("idle-with-backlog", "1 ready unit fits"), janus.finding("unit:x:finished", "t")]
        janus.wake_on_change(self.cfg, lambda m: None, "P", F); self.assertEqual(len(self.sent), 1)
        self.age(100); janus.wake_on_change(self.cfg, lambda m: None, "P", F); self.assertEqual(len(self.sent), 1)   # the cooldown holds
        self.age(700); janus.wake_on_change(self.cfg, lambda m: None, "P", F); self.assertEqual(len(self.sent), 2)   # still there: woken again
        self.assertIn("idle-with-backlog", self.sent[-1])
        G = [janus.finding("unit:x:finished", "t")]
        self.age(700); janus.wake_on_change(self.cfg, lambda m: None, "P", G); self.assertEqual(len(self.sent), 2)   # unchanged, not dispatch: no wake
    def test_the_wake_prompt_runs_the_loop_not_only_changes(self):
        w = janus.wake(self.cfg, "x")
        self.assertNotIn("act on what changed", w)
        for k in ("integrate", "brief", "fill", "blocker"): self.assertIn(k, w.lower())
    def test_fill_runs_as_a_watch_step_and_its_failure_is_counted(self):
        """Catches: a failing fill logged once as 'fill failed' and never counted (no watcher:step:fill finding)."""
        cfg = self.cfg; cfg.c["fill"] = {"watcher": True}
        def boom(cfg, a): raise RuntimeError("fill exploded")
        stubs = {"run_check": lambda cfg, cycle=False, writer=True, pre=None: ([janus.finding("idle-with-backlog", "t")], None, {}, []),
                 "herdr": lambda *a: None, "cmd_fill": boom}
        stubs.update({n: (lambda *a, **k: []) for n in ("heartbeat_beat", "board_alerts", "tend_parked", "tend_thrash", "tend_tidy", "orphan_jobs")})
        with mock.patch.multiple(sys.modules["janus.watch"], **stubs): janus.watch_pass(cfg, lambda m: None)
        self.assertIn("fill", cfg.jget("watch_steps.json")); self.assertIn("fill exploded", cfg.jget("watch_steps.json")["fill"]["err"])


def git(c, *a, check=True):
    r = subprocess.run(["git", "-C", str(c), *a], capture_output=True, text=True)
    if check and r.returncode: raise AssertionError(f"git {a}: {r.stderr}")
    return r.stdout.strip()


class LandingBase(unittest.TestCase):
    """A project with a nested repository, a bare origin and a unit worktree with one pushed and one unpushed commit."""
    def setUp(self):
        self.d, self.cfg = project(CONF + '\n[landing]\nbranch = "main"\npush = true\n')
        (self.d / "S.md").write_text(CHECK); self.w = World(); self.w.patch(self)
        self.origin = self.d / "origin.git"; git(self.d, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.r = self.d / "repo"; self.r.mkdir(); git(self.r, "init", "-q", "-b", "main"); (self.r / "a").write_text("1\n")
        git(self.r, "add", "a"); git(self.r, "commit", "-qm", "base"); git(self.r, "remote", "add", "origin", str(self.origin)); git(self.r, "push", "-q", "-u", "origin", "main")
        self.wt = self.d / "wt"; git(self.r, "worktree", "add", "-q", "-b", "wk/u", str(self.wt))
        (self.wt / "b").write_text("x\n"); git(self.wt, "add", "b"); git(self.wt, "commit", "-qm", "b"); git(self.wt, "push", "-q", "-u", "origin", "wk/u")
        (self.wt / "c").write_text("y\n"); git(self.wt, "add", "c"); git(self.wt, "commit", "-qm", "c")   # unpushed on wk/u: squash-landed only
        self.u = {"id": "u1", "title": "unit:u", "name": "u", "cwd": str(self.wt), "host": "here", "item": "X1", "brief": "/none"}
        self.w.units = [self.u]
        for p in (mock.patch.object(janus, "own_tree", lambda *a: True), mock.patch.object(janus, "unapplied_patch", lambda *a: None)): p.start(); self.addCleanup(p.stop)
    def land(self): return out(janus.cmd_land, self.cfg, type("A", (), {"unit": "u", "push": True, "host": None})())
    def remote_count(self): return int(git(self.origin, "rev-list", "--count", "main"))
    def set_done(self, dry=False, commit=None):
        self.cfg.run.dry = dry
        try: return out(janus.cmd_backlog, self.cfg, type("A", (), {"action": "set", "args": ["X1", "status=DONE"], "unit": "u", "commit": commit})())
        finally: self.cfg.run.dry = False

    def close(self, dry=True):
        self.cfg.run.dry = dry   # dry: the git checks run fully; the host cleanup is only printed
        try: return out(janus.cmd_close, self.cfg, type("A", (), {"unit": "u", "force": False, "reason": None})())
        finally: self.cfg.run.dry = False


class Landing(LandingBase):
    """Publication metadata, the checklist's evidence cell, explicit DONE, and squash close by published containment."""
    def test_push_failure_records_nothing(self):
        git(self.wt, "remote", "set-url", "origin", str(self.d / "missing.git"))
        with self.assertRaises(janus.Err) as e: self.land()
        self.assertIn("NOT PUBLISHED", str(e.exception)); self.assertNotIn("published", self.u)
        self.assertEqual((self.d / "S.md").read_text(), CHECK)

    def test_publication_is_recorded_and_the_evidence_cell_written_status_unchanged(self):
        rc, txt = self.land(); self.assertEqual(rc, 0, txt)
        pub = self.u["published"]; tip = git(self.origin, "rev-parse", "main")
        self.assertEqual(pub["commit"], tip); self.assertEqual((pub["remote"], pub["branch"]), ("origin", "main")); self.assertTrue(pub["source"])
        self.assertEqual(pub["writeback"], "done")
        text = (self.d / "S.md").read_text()
        self.assertIn(f"| X1 | one | READY | {tip[:12]} |", text)   # status stays: a landing is not acceptance
        self.assertEqual(text.replace(f" {tip[:12]} ", " - "), CHECK)   # every other byte as it was

    def test_a_failed_writeback_is_retried_without_landing_again(self):
        real = janus.checklist_set; calls = []
        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1: raise janus.Err("checklist busy")
            return real(*a, **k)
        with mock.patch.object(janus, "checklist_set", flaky):
            rc, txt = self.land(); self.assertEqual(rc, 0); self.assertIn("incomplete", txt)
            self.assertTrue(self.u["published"]["writeback"].startswith("incomplete")); n = self.remote_count()
            self.assertEqual((self.d / "S.md").read_text(), CHECK)
            rc, txt = self.land(); self.assertEqual(rc, 0, txt)
        self.assertEqual(self.remote_count(), n, "landed again"); self.assertEqual(self.u["published"]["writeback"], "done")
        self.assertIn(self.u["published"]["commit"][:12], (self.d / "S.md").read_text())

    def test_explicit_done_needs_verified_publication_and_repeats_cleanly(self):
        self.w.issue("X1")
        with self.assertRaises(janus.Err) as e: self.set_done()
        self.assertIn("publication", str(e.exception)); self.assertEqual((self.d / "S.md").read_text(), CHECK)
        self.land(); tip = self.u["published"]["commit"]
        _, txt = self.set_done(dry=True); self.assertIn("DONE", txt); self.assertNotIn("| X1 | one | DONE", (self.d / "S.md").read_text())
        self.set_done()
        self.assertIn(f"| X1 | one | DONE | {tip[:12]} |", (self.d / "S.md").read_text())
        self.assertEqual([x for x in self.w.writes if x[0] == "close"], [("close", "b-X1", "--reason", f"checklist: DONE (unit u, {tip[:12]})")])
        before = (self.d / "S.md").read_text(); self.set_done()   # repeated: nothing changes, nothing is closed twice
        self.assertEqual((self.d / "S.md").read_text(), before); self.assertEqual(len([x for x in self.w.writes if x[0] == "close"]), 1)
        self.u["published"] = {**self.u["published"], "commit": "f" * 40}   # a record whose commit is not on the remote: refused
        with self.assertRaises(janus.Err): self.set_done()

    def test_done_for_a_commit_the_units_agent_pushed_itself(self):
        """Catches (2026-11-18, BRIEF-UPKEEP, CHECKLIST-UPKEEP, A80): a unit whose agent pushed to main itself has no janus land
        record, so its item could not be accepted at all, though `merge-base --is-ancestor` proves it landed. --commit names
        the commit; it must be one the unit's own report names and on the landing branch now."""
        self.w.issue("X1"); sha = git(self.wt, "rev-parse", "HEAD"); rep = self.d / ".janus/out/u/REPORT.md"; rep.parent.mkdir(parents=True, exist_ok=True)
        rep.write_text("VERDICT: DONE\npushed\n")
        with self.assertRaises(janus.Err) as e: self.set_done(commit=sha[:9])
        self.assertIn("report names", str(e.exception))                     # a commit its report does not name: not its evidence
        rep.write_text(f"VERDICT: DONE (pushed {sha[:9]} to main)\n")
        with self.assertRaises(janus.Err) as e: self.set_done(commit=sha[:9])
        self.assertIn("is not on origin/main", str(e.exception))           # named, not on the landing branch yet
        git(self.wt, "push", "-q", "origin", "HEAD:main")                   # the agent's own push
        self.u["started"] = int(time.time()) + 3600                          # a commit from before the unit started: its base, not its work
        with self.assertRaises(janus.Err) as e: self.set_done(commit=sha[:9])
        self.assertIn("before u started", str(e.exception)); self.u["started"] = int(time.time()) - 3600
        self.set_done(commit=sha[:9])
        self.assertIn(f"| X1 | one | DONE | {sha[:12]} |", (self.d / "S.md").read_text())
        self.u["published"] = {"commit": sha, "remote": "origin", "branch": "main"}   # a recorded publication is the evidence then
        with self.assertRaises(janus.Err) as e: self.set_done(commit=sha[:9])
        self.assertIn("only for a unit pushed without janus land", str(e.exception))

    def test_ambiguous_or_unsupported_layouts_refuse(self):
        (self.d / "S.md").write_text(CHECK + "| X1 | again | READY | - |\n")
        with self.assertRaises(janus.Err) as e: janus.checklist_set(self.cfg, "X1", evidence="abc")
        self.assertIn("ambiguous", str(e.exception))
        (self.d / "S.md").write_text("- [ ] X1 thing\n")
        with self.assertRaises(janus.Err) as e: janus.checklist_set(self.cfg, "x1-thing", evidence="abc")
        self.assertIn("unsupported", str(e.exception)); self.assertEqual((self.d / "S.md").read_text(), "- [ ] X1 thing\n")
        (self.d / "S.md").write_text("| # | item | status |\n|---|---|---|\n| X1 | one | READY |\n")
        with self.assertRaises(janus.Err) as e: janus.checklist_set(self.cfg, "X1", evidence="abc")
        self.assertIn("evidence", str(e.exception))

    def test_squash_close_by_published_containment(self):
        """Catches: a squash-landed unit refused as 'unpushed' forever (only --force, which skips the other guards), and a
        containment check that would accept an unpublished, divergent or since-edited branch."""
        with self.assertRaises(janus.Err) as e: self.close()
        self.assertIn("unpushed", str(e.exception))   # unpublished: refused as before
        self.land(); self.close()                       # landed and published: contained, closes
        pub = self.u["published"]
        self.u["published"] = {**pub, "commit": git(self.r, "rev-parse", "main~1")}   # divergent: the record names a commit without the unit's files
        with self.assertRaises(janus.Err): self.close()
        self.u["published"] = pub
        (self.wt / "c").write_text("edited\n"); git(self.wt, "commit", "-qam", "edit after landing")
        with self.assertRaises(janus.Err) as e: self.close()
        self.assertIn("unpushed", str(e.exception))


    def test_close_records_the_units_verdict_not_done(self):
        """Catches: janus close recorded reason 'done' for a unit whose report said BLOCKED or PENDING, so the ledger read as if
        every closed unit had finished."""
        self.land(); rep = self.d / ".janus/out/u/REPORT.md"; rep.parent.mkdir(parents=True, exist_ok=True); rep.write_text("VERDICT: BLOCKED (no VM host)\n")
        with mock.patch.object(janus, "uncollected", lambda *a: None), mock.patch.object(janus, "X", lambda *a, **k: (0, "", "")):
            self.close(dry=False)
        self.assertIn(("close", "u1", "--reason", "VERDICT: BLOCKED (no VM host)"), self.w.writes)

class FollowUp(LandingBase):
    """The reviewer's follow-up findings: each test fails on the first-pass code."""
    def held(self): return str(self.cfg.f("write.lock")) in sys.modules["janus.context"]._HELD

    def test_1_a_fill_whose_launch_failed_is_a_failed_watch_step(self):
        """Catches: cmd_fill returning 1 (a launch failed) while the watcher's step counted it as working."""
        cfg = self.cfg; cfg.c["fill"] = {"watcher": True}
        stubs = {"run_check": lambda cfg, cycle=False, writer=True, pre=None: ([janus.finding("idle-with-backlog", "t")], None, {}, []),
                 "herdr": lambda *a: None, "cmd_fill": lambda cfg, a: (print("fill: x1-a not launched: boom"), 1)[1]}
        stubs.update({n: (lambda *a, **k: []) for n in ("heartbeat_beat", "board_alerts", "tend_parked", "tend_thrash", "tend_tidy", "orphan_jobs")})
        with mock.patch.multiple(sys.modules["janus.watch"], **stubs): janus.watch_pass(cfg, lambda m: None)
        self.assertIn("fill", cfg.jget("watch_steps.json"))

    def auto(self, item="X1"):
        b = self.d / ".janus/briefs/x1-a.md"; b.write_text(f"ITEM: {item}\n")
        k = self.d / "key.env"; k.write_text("ANTHROPIC_API_KEY=test-only\n"); k.chmod(0o600)   # a fixture value: the model is usable
        a = type("A", (), {"unit": "x1-a", "kind": "implement", "tier": "normal", "host": None, "model": "m", "brief": str(b), "cwd": None, "budget": None,
                           "test_host": None, "allow_main": False, "of": None, "report": None, "resume_of": None, "item": item, "dry_run": False,
                           "new_worktree": None, "from_ref": None, "auto": True})()
        def nowhere(*a, **k): raise AssertionError("the launch reached a host command (pueue, ssh, tmux)")
        with mock.patch.object(janus, "probe_all", lambda cfg: {"here": host_probe(name="here")}), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "X", nowhere), mock.patch.object(janus, "hrun", nowhere), mock.patch.object(janus, "herdr", nowhere), self.cfg.writer():
            with self.assertRaises(janus.Err) as e: janus.cmd_launch(self.cfg, a)
        self.assertFalse((self.d / ".janus/out/x1-a").exists()); self.assertFalse([x for x in self.w.writes if x[0] == "create"])
        return str(e.exception)

    def test_2_auto_launch_rechecks_ready_actor_and_hold_and_fails_closed(self):
        """Catches: an automatic launch that only rechecked coverage, so an item held, given to the owner, blocked or no longer
        ready while fill probed the hosts (or with beads gone) was still launched."""
        self.w.units = []; i = self.w.issue("X1", labels=["hold:parked"])
        self.assertIn("no longer eligible", self.auto())
        i["labels"] = ["backlog", "backlog-ready", "actor:owner"]; self.assertIn("no longer eligible", self.auto())
        i["labels"] = ["backlog", "backlog-ready"]; self.w.ready = set(); self.assertIn("no longer eligible", self.auto())
        self.w.ready = None; self.w.fail_backlog = True; self.assertIn("beads", self.auto())

    def test_3_a_unit_naming_the_bead_id_covers_its_checklist_item(self):
        """Catches: a historical unit whose item is the bead id (orch-7) not covering checklist item X1, so fill dispatched X1 again,
        and its landing evidence / DONE not finding the row."""
        self.w.issue("X1", id="orch-7"); (self.d / ".janus/briefs/x1-a.md").write_text("ITEM: X1\n")
        units = [{"name": "old", "item": "orch-7", "brief": "/none", "id": "u9"}]
        self.assertEqual(janus.unit_items(self.cfg, units), {"x1"})
        self.assertEqual(janus.fill_candidates(self.cfg, units), [])
        self.u["item"] = "orch-7"; rc, txt = self.land(); self.assertEqual(rc, 0, txt)
        self.assertEqual(self.u["published"]["writeback"], "done"); self.assertIn(self.u["published"]["commit"][:12], (self.d / "S.md").read_text())
        self.set_done(); self.assertIn("| X1 | one | DONE |", (self.d / "S.md").read_text())

    def test_4_squash_close_requires_the_published_source_and_rechecks_before_cleanup(self):
        """Catches: containment accepted for a HEAD that is not the published source (an empty changed-file set bypassing it), and
        a close that let go of the lock and then cleaned up a tree that became dirty or moved meanwhile."""
        self.land(); pub = dict(self.u["published"])
        git(self.wt, "reset", "-q", "--hard", pub["commit"])   # HEAD is the published commit, not its source: no file changed since the merge-base
        with self.assertRaises(janus.Err) as e: self.close()
        self.assertIn("source", str(e.exception))
        self.assertIsNotNone(janus.published_contains(self.cfg, {**self.u, "published": {**pub, "source": git(self.wt, "rev-parse", "HEAD~1")}}))
        git(self.wt, "reset", "-q", "--hard", pub["source"]); self.close()   # back at its source: closes

    def close_racing(self, change):
        self.land(); calls = []
        def unc(cfg, u):
            calls.append(1)
            if len(calls) == 1: change()
            return None
        with mock.patch.object(janus, "uncollected", unc), mock.patch.object(janus, "X", lambda *a, **k: (0, "", "")):
            with self.assertRaises(janus.Err) as e: self.close(dry=False)
        self.assertNotIn(("close", "u1", "--reason", "done"), self.w.writes); self.assertNotIn("closing", self.u)
        return str(e.exception)

    def test_4b_a_tree_dirtied_during_close_is_not_cleaned_up(self):
        self.assertIn("uncommitted", self.close_racing(lambda: (self.wt / "c").write_text("dirty\n")))

    def test_4c_a_tree_moved_during_close_is_not_cleaned_up(self):
        def move(): (self.wt / "c").write_text("moved\n"); git(self.wt, "commit", "-qam", "moved")
        self.assertIn("moved", self.close_racing(move))

    def test_5_landing_writes_hold_the_writer_lock_even_for_an_agent(self):
        """Catches: save_unit and the checklist write under command_writer, which takes no lock for JANUS_ROLE=agent."""
        seen = []; real = janus.checklist_set; real_save = self.w.beads().save
        def cs(*a, **k): seen.append(("checklist", self.held())); return real(*a, **k)
        with mock.patch.dict(os.environ, {"JANUS_ROLE": "agent"}), mock.patch.object(janus, "checklist_set", cs):
            B = self.w.beads(); orig = B.save
            def save(s, u): seen.append(("save", self.held())); return orig(s, u)
            B.save = save
            with mock.patch.object(janus, "Beads", B): rc, txt = self.land()
        self.assertEqual(rc, 0, txt); self.assertTrue(seen); self.assertTrue(all(h for _, h in seen), seen)
        self.assertFalse(self.held())   # nothing is held after: never across the push or the fetch

    def test_6_done_needs_its_issue_first_and_a_failed_close_is_retried(self):
        """Catches: DONE written into the checklist for an item beads does not hold, and a failed bead close raised as a bare bd
        error after the checklist changed (no word that the same command finishes it)."""
        self.land(); before = (self.d / "S.md").read_text()
        with self.assertRaises(janus.Err) as e: self.set_done()
        self.assertIn("issue", str(e.exception)); self.assertEqual((self.d / "S.md").read_text(), before)   # no issue: nothing written
        self.w.issue("X1"); self.w.issue("x1", id="b-dup")
        with self.assertRaises(janus.Err) as e: self.set_done()
        self.assertIn("ambiguous", str(e.exception)); self.assertEqual((self.d / "S.md").read_text(), before)
        self.w.backlog.pop()
        B = self.w.beads(); run = B.run
        def failing(s, *a):
            if a[0] == "close": raise janus.Err("bd close: database locked")
            return run(s, *a)
        with mock.patch.object(janus, "Beads", type("F", (B,), {"run": failing})):
            with self.assertRaises(janus.Err) as e: self.set_done()
        m = str(e.exception); self.assertIn("checklist", m); self.assertIn("same command", m)
        self.assertIn("| X1 | one | DONE |", (self.d / "S.md").read_text())
        self.set_done(); self.assertEqual(len([x for x in self.w.writes if x[0] == "close"]), 1)   # the retry closes it once
        self.set_done(); self.assertEqual(len([x for x in self.w.writes if x[0] == "close"]), 1)

    def test_8_done_after_the_worktree_is_removed_in_a_nested_repository(self):
        """Catches: verification falling back to the project root (not a repository here: the repository is nested) once the
        unit's worktree is gone, so DONE could never be accepted."""
        self.w.issue("X1"); self.land()
        git(self.r, "worktree", "remove", "--force", str(self.wt)); self.assertFalse(self.wt.exists())
        self.set_done(); self.assertIn("| X1 | one | DONE |", (self.d / "S.md").read_text())


class Reports(unittest.TestCase):
    """One checklist total for report, status and monitor; open-unit coverage named as such; report ages kept apart."""
    def setUp(self):
        self.d, self.cfg = project(CONF + '\n[hosts.far]\nssh = "far"\nroot = "/x"\n'); (self.d / "S.md").write_text(CHECK)
        self.w = World(); self.w.patch(self)
    def test_status_and_report_share_the_checklist_totals(self):
        self.w.issue("X1"); self.w.issue("X2"); (self.d / ".janus/briefs/x1-a.md").write_text("ITEM: X1\n")
        units = [{"name": "u", "item": "X1", "brief": "/none", "host": "here", "id": "u1"}]
        line = janus.checklist_line(janus.checklist_totals(self.cfg, units), ["X2"])
        self.assertIn("1 of 3 checklist items DONE (33 %", line); self.assertIn("not weighted", line)
        self.assertIn("1 in open units (open, not necessarily running)", line); self.assertIn("1 ready without a usable brief: X2", line)
        with mock.patch.object(janus, "ready_work", lambda cfg: []): s = janus.status_text(self.cfg, {}, units, [])
        self.assertIn("1 of 3 checklist items DONE", s)
        self.w.units = [{"id": "u1", "title": "unit:u", "name": "u", "item": "X1", "brief": "/none"}]
        with mock.patch.object(janus, "run_outcomes", lambda cfg: []): _, L = janus.cmd_report(self.cfg, type("A", (), {"brief": True})())
        self.assertIn("1 of 3 checklist items DONE", "\n".join(L)); self.assertIn("X2", "\n".join(L))

    def test_monitor_ends_tab_report_ages_and_open_unit_coverage(self):
        self.assertIn("Ended", M.TABS); self.assertNotIn("Finished", M.TABS)
        now = time.time()
        u = {"name": "r", "host": "far", "via": "tmux", "cwd": "/w", "started": now - 9000, "item": "X1"}
        od = self.d / ".janus/out/r"; od.mkdir(parents=True); (od / "REPORT.md").write_text("VERDICT: PENDING\n"); os.utime(od / "REPORT.md", (now - 7200,) * 2)
        rep = M.collect_reports(self.cfg, [u])["r"]
        for read_at in (now - 5, now - 300):   # a later read keeps the report's own age
            d = "\n".join(M.unit_row(u, "Active", "working", "", now, False, rep, read_at=read_at).detail)
            self.assertIn("Modified:  2h00m ago", d); self.assertIn("local copy", d); self.assertNotIn("Synced:", d)
        self.assertIn("Read:      5m ago", d)
        rep = M.collect_reports(self.cfg, [{**u, "saved_at": now - 600}])["r"]
        d = "\n".join(M.unit_row(u, "Active", "working", "", now, False, rep, read_at=now).detail)
        self.assertNotIn("Synced:", d); self.assertIn("Output:    synced", d); self.assertIn("this report with it", d)
        lu = {**u, "host": "here", "report": str(self.d / "elsewhere.md"), "saved_at": now - 600}; (self.d / "elsewhere.md").write_text("VERDICT: PENDING\n")
        d = "\n".join(M.unit_row(lu, "Active", "working", "", now, False, M.collect_reports(self.cfg, [lu])["r"], read_at=now).detail)
        self.assertNotIn("local copy", d); self.assertNotIn("Synced:", d)   # a report outside out/<unit> is not what the output sync brought
        rows = M.backlog_rows({"items": [{"id": "b-X1", "title": "backlog: X1 one", "labels": ["backlog"], "external_ref": "backlog:X1"}], "ready": []},
                              [{"name": "u", "item": "x1"}], now, False)
        self.assertEqual(rows[0].state, "open-unit"); self.assertIn("not necessarily running", " ".join(rows[0].detail))


DRAFT = "# X1 draft\nPRIORITY: 1\nHOLD: waiting on the owner's design\nbody kept\r\nmore\n" + "x\n" * 10 + "HOLD: past the head, not a hold\n"


class HoldRelease(unittest.TestCase):
    """janus backlog set <id> hold=off takes every hold off: the beads labels first, then the drafts' HOLD: lines (kept as
    RELEASED-HOLD:). bd is the fake World (no database); no host is reached."""
    def setUp(self):
        self.d, self.cfg = project(CONF); (self.d / "S.md").write_text(CHECK)
        self.w = World(); self.w.patch(self)
        self.dr = self.d / ".janus/briefs/drafts"; self.dr.mkdir(parents=True)
    def setup_holds(self):
        self.w.issue("X1", labels=["hold:parked", "actor:agent"]); self.w.issue("x1.", labels=["hold:old"])   # two refs, one item (norm_item)
        (self.dr / "X1.md").write_text(DRAFT); (self.dr / "x1..md").write_text("HOLD:\nsecond draft\n")      # stems X1 and x1. name item x1
        (self.dr / "X2.md").write_text("HOLD: another item\n"); self.w.issue("X2")
    def set(self, item, v):
        return out(janus.cmd_backlog, self.cfg, type("A", (), {"action": "set", "args": [item, v], "unit": None})())[1]
    def labels(self): return {i["external_ref"]: [l for l in i["labels"] if l.startswith("hold:")] for i in self.w.backlog}

    def test_every_alias_releases_every_source_keeping_reasons_and_is_idempotent(self):
        """Catches: hold=off clearing the beads label while a draft's HOLD: line kept the item held (and saying 'no longer held')."""
        for alias in ("hold=off", "hold=none", "hold=no", "hold=", "hold=OFF"):
            self.w.backlog.clear(); self.w.writes.clear(); self.setup_holds()
            self.assertIn("x1", janus.item_holds(self.cfg))
            o = self.set("x1", alias)   # another case than the checklist's X1
            self.assertIn("no longer held", o, alias)
            self.assertEqual(self.labels(), {"backlog:X1": [], "backlog:x1.": [], "backlog:X2": []}, alias)
            self.assertIn("actor:agent", self.w.backlog[0]["labels"])   # other labels kept
            self.assertEqual((self.dr / "X1.md").read_bytes().decode(), DRAFT.replace("HOLD: waiting", "RELEASED-HOLD: waiting", 1))   # \r\n, the rest and the line past the head kept
            self.assertEqual((self.dr / "x1..md").read_text(), "RELEASED-HOLD:\nsecond draft\n")
            self.assertEqual((self.dr / "X2.md").read_text(), "HOLD: another item\n")   # another item's draft untouched
            hs = janus.item_holds(self.cfg); self.assertNotIn("x1", hs); self.assertEqual(hs.get("x2"), "another item")
            n = len(self.w.writes); o = self.set("X1", alias)   # again: nothing to do, nothing written
            self.assertIn("not held", o); self.assertNotIn("no longer held", o); self.assertEqual(len(self.w.writes), n)
        self.assertEqual(sorted(p.name for p in self.dr.iterdir()), ["X1.md", "X2.md", "x1..md"])   # no draft removed

    def test_dry_run_names_both_changes_and_makes_none(self):
        self.setup_holds(); self.cfg.run.dry = True
        o = self.set("X1", "hold=off")
        self.assertIn("bd update b-X1 --set-labels backlog,backlog-ready,actor:agent", o); self.assertIn("clears hold:parked", o); self.assertIn("b-x1.", o)
        self.assertIn("X1.md line 3: HOLD: waiting on the owner's design -> RELEASED-HOLD: waiting on the owner's design", o)
        self.assertIn("x1..md line 1: HOLD: held -> RELEASED-HOLD: held", o); self.assertIn("4 hold source(s)", o); self.assertIn("dry-run", o)
        self.assertEqual(self.w.writes, []); self.assertEqual((self.dr / "X1.md").read_text(), DRAFT.replace("\r\n", "\n"))
        self.assertIn("x1", janus.item_holds(self.cfg))

    def test_a_failed_label_update_names_what_still_holds_and_a_retry_finishes(self):
        """Catches: a beads failure reported as released, or the drafts rewritten while the label still holds the item."""
        self.setup_holds(); self.w.fail_update = True
        with self.assertRaises(janus.Err) as e: self.set("X1", "hold=off")
        m = str(e.exception)
        for x in ("still held", "hold:parked", "hold:old", "X1.md line 3", "x1..md line 1", "nothing changed", "again"): self.assertIn(x, m)
        self.assertEqual((self.dr / "X1.md").read_text(), DRAFT.replace("\r\n", "\n")); self.assertEqual(self.labels()["backlog:X1"], ["hold:parked"])
        self.w.fail_update = False; o = self.set("X1", "hold=off")
        self.assertIn("no longer held", o); self.assertNotIn("x1", janus.item_holds(self.cfg))

    def test_a_failed_draft_write_after_the_labels_is_honest_and_retried_safely(self):
        """Catches: 'no longer held' while a draft still holds the item, and a retry touching the cleared labels again."""
        self.setup_holds()
        def boom(p, text, mode=0o644): raise OSError(28, "No space left on device")
        with mock.patch.object(janus, "atomic_write", boom):
            with self.assertRaises(janus.Err) as e: self.set("X1", "hold=off")
        m = str(e.exception)
        for x in ("still held", "X1.md", "No space left", "released: beads b-X1 label hold:parked", "again"): self.assertIn(x, m)
        self.assertEqual(self.labels()["backlog:X1"], []); self.assertEqual(janus.item_holds(self.cfg).get("x1"), "waiting on the owner's design")
        updates = len([x for x in self.w.writes if x[0] == "update"])
        o = self.set("X1", "hold=off")
        self.assertIn("no longer held", o); self.assertEqual(len([x for x in self.w.writes if x[0] == "update"]), updates)   # labels not touched again
        self.assertNotIn("x1", janus.item_holds(self.cfg)); self.assertIn("RELEASED-HOLD: waiting", (self.dr / "X1.md").read_text())

    def test_an_unreadable_draft_refuses_before_any_change(self):
        """Catches: a draft that cannot be read taken for 'not held' during the release (item_holds' tolerant read swallows it)."""
        self.setup_holds(); (self.dr / "x1..md").write_bytes(b"HOLD: x\n\xff\xfe not text\n")
        with self.assertRaises(janus.Err) as e: self.set("X1", "hold=off")
        self.assertIn("x1..md", str(e.exception)); self.assertIn("nothing changed", str(e.exception))
        self.assertEqual(self.w.writes, []); self.assertEqual(self.labels()["backlog:X1"], ["hold:parked"]); self.assertEqual((self.dr / "X1.md").read_text(), DRAFT.replace("\r\n", "\n"))
        if os.geteuid() != 0:   # a permission-denied draft too
            (self.dr / "x1..md").write_text("HOLD: y\n"); (self.dr / "x1..md").chmod(0)
            try:
                with self.assertRaises(janus.Err) as e: self.set("X1", "hold=off")
                self.assertIn("x1..md", str(e.exception)); self.assertEqual(self.w.writes, [])
            finally: (self.dr / "x1..md").chmod(0o644)

    def test_release_keeps_the_existing_restrictions(self):
        """An item beads does not hold is refused as before (nothing written, its draft kept); setting a hold is unchanged."""
        (self.dr / "X9.md").write_text("HOLD: kept\n")
        with self.assertRaises(janus.Err) as e: self.set("X9", "hold=off")
        self.assertIn("no backlog item X9", str(e.exception)); self.assertEqual(self.w.writes, []); self.assertEqual((self.dr / "X9.md").read_text(), "HOLD: kept\n")
        self.w.issue("X1"); o = self.set("X1", "hold=Owner Review")
        self.assertIn("held (owner-review)", o); self.assertEqual(self.labels()["backlog:X1"], ["hold:owner-review"])


class HeldVisible(unittest.TestCase):
    """fill and status say which items a hold keeps out (reason, source, a waiting brief); a held item never launches."""
    def setUp(self):
        self.d, self.cfg = project(CONF); (self.d / "S.md").write_text(CHECK); self.cfg.hosts["here"].update(ram_gb=64, cores=16)
        self.w = World(); self.w.patch(self); self.hs = {"here": host_probe(name="here")}
        (self.d / ".janus/briefs/drafts").mkdir()
        self.w.issue("X1", labels=["hold:parked"]); self.w.issue("X2"); self.w.issue("X4"); self.w.issue("X5")
        self.w.ready = {"b-X1", "b-X2", "b-X4"}   # X5 waits on a dependency
        (self.d / ".janus/briefs/x1-a.md").write_text("# x1-a\nITEM: x1\n")       # held, with a usable brief
        (self.d / ".janus/briefs/drafts/X2.md").write_text("HOLD: needs the owner\n")   # held by its draft, no brief
        (self.d / ".janus/briefs/x5-a.md").write_text("# x5-a\nITEM: X5\n")       # blocked; X4: ready without a brief

    def test_held_items_are_explained_with_zero_candidates_and_never_launched(self):
        """Catches: a held item with a ready brief looking like nothing to do (no candidates, no reason), or being launched."""
        held, missing, blocked = [], [], []
        self.assertEqual(janus.fill_candidates(self.cfg, [], missing=missing, blocked=blocked, held=held), [])
        self.assertEqual(missing, ["X4"]); self.assertEqual(blocked, ["X5"])
        self.assertEqual([(i, r, b) for i, r, src, b in held], [("X1", "parked", "x1-a"), ("X2", "needs the owner", None)])
        self.assertIn("beads label hold:parked on b-X1", held[0][2][0]); self.assertIn("X2.md", held[1][2][0])
        plan, stops = janus.fill_plan(self.cfg, self.hs, [])
        s = " ".join(stops); self.assertEqual(plan, [])
        for x in ("2 held on purpose", "X1 (parked", "unlaunched brief x1-a waits", "X2 (needs the owner", "no usable brief", "janus backlog list", "ready without a usable brief: X4"): self.assertIn(x, s)
        self.assertNotIn("X5", s.split("held on purpose")[1])
        F = dict(janus.backlog_findings(self.cfg, self.hs, []))
        self.assertNotIn("idle-with-backlog", F); self.assertNotIn("X1", F.get("idle-with-backlog:nobrief", "")); self.assertNotIn("X2", F.get("idle-with-backlog:nobrief", ""))
        self.assertFalse(any("X1" in t or "X2" in t for k, t in F.items() if janus.FAMILIES.get(k.split(":")[0]) and janus.FAMILIES[k.split(":")[0]].recur))
        line = janus.checklist_line(janus.checklist_totals(self.cfg, []), self.cfg.run.briefless, self.cfg.run.held)   # what janus status prints
        self.assertIn("2 held on purpose", line); self.assertIn("unlaunched brief x1-a waits", line)
        launched = []
        with mock.patch.object(janus, "probe_all", lambda cfg: self.hs), mock.patch.object(janus, "herdr_agents", lambda cfg: {}), \
             mock.patch.object(janus, "cmd_launch", lambda cfg, a: launched.append(a.unit)):
            r, o = out(janus.cmd_fill, self.cfg, type("A", (), {"max": None})())
        self.assertEqual(launched, []); self.assertIn("held on purpose", o)
        o = out(janus.cmd_backlog, self.cfg, type("A", (), {"action": "list", "args": [], "unit": None})())[1]
        self.assertIn("held: parked (beads label hold:parked on b-X1); unlaunched brief x1-a waits", o); self.assertIn("held: needs the owner (HOLD: line in X2.md (drafts)); no usable brief", o)

    def test_the_summary_is_bounded(self):
        held = [(f"X{n}", "r" * 200, ["s" * 300], None) for n in range(20)]
        t = janus.held_text(held, 4); self.assertIn("20 held on purpose", t); self.assertIn("X3 (", t); self.assertNotIn("X4 (", t); self.assertLess(len(t), 1000)

    def test_a_released_item_is_eligible_only_under_the_existing_rules(self):
        """After hold=off the item is a candidate when beads lists it ready; dependency-blocked it stays blocked, not launched."""
        out(janus.cmd_backlog, self.cfg, type("A", (), {"action": "set", "args": ["X1", "hold=off"], "unit": None})())
        self.assertEqual([c[4] for c in janus.fill_candidates(self.cfg, [])], ["x1-a"])
        self.w.ready = {"b-X2", "b-X4"}; blocked = []
        self.assertEqual(janus.fill_candidates(self.cfg, [], blocked=blocked), []); self.assertIn("X1", blocked)
        self.assertIsNotNone(janus.item_eligible(self.cfg, "X1", []))


class Texts(unittest.TestCase):
    """idle:<host> is about non-backlog beads work; missing-brief messages name the configured briefs folder; the hook lets the
    orchestrator write briefs there and nowhere unrelated (no hook change)."""
    def test_idle_host_names_non_backlog_work_without_brief_advice(self):
        d, cfg = project(); hs = {"here": host_probe(avail_mb="64000", disk_gb="500", load="0")}
        with mock.patch.object(janus, "ready_work", lambda cfg: [{"title": "fix the flaky probe"}]), mock.patch.object(janus, "other_projects", lambda cfg: []), \
             mock.patch.object(janus, "room", lambda *a, **k: (2, "")):
            F = dict(janus.host_findings(cfg, hs, []))
        t = F["idle:here"]; self.assertIn("non-backlog beads work", t); self.assertIn("fix the flaky probe", t); self.assertNotIn("write a brief", t)
        # 2026-11-18: the host had agent room but its build capacity was full, so fill refused while check called it idle
        with mock.patch.object(janus, "ready_work", lambda cfg: [{"title": "fix the flaky probe"}]), mock.patch.object(janus, "other_projects", lambda cfg: []), \
             mock.patch.object(janus, "room", lambda *a, **k: (2, "")), mock.patch.object(janus, "build_room", lambda *a, **k: (False, "here build capacity full")):
            F = dict(janus.host_findings(cfg, hs, []))
        self.assertNotIn("idle:here", F)

    def test_missing_brief_messages_and_the_hook_use_the_configured_folder(self):
        for conf, rel in ((CONF, ".janus/briefs"), (CONF + '[paths]\nbriefs = "work/briefs"\n', "work/briefs")):
            d, cfg = project(conf); (d / "S.md").write_text(CHECK); (d / rel).mkdir(parents=True, exist_ok=True)
            w = World(); w.patch(self); w.issue("X1")
            F = dict(janus.backlog_findings(cfg, {"here": host_probe(name="here")}, []))
            self.assertIn(f"write {rel}/<unit>.md", F["idle-with-backlog:nobrief"])
            self.assertIn(f"{rel}/<unit>.md", " ".join(janus.fill_plan(cfg, {"here": host_probe(name="here")}, [])[1]))
            self.assertIn(f"write {rel}/<unit>.md", " ".join(M.backlog_rows({"items": [{"id": "b-X1", "title": "backlog: X1", "labels": ["backlog", "backlog-ready"], "external_ref": "backlog:X1"}],
                                                                             "ready": [{"id": "b-X1"}]}, [], time.time(), False, (), ["X1"], rel)[0].detail))
            why = lambda fp: janus.pretool_why(cfg, {"tool_name": "Write", "tool_input": {"file_path": str(fp)}, "cwd": str(d)})
            for ok in (d / rel / "x1-a.md", d / rel / "drafts/X1.md", d / ".janus/briefs/x1-b.md"): self.assertIsNone(why(ok), ok)
            for bad in (d / "src/main.py", d / "work/other.md", d.parent / "elsewhere.md"): self.assertIsNotNone(why(bad), bad)
            e = janus.pretool_why(cfg, {"tool_name": "Edit", "tool_input": {"file_path": f"{rel}/x1-a.md"}, "cwd": str(d)}); self.assertIsNone(e)   # relative to the cwd


if __name__ == "__main__":
    unittest.main()
