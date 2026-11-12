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


class Item13Handover(unittest.TestCase):
    def test_close_force_allows_the_unit_when_own_tree_holds(self):
        """Catches: janus close <u> --force was refused although own_tree() held, because the `2` of `2>&1` counted as a second
        unit name (the shell tokenizer leaves it in the words)."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-h13-")); R = base / "repo"; R.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g("init", "-q"); (R / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
        W = base / "wt"; g("worktree", "add", "-q", str(W), "-b", "unit")
        d, cfg = project()
        u = {"id": "t-1", "name": "u1", "cwd": str(W)}
        real = janus.Beads; janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u]})
        try:
            self.assertTrue(janus.force_close_ok(cfg, ["close", "u1", "--force", "--reason", "a; b", "2"]))
        finally: janus.Beads = real

    def test_resume_of_takes_the_predecessors_cwd(self):
        """Catches: 'janus launch <successor> --resume-of <u> --cwd <its tree>' was refused by H9, so no route handed a unit's
        dirty worktree to a successor."""
        d, cfg = project(); W = d / "wt"; W.mkdir()
        units = [{"id": "t-1", "name": "u1", "cwd": str(W)}]
        self.assertEqual(janus.cwd_taken(cfg, units, W.resolve(), "u2"), (["u1"], None))
        self.assertEqual(janus.cwd_taken(cfg, units, W.resolve(), "u2", resume_of="u1"), ([], "u1"))


class Item12FinalGate(unittest.TestCase):
    def test_final_gate_substitutes_unit_tree_and_branch(self):
        """Catches: janus land took one fixed [landing] final_gate command, so a project that gates per unit could not use it."""
        ld = {"final_gate": "bin/run-handoff.sh out/land-{unit}-linux --tree {wt} --branch {branch}"}
        self.assertEqual(janus.final_gate_cmd(ld, "u1", "/w/u1", "wk/u1"),
                         "bin/run-handoff.sh out/land-u1-linux --tree /w/u1 --branch wk/u1")
        self.assertEqual(janus.final_gate_cmd({}, "u", "/w", "b"), "")


class Item9ResumeCard(unittest.TestCase):
    def test_resume_posts_the_card_as_waiting_with_the_new_task(self):
        """Catches: a unit resumed with janus resume --message whose run is queued in pueue (agents slot full) showed board GONE
        (relaunch it), because resume did not post the card with the new run."""
        d, cfg = project('\n[providers.api]\nenabled = true\n[models.m]\nprovider = "api"\nkind = "api"\n')
        b = d / "briefs/u1.md"; b.write_text("# Brief: u1\n")
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "model": "m", "task": 5, "budget": 2.0, "run": "u1.1",
             "brief": str(b), "rbrief": str(b), "rcwd": str(d), "started": 0}
        posts = []
        saved = {k: getattr(janus, k) for k in ("session_of", "run_cost", "ledger_done", "reserve_budget", "unit_spent", "X", "confirm_started", "save_task_log", "board")}
        janus.session_of = lambda cfg, u: "sid"; janus.run_cost = lambda *a, **k: (0.1, True); janus.ledger_done = lambda *a, **k: None
        janus.reserve_budget = lambda cfg, mc, name, budget, run=None: "u1.r2.1"; janus.unit_spent = lambda cfg, u, wrapup=False: 0.0
        janus.X = lambda cfg, h, cmd, **k: (0, "7", ""); janus.confirm_started = lambda cfg, h, tid: ("running", ""); janus.save_task_log = lambda *a, **k: None
        janus.board = lambda cfg, *args, env=None, **k: posts.append((args, env))
        try: janus.resume_unit(cfg, u, "your jobs finished")
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertEqual(u["task"], 7)
        self.assertTrue(any(a[0] == "post" and a[2] == "waiting" and (e or {}).get("BOARD_CHECK") == "pueue here:7" for a, e in posts), posts)


class Item10UnitChanges(unittest.TestCase):
    def test_a_staged_landing_merge_does_not_count_the_branchs_files(self):
        """Catches: a unit holding a staged merge of the landing branch counted all of the branch's files as its changes, so it
        overlapped every other such unit."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u10-")); R = base / "repo"; R.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main"); (R / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
        g("checkout", "-q", "-b", "unit"); (R / "a").write_text("1x\n"); g("commit", "-qam", "unit change")
        g("checkout", "-q", "main"); (R / "c").write_text("2\n"); g("add", "."); g("commit", "-qm", "main adds c")
        g("checkout", "-q", "unit"); (R / "a").write_text("1y\n")   # the unit's staged work
        g("merge", "--no-commit", "--no-ff", "main")                 # the landing branch merged and staged
        d, cfg = project()
        u = {"id": "t-1", "name": "u1", "host": "here", "cwd": str(R)}
        hs = {"here": probe_of()}
        br, fs = janus.unit_changes(cfg, u, [u])
        self.assertIn("a", fs); self.assertNotIn("c", fs, "the landing branch's own file counted as the unit's change")


class Item8ResumeDryRun(unittest.TestCase):
    def test_dry_run_says_would_prompt(self):
        """Catches: janus resume --dry-run <herdr unit> printed '<unit>: prompted in its herdr pane' although nothing was prompted."""
        d, cfg = project(); cfg.run.dry = True
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "herdr", "pane": "p1", "model": "m", "started": 0}
        called = []; real = janus.herdr; janus.herdr = lambda *a, **k: called.append(a)
        try: line = janus.resume_unit(cfg, u, "go")
        finally: janus.herdr = real
        self.assertIn("would prompt", line); self.assertNotIn(": prompted", line)
        self.assertEqual(called, [], "the dry run prompted the pane")


class Item14OldOrch(unittest.TestCase):
    def test_old_orch_processes_are_listed_and_retired_only_with_a_yes(self):
        """Catches: janus migrate left the old orch's standing processes (fleet loops, orch.py run waiters) running beside janus
        with no way to see or stop them."""
        ps = (" 101     1 /usr/bin/python3 /Users/x/orch.py run --wt /w\n"
              " 102     1 /usr/bin/python3 /Users/x/janus.py watch\n"
              " 103     1 /bin/sh bin/fleet-loops.sh\n")
        self.assertEqual([p for p, _ in janus.old_orch_procs(ps)], [101])
        out = janus.retire_old_orch(dry=False, retire=False, ps_text=ps)
        self.assertTrue(any("101" in l for l in out)); self.assertTrue(any("not stopped" in l for l in out))
        kills = []; real = janus.run; janus.run = lambda cmd, **k: (kills.append(list(cmd)), 0, "", "")[1:]
        try: janus.retire_old_orch(dry=False, retire=True, ps_text=ps)
        finally: janus.run = real
        self.assertEqual(kills, [["kill", "-TERM", "101"]])


class Item15ReadyWork(unittest.TestCase):
    def test_log_beads_are_not_ready_work(self):
        """Catches: backlog.ready_work counted the decisions-log bead ('orch: decisions' when the old orch made it, label
        orch-log), so idle:<host> fired with '1 unit(s) are ready' when nothing was."""
        d, cfg = project()
        items = [{"title": "orch: decisions", "labels": ["orch-log"]}, {"title": "janus: decisions", "labels": ["janus-log"]},
                 {"title": "unit:x", "labels": ["unit"]}, {"title": "backlog: A1", "labels": ["backlog"]},
                 {"title": "instr: do it", "labels": ["instruction"]}, {"title": "real work", "labels": []}]
        real = janus.Beads; janus.Beads = type("B", (), {"__init__": lambda s, c: None, "run": lambda s, *a: items})
        try: got = [i["title"] for i in janus.ready_work(cfg)]
        finally: janus.Beads = real
        self.assertEqual(got, ["real work"])


class Item16RelatedRepos(unittest.TestCase):
    def test_related_compares_only_units_in_the_same_repository(self):
        """Catches (item 16a): related:<a>:<b> matched a module name across different repositories (two projects that both change
        a docs module)."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u16a-"))
        def repo(n):
            R = base / n; R.mkdir()
            g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
            g("init", "-q", "-b", "main"); (R / "docs").mkdir(); (R / "docs/a.md").write_text("x\n"); g("add", "."); g("commit", "-qm", "a")
            return R, g
        R1, g1 = repo("r1")
        W1 = base / "w1"; g1("worktree", "add", "-q", str(W1), "-b", "u1"); (W1 / "docs/a.md").write_text("y\n")
        W2 = base / "w2"; g1("worktree", "add", "-q", str(W2), "-b", "u2"); (W2 / "docs/a.md").write_text("y2\n")
        R2, _ = repo("r2"); (R2 / "docs/a.md").write_text("z\n")
        d, cfg = project()
        units = [{"id": "t-1", "name": "u1", "host": "here", "cwd": str(W1), "state": "working"},
                 {"id": "t-2", "name": "u2", "host": "here", "cwd": str(W2), "state": "working"},
                 {"id": "t-3", "name": "u3", "host": "here", "cwd": str(R2), "state": "working"}]
        saved = {k: getattr(janus, k) for k in ("all_units", "gate_results", "learned_posts")}
        janus.all_units = lambda cfg: {}; janus.gate_results = lambda cfg: {}; janus.learned_posts = lambda cfg: []
        try: cases = janus.related_cases(cfg, units)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertEqual(sorted(cases), [("u1", "u2")], "units in different repositories were called related")


class Item16NewWorktree(unittest.TestCase):
    def test_new_worktree_makes_a_fresh_branch(self):
        """Catches (item 16b): janus launch had no fresh-worktree option (without --cwd it defaulted to the project state root)."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u16b-")); R = base / "repo"; R.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main"); (R / "f").write_text("x\n"); g("add", "."); g("commit", "-qm", "a")
        d, cfg = project(); wt = d / "worktrees/u1"
        janus.new_worktree(R, wt, "u1", "main")
        self.assertTrue((wt / "f").is_file())
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, "u1")


class Item16Budget(unittest.TestCase):
    def test_budget_ignores_tiny_and_verdictless_runs(self):
        """Catches (item 16c): the default budget for implement/normal was $0.50 from a median of $0.09 over 23 runs that ended
        without a verdict, so a real unit started at a cap (the waste cause)."""
        d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 15\nledger = "out/ledger.tsv"\nkey_file = "k"\n[models.m]\nprovider = "api"\nkind = "api"\n')
        mc = cfg.models["m"]; runs = {}; au = {}
        for i in range(23):
            n = f"tiny{i}"; (d / "out" / n).mkdir(parents=True); (d / "out" / n / "REPORT.md").write_text("VERDICT: PENDING\n")
            runs[f"{n}.1"] = {"done": True, "cost": 0.09, "unit": n}; au[n] = {"name": n, "kind": "implement", "tier": "normal", "run": f"{n}.1", "status": "closed"}
        for i in range(3):
            n = f"real{i}"; (d / "out" / n).mkdir(parents=True); (d / "out" / n / "REPORT.md").write_text("VERDICT: DONE\ndone\n")
            runs[f"{n}.1"] = {"done": True, "cost": 2.0, "unit": n}; au[n] = {"name": n, "kind": "implement", "tier": "normal", "run": f"{n}.1", "status": "closed"}
        cfg.run.all_units = au
        real = janus.ledger_read; janus.ledger_read = lambda cfg, pv: (runs, [], [])
        try: b, why = janus.budget_for(cfg, mc, "implement", "normal")
        finally: janus.ledger_read = real
        self.assertGreaterEqual(b, 2.0, f"the tiny/verdictless runs skewed the budget: {b} ({why})")


if __name__ == "__main__": unittest.main()
