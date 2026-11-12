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

    def test_final_gate_quotes_substitutions_and_leaves_shell_values(self):
        """Catches (finding 7): {unit}, {wt} and {branch} went unquoted into a shell command, so a hostile branch name or a
        path with a space split it (an agent names its own branch, and the command runs on the final-gate host), and a ${...}
        in the config was mangled (${wt} became $/w)."""
        ld = {"final_gate": "bin/gate.sh {unit} {branch} --wt {wt}"}
        self.assertEqual(janus.final_gate_cmd(ld, "u1", "/Users/me/My Code/wt", "x;touch${IFS}/tmp/pwned"),
                         "bin/gate.sh u1 'x;touch${IFS}/tmp/pwned' --wt '/Users/me/My Code/wt'")
        self.assertEqual(janus.final_gate_cmd({"final_gate": "cd ${wt} && ./gate ${unit}"}, "u1", "/w", "b"),
                         "cd ${wt} && ./gate ${unit}")


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
    def test_old_orch_standing_loops_are_listed_not_waiters_or_editors(self):
        """Catches (finding 4): migrate matched any process with an `orch` token (a job waiter, an ssh, an editor) and never
        listed the old orch's standing loops (fleet-loops.sh, handoff-queue.sh, orch-backup.sh, wip-snapshot.sh, its watcher),
        and cut the list at 100 characters."""
        ps = (" 101 1 /bin/zsh /Users/x/proj/bin/fleet-loops.sh\n"
              " 102 1 /bin/zsh /Users/x/proj/bin/handoff-queue.sh w1 /Users/x/proj/out/handoff-queue-linux2\n"
              " 103 1 /bin/zsh /Users/x/proj/bin/orch-backup.sh 30\n"
              " 104 1 /bin/zsh /Users/x/proj/bin/wip-snapshot.sh 30\n"
              " 105 1 /bin/zsh /Users/x/proj/bin/watch-stream.sh\n"
              " 106 1 /usr/bin/python3 /Users/x/proj/orch.py run --host h --lane jobs --wt /w -- cargo test\n"
              " 107 1 /bin/zsh /Users/x/proj/bin/pueue wait -q 9\n"
              " 108 1 ssh host cd /Users/x/proj && ls\n"
              " 109 1 /usr/bin/vim /Users/x/proj/orch.py\n"
              " 111 1 /usr/bin/python3 /Users/x/proj/janus.py watch\n")
        self.assertEqual([p for p, _ in janus.old_orch_procs(ps)], [101, 102, 103, 104, 105])
        out = janus.retire_old_orch(dry=False, retire=False, ps_text=ps)
        self.assertTrue(any("101" in l for l in out)); self.assertTrue(any("not stopped" in l for l in out))
        self.assertTrue(any("handoff-queue-linux2" in l for l in out), "the command line was cut before its argument")
        kills = []; real = janus.run; janus.run = lambda cmd, **k: (kills.append(list(cmd)), 0, "", "")[1:]
        try: janus.retire_old_orch(dry=False, retire=True, ps_text=ps)
        finally: janus.run = real
        self.assertEqual(kills, [["kill", "-TERM", str(p)] for p in (101, 102, 103, 104, 105)])

    def test_old_orch_procs_are_scoped_to_this_project(self):
        """Catches (finding 4): another project's old-orch loop was listed for this project's migrate."""
        root = "/Users/x/proj"
        ps = (" 101 1 /bin/zsh /Users/x/proj/bin/fleet-loops.sh\n"
              " 110 1 /bin/zsh /other/proj/bin/fleet-loops.sh\n")
        self.assertEqual([p for p, _ in janus.old_orch_procs(ps, root)], [101])


class Item15ReadyWork(unittest.TestCase):
    def test_only_the_toolkit_log_beads_are_excluded(self):
        """Catches (finding 8): ready_work dropped any bead whose title or label merely contains log ('recorder: log every
        dropped frame', 'api: decision table for retries', an audit-log label), hiding real work."""
        d, cfg = project()
        items = [{"title": "orch: decisions", "labels": ["orch-log"]}, {"title": "janus: decisions", "labels": ["janus-log"]},
                 {"title": "unit:x", "labels": ["unit"]}, {"title": "backlog: A1", "labels": ["backlog"]},
                 {"title": "instr: do it", "labels": ["instruction"]},
                 {"title": "recorder: log every dropped frame", "labels": []},
                 {"title": "station: log rotation is broken", "labels": []},
                 {"title": "api: decision table for retries", "labels": []},
                 {"title": "audit bead", "labels": ["audit-log"]}, {"title": "error bead", "labels": ["error-log"]},
                 {"title": "real work", "labels": []}]
        real = janus.Beads; janus.Beads = type("B", (), {"__init__": lambda s, c: None, "run": lambda s, *a: items})
        try: got = [i["title"] for i in janus.ready_work(cfg)]
        finally: janus.Beads = real
        self.assertEqual(got, ["recorder: log every dropped frame", "station: log rotation is broken",
                               "api: decision table for retries", "audit bead", "error bead", "real work"])


class Item16RelatedRepos(unittest.TestCase):
    def test_related_compares_only_units_in_the_same_repository(self):
        """Catches (item 16a): related:<a>:<b> matched a module name across different repositories (two projects that both change
        a docs module)."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u16a-"))
        def repo(n):
            R = base / n; R.mkdir()
            g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
            g("init", "-q", "-b", "main"); (R / "docs").mkdir(); (R / "docs/a.md").write_text(n + "\n"); g("add", "."); g("commit", "-qm", "a")
            return R, g
        R1, g1 = repo("r1")
        W1 = base / "w1"; g1("worktree", "add", "-q", str(W1), "-b", "u1"); (W1 / "docs/a.md").write_text("y\n")
        W2 = base / "w2"; g1("worktree", "add", "-q", str(W2), "-b", "u2"); (W2 / "docs/a.md").write_text("y2\n")
        R2, _ = repo("r2"); (R2 / "docs/a.md").write_text("z\n")   # a different repository (its own history)
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


class FakeBeads:
    """A beads stand-in for a launch fixture: no database, no unit ever recorded."""
    def __init__(self, cfg): pass
    def units(self): return []
    def create(self, title, *a): return title
    def run(self, *a): return []
    def save(self, u): pass
    def comment(self, *a): pass


def launch_cfg(extra=""):
    d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 1\nledger = "out/ledger.tsv"\nkey_file = "k"\n'
                     '[models.m]\nprovider = "api"\nkind = "api"\napi_id = "x"\n' + extra)
    return d, cfg


def git_repo(prefix):
    base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-" + prefix + "-")); R = base / "repo"; R.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
    g("init", "-q", "-b", "main"); (R / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
    return base, R, g


class Item2VerdictEnding(unittest.TestCase):
    def test_a_report_ending_waiting_is_not_finished(self):
        """Catches (finding 2): units.verdict returned an earlier VERDICT: DONE although the report's last VERDICT line was
        WAITING, so the unit read as finished and its jobs became orphans (cancelled)."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True)
        (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\nVERDICT: DONE - items 1-6\n(more work)\nWAITING here:9\nVERDICT: WAITING\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        (d / "out/u1/report.json").write_text(json.dumps({"verdict": "DONE"}))   # a stale json beside the prose
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u.get("_park"), ["here:9"]); self.assertNotIn("unit:u1:finished", [k for k, _ in F])


class Item1ParkedAfterDone(unittest.TestCase):
    def test_a_park_line_after_a_done_verdict_means_parked(self):
        """Catches (finding 1, high): units.verdict read the last VERDICT: DONE while a plain WAITING park line followed it, so a
        unit sent more work (janus resume --message) was judged finished and its still-running job was cancelled as an orphan."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True)
        (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\n## phase 1\nVERDICT: DONE - items 1-6\n## phase 2\nmore work\nWAITING here:9\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}},
                                               "9": {"status": {"Running": {}}, "label": "unit:u1@" + janus.project_id(cfg)}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u.get("_park"), ["here:9"]); self.assertEqual(u.get("state"), "parked")
        self.assertNotIn("unit:u1:finished", [k for k, _ in F])
        self.assertEqual([r for r, _ in janus.orphan_jobs(cfg, hs, [u])], [], "the parked unit's job was orphaned (cancelled)")

    def test_the_last_of_verdict_line_and_park_line_decides(self):
        """Catches (finding 1, high): the rule is 'the last of {VERDICT line, WAITING line} decides' for EVERY unit; a closing
        VERDICT: DONE after an earlier park line is finished (no false park), and a park line after it is parked."""
        d, cfg = project(); (d / "out/u1").mkdir(parents=True); p = d / "out/u1/REPORT.md"
        p.write_text("VERDICT: PENDING\nWAITING here:9\nVERDICT: DONE - all items\n")
        self.assertEqual(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}), "VERDICT: DONE - all items")
        p.write_text("VERDICT: DONE - all items\nWAITING here:9\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        p.write_text("VERDICT: DONE - all items\nWAITING here:9\nVERDICT: DONE - all done\n")
        self.assertEqual(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}), "VERDICT: DONE - all done")


class Item2ResumeGuard(unittest.TestCase):
    """Finding 2 (medium-high): `janus resume <u> --message ...` had no guard: it started a second agent beside a running one
    and marked a still-running job handled (so it became an orphan, cancelled, when the new run parked again)."""

    def setUp(self):
        self.d, self.cfg = project()
        self.calls = []
        self.saved = {k: getattr(janus, k) for k in ("Beads", "probe_all", "herdr_agents", "unit_state", "job_state", "resume_unit", "waiting_line_jobs")}

    def tearDown(self):
        for k, v in self.saved.items(): setattr(janus, k, v)

    def _run(self, u, **kw):
        janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: kw.get("units", [u]),
                                     "comment": lambda s, *a: None, "save": lambda s, x: None})
        janus.probe_all = lambda cfg: {"here": probe_of()}
        janus.herdr_agents = lambda cfg: {}
        janus.unit_state = lambda x, hs, ag: kw.get("state", "gone")
        janus.job_state = lambda hs, j: (kw.get("job", "done"), "")
        janus.waiting_line_jobs = lambda cfg, x: kw.get("jobs", ["here:9"])
        janus.resume_unit = lambda cfg, x, msg, budget=None: (self.calls.append(("resume", msg, budget)), "resumed")[1]
        a = type("A", (), {"unit": u["name"], "message": kw.get("message", "go"), "force": kw.get("force", False),
                           "budget": kw.get("budget"), "runtime": None, "here": False})()
        janus.cmd_resume(self.cfg, a)
        return u

    def test_message_refuses_while_the_agent_runs(self):
        """Catches: `resume --message` with a Running agent task started a second agent on the same session in the same tree."""
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(self.d / "wt")}
        with self.assertRaises(janus.Err) as e: self._run(u, state="working", job="running", message="go")
        self.assertIn("still working", str(e.exception)); self.assertIn("--force", str(e.exception))
        self.assertEqual(self.calls, [], "a second agent was started beside the running one")

    def test_message_refuses_while_its_jobs_are_unfinished(self):
        """Catches: the jobs guard was skipped entirely with --message, so a unit whose job still ran was resumed."""
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(self.d / "wt")}
        with self.assertRaises(janus.Err) as e: self._run(u, state="gone", job="running", message="go")
        self.assertIn("here:9", str(e.exception)); self.assertIn("not finished", str(e.exception)); self.assertEqual(self.calls, [])

    def test_a_forced_resume_never_marks_a_running_job_handled(self):
        """Catches: resume added every park-line job to handled_jobs even while it ran, so the next park line's same job was an
        orphan (cancelled)."""
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(self.d / "wt")}
        self._run(u, state="gone", job="running", message="go", force=True)
        self.assertEqual(len(self.calls), 1, "the forced resume did not run")
        self.assertNotIn("here:9", u.get("handled_jobs", []), "a still-running job was marked handled")

    def test_resume_refuses_the_tree_of_another_open_unit(self):
        """Catches: resume had no own-tree check, so a predecessor whose handover was refused was put back into the tree its
        successor now works in."""
        W = self.d / "wt"; W.mkdir()
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(W)}
        u2 = {"id": "t-2", "name": "u2", "host": "here", "cwd": str(W)}
        with self.assertRaises(janus.Err) as e: self._run(u, units=[u, u2], state="gone", job="done", message="go")
        self.assertIn("u2", str(e.exception)); self.assertEqual(self.calls, [])


class Item3Handover(unittest.TestCase):
    def _close(self, cfg):
        return janus.cmd_close(cfg, type("A", (), {"unit": "u1", "force": True, "handover": True, "reason": "handover"})())

    def _beads(self, u):
        return type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u], "comment": lambda s, *a: None,
                              "run": lambda s, *a: [], "save": lambda s, x: None, "close": lambda s, *a, **k: None})

    def test_handover_refuses_when_the_remote_copy_has_uncollected_work(self):
        """Catches (finding 3): launch --resume-of closed a remote predecessor with force, skipping the uncollected guard, and
        deleted its tree and output (its dirty tree was its copy on the host, not the successor's)."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "cwd": "/w/u1"}
        saved = {k: getattr(janus, k) for k in ("Beads", "uncollected", "X", "save_task_log", "board", "note_event", "herdr")}
        janus.Beads = self._beads(u)
        janus.uncollected = lambda cfg, u: "its copy on far has changes or output not collected yet"
        calls = []; janus.X = lambda cfg, h, cmd, **k: (calls.append(cmd), (0, "", ""))[1]
        janus.save_task_log = lambda *a, **k: None; janus.board = lambda *a, **k: None; janus.note_event = lambda *a, **k: None
        janus.herdr = lambda *a, **k: None
        try:
            with self.assertRaises(janus.Err) as e: self._close(cfg)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn("collect", str(e.exception))
        self.assertFalse(any("rm -rf" in c for c in calls), "the remote tree was deleted")

    def test_handover_refuses_a_predecessor_whose_agent_still_runs(self):
        """Catches (finding 3): nothing checked that the predecessor's agent had ended, so a running predecessor was closed in
        beads and kept running unowned in the tree the successor now works in."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "cwd": "/w/u1"}
        saved = {k: getattr(janus, k) for k in ("Beads", "uncollected", "probe_all", "herdr_agents", "unit_state", "X")}
        janus.Beads = self._beads(u)
        janus.uncollected = lambda cfg, u: None
        janus.probe_all = lambda cfg: {}; janus.herdr_agents = lambda cfg: {}; janus.unit_state = lambda u, hs, ag: "working"
        calls = []; janus.X = lambda cfg, h, cmd, **k: (calls.append(cmd), (0, "", ""))[1]
        try:
            with self.assertRaises(janus.Err) as e: self._close(cfg)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn("still", str(e.exception)); self.assertFalse(any("rm -rf" in c for c in calls))


class Item5ReportSync(unittest.TestCase):
    def _remote(self, state="done", via="pueue", **kw):
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        (d / "out/u1").mkdir(parents=True); (d / "out/u1/REPORT.md").write_text("VERDICT: PENDING\n")
        u = {"id": "t-1", "name": "u1", "host": "far", "via": via, "task": 5, "model": "m", "started": 0, "state": state, **kw}
        return d, cfg, u

    def _bd(self): return type("B", (), {"save": lambda self, u: None})()

    def test_a_failed_report_sync_never_orphans_a_remote_unit(self):
        """Catches (finding 5): when the report sync failed, the orphan decision was taken on the stale local report and a
        parked remote unit's jobs were cancelled."""
        d, cfg, u = self._remote()
        hs = {"far": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}},
                                               "9": {"status": {"Running": {}}, "label": "unit:u1@t"}}})}
        saved = janus.save_output, janus.remote_patch
        janus.remote_patch = lambda *a, **k: None; janus.save_output = lambda cfg, u: False
        try:
            janus.sync_remote_outputs(cfg, [u], self._bd())
            got = [r for r, _ in janus.orphan_jobs(cfg, hs, [u])]
        finally: janus.save_output, janus.remote_patch = saved
        self.assertEqual(got, [], "a failed sync let a remote unit's jobs be cancelled")

    def test_the_end_of_turn_backup_patch_is_written_by_the_sync(self):
        """Catches (finding 5): the sync marked the unit saved before refresh_remote wrote the end-of-turn backup.patch, so it
        was not written until the periodic backup."""
        d, cfg, u = self._remote()
        calls = []; saved = janus.save_output, janus.remote_patch
        janus.remote_patch = lambda cfg, h, n, f: calls.append((n, f)); janus.save_output = lambda cfg, u: True
        try: janus.sync_remote_outputs(cfg, [u], self._bd())
        finally: janus.save_output, janus.remote_patch = saved
        self.assertIn(("u1", "backup.patch"), calls)

    def test_a_remote_tmux_unit_is_synced_too(self):
        """Catches (finding 5): a remote tmux unit has no pueue task, so `saved == task` was None == None and it was never
        synced by this path."""
        d, cfg, u = self._remote(via="tmux"); u.pop("task")
        got = []; saved = janus.save_output, janus.remote_patch
        janus.remote_patch = lambda *a, **k: None; janus.save_output = lambda cfg, u: (got.append(u["name"]), True)[1]
        try: janus.sync_remote_outputs(cfg, [u], self._bd())
        finally: janus.save_output, janus.remote_patch = saved
        self.assertEqual(got, ["u1"])


class Item9BudgetHistory(unittest.TestCase):
    def test_budget_counts_capped_cheap_and_tidied_runs(self):
        """Catches (finding 9): capped runs (which show the budget was too small), a finished run whose report was tidied, and
        cheap finished runs were all dropped from the history."""
        d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 15\nledger = "out/ledger.tsv"\nkey_file = "k"\n'
                         '[models.m]\nprovider = "api"\nkind = "api"\n')
        mc = cfg.models["m"]; runs = {}; au = {}
        def add(n, cost, kind, report=None, capped=False):
            (d / "out" / n).mkdir(parents=True)
            if report is not None: (d / "out" / n / "REPORT.md").write_text(report)
            if capped: (d / "out" / n / "api.json").write_text(json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": cost}))
            runs[f"{n}.1"] = {"done": True, "cost": cost, "unit": n, "budget": cost, "how": "done rc=0"}
            au[n] = {"name": n, "kind": kind, "tier": "normal", "run": f"{n}.1", "status": "closed"}
        for i in range(3): add(f"cap{i}", 5.0, "implement", "VERDICT: PENDING\n", capped=True)
        for i, c in enumerate((1.1, 1.2, 1.4)): add(f"fin{i}", c, "implement", "VERDICT: DONE\ndone\n")
        add("tidied", 3.0, "implement")   # its out/ folder was tidied: no REPORT.md at all
        for i, c in enumerate((0.30, 0.40, 0.45)): add(f"rev{i}", c, "review", "VERDICT: DONE\ndone\n")
        cfg.run.all_units = au
        real = janus.ledger_read; janus.ledger_read = lambda cfg, pv: (runs, [], [])
        try:
            b, why = janus.budget_for(cfg, mc, "implement", "normal")
            br, whyr = janus.budget_for(cfg, mc, "review", "normal")
        finally: janus.ledger_read = real
        self.assertGreaterEqual(b, 3.0, f"capped/tidied runs were dropped: {b} ({why})")
        self.assertLess(br, 15, f"the cheap review runs never taught it: {br} ({whyr})")


class Item10Clones(unittest.TestCase):
    def test_two_clones_of_one_repo_are_related(self):
        """Catches (finding 10): two clones of one repository compared different (git common dir), so units in separate clones
        were never related."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u10c-")); R = base / "r"; R.mkdir()
        g = lambda c, *a: subprocess.run(["git", "-C", str(c), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g(R, "init", "-q", "-b", "main"); (R / "docs").mkdir(); (R / "docs/a.md").write_text("x\n"); g(R, "add", "."); g(R, "commit", "-qm", "a")
        C = base / "clone"; g(base, "clone", "-q", str(R), str(C))
        W1 = base / "w1"; g(R, "worktree", "add", "-q", str(W1), "-b", "u1"); (W1 / "docs/a.md").write_text("y\n")
        W2 = base / "w2"; g(C, "worktree", "add", "-q", str(W2), "-b", "u2"); (W2 / "docs/a.md").write_text("y2\n")
        d, cfg = project()
        units = [{"id": "t-1", "name": "u1", "host": "here", "cwd": str(W1), "state": "working"},
                 {"id": "t-2", "name": "u2", "host": "here", "cwd": str(W2), "state": "working"}]
        saved = {k: getattr(janus, k) for k in ("all_units", "gate_results", "learned_posts")}
        janus.all_units = lambda cfg: {}; janus.gate_results = lambda cfg: {}; janus.learned_posts = lambda cfg: []
        try: cases = janus.related_cases(cfg, units)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn(("u1", "u2"), cases, "two clones of one repository were not related")


class Item11CommitStateAdd(unittest.TestCase):
    def test_a_file_that_fails_to_add_is_an_error(self):
        """Catches (finding 11): commit-state reported success ('committing what was staged') while a file that failed to add
        every time was left out."""
        d = Path(tempfile.mkdtemp(prefix="janus-fix-")); (d / "janus.toml").write_text('[project]\nname = "t"\n'); (d / "out").mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True, check=True)
        g("init", "-q"); (d / "a").write_text("1\n"); g("add", "."); g("commit", "-qm", "a")
        (d / "b").write_text("2\n")
        cfg = janus.Cfg(d)
        real_sh, real_push = janus.sh, janus.push_cmd
        def sh_stub(cmd, **k):
            if cmd[:3] == ["git", "add", "-A"]: return 128, "", 'error: open("b"): Permission denied'
            return real_sh(cmd, **k)
        janus.sh = sh_stub; janus.push_cmd = lambda repo, cfg=None: ["true"]
        try:
            with self.assertRaises(janus.Err): janus.cmd_commit_state(cfg, type("A", (), {"message": "state"})())
        finally: janus.sh, janus.push_cmd = real_sh, real_push


class Item12NewWorktree(unittest.TestCase):
    def _ns(self, unit, **kw):
        import argparse
        base = dict(unit=unit, kind="implement", tier="normal", host="here", model="m", brief=None, cwd=None, budget=None,
                    test_host=None, allow_main=True, of=None, report=None, resume_of=None, item=None,
                    probes={"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "100", "load": "0"}})
        base.update(kw); return argparse.Namespace(**base)

    def test_a_refused_launch_leaves_no_worktree_or_branch(self):
        """Catches (finding 12): --new-worktree created the tree and branch before the launch could still be refused (the brief
        lint), leaving them behind so the same command then failed with git's 'a branch named ... already exists'."""
        base, R, g = git_repo("u12a")
        d, cfg = launch_cfg('\n[quality]\nbrief_lint = true\n')
        b = d / "briefs/u1.md"; b.write_text("just some words with no markers\n")
        saved = janus.Beads, janus.herdr; janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None
        try:
            with self.assertRaises(janus.Err): janus.cmd_launch(cfg, self._ns("u1", brief=str(b), new_worktree=str(R)))
        finally: janus.Beads, janus.herdr = saved
        self.assertFalse((d / "worktrees/u1").exists(), "a refused launch left the worktree behind")
        self.assertNotEqual(g("rev-parse", "-q", "--verify", "u1").returncode, 0, "a refused launch left the branch behind")

    def test_cwd_with_new_worktree_is_an_error(self):
        """Catches (finding 12): --cwd given with --new-worktree was silently ignored."""
        base, R, g = git_repo("u12b")
        d, cfg = launch_cfg('\n[quality]\nbrief_lint = true\n')
        b = d / "briefs/u1.md"; b.write_text("just some words with no markers\n")
        saved = janus.Beads, janus.herdr; janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None
        try:
            with self.assertRaises(janus.Err) as e: janus.cmd_launch(cfg, self._ns("u1", brief=str(b), cwd=str(d), new_worktree=str(R)))
        finally: janus.Beads, janus.herdr = saved
        self.assertIn("--cwd", str(e.exception))

    def test_from_defaults_to_the_landing_branch(self):
        """Catches (finding 12): --from defaulted to main, not [landing] branch."""
        base, R, g = git_repo("u12c")
        g("checkout", "-q", "-b", "develop"); (R / "d").write_text("d\n"); g("add", "."); g("commit", "-qm", "develop"); g("checkout", "-q", "main")
        d, cfg = launch_cfg('\n[landing]\nbranch = "develop"\n[models.c]\nprovider = "api"\nkind = "claude"\n'); cfg.run.dry = True
        b = d / "briefs/u1.md"; b.write_text("TIER: NORMAL\nSCOPE: x\nREPORT.md\nDONE-WHEN: done\n")
        saved = janus.Beads, janus.herdr; janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                try: janus.cmd_launch(cfg, self._ns("u1", model="c", brief=str(b), new_worktree=str(R)))
                except janus.Err: pass
        finally: janus.Beads, janus.herdr = saved
        self.assertIn("worktree add -b u1", out.getvalue())
        self.assertIn("develop", out.getvalue(), "the new worktree did not start from [landing] branch")


class Item13ForceClose(unittest.TestCase):
    def test_force_close_approves_only_the_unit_being_closed(self):
        """Catches (finding 13): force_close_ok approved on any word that named a unit (a --reason value), not the unit being
        closed."""
        base, R, g = git_repo("h13b")
        W = base / "wt"; g("worktree", "add", "-q", str(W), "-b", "u1")
        d, cfg = project()
        u1 = {"id": "t-1", "name": "u1", "cwd": str(W)}
        u2 = {"id": "t-2", "name": "u2", "cwd": str(d)}   # the project root: shared, not its own tree
        real = janus.Beads; janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u1, u2]})
        try:
            self.assertTrue(janus.force_close_ok(cfg, ["close", "u1", "--force"]))
            self.assertFalse(janus.force_close_ok(cfg, ["close", "u2", "--force", "--reason", "handover", "from", "u1"]),
                             "a unit named elsewhere approved a close of another unit")
        finally: janus.Beads = real


class Item14UnitChanges(unittest.TestCase):
    def test_a_staged_merge_of_another_branch_does_not_use_the_landing_base(self):
        """Catches (finding 14): a staged merge of a branch other than the landing branch made unit_changes diff against the
        landing branch tip, so the landing branch's later file counted as the unit's change."""
        base, R, g = git_repo("u14")
        g("checkout", "-q", "-b", "z"); (R / "z").write_text("z\n"); g("add", "."); g("commit", "-qm", "z")
        g("checkout", "-q", "main"); (R / "c").write_text("2\n"); g("add", "."); g("commit", "-qm", "main adds c")
        g("checkout", "-q", "-b", "unit", "main~1"); (R / "u").write_text("u\n"); g("add", "."); g("commit", "-qm", "unit")
        g("merge", "--no-commit", "--no-ff", "z")
        d, cfg = project(); cfg.c["landing"] = {"branch": "main"}
        u = {"id": "t-1", "name": "u1", "host": "here", "cwd": str(R)}
        br, fs = janus.unit_changes(cfg, u, [u])
        self.assertIn("u", fs); self.assertNotIn("c", fs, "the landing branch's own file counted as the unit's change")


class Item6Adoption(unittest.TestCase):
    def setUp(self):
        import shutil
        if not shutil.which("pueued") or not shutil.which("pueue"): self.skipTest("pueue not installed")
        self.pd = Path(tempfile.mkdtemp(prefix="ad")); (self.pd / "d").mkdir(); (self.pd / "r").mkdir()
        (self.pd / "p.yml").write_text(f"shared:\n  pueue_directory: {self.pd}/d\n  runtime_directory: {self.pd}/r\n"
                                       f"  use_unix_socket: true\n  unix_socket_path: {self.pd}/s.sock\n")
        subprocess.run(["pueued", "-c", str(self.pd / "p.yml"), "-d"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        for _ in range(50):
            if (self.pd / "s.sock").exists(): break
            time.sleep(0.1)

    def tearDown(self):
        subprocess.run(["pueue", "-c", str(self.pd / "p.yml"), "shutdown"], capture_output=True, timeout=20)

    def _pq(self, *a): return subprocess.run(["pueue", "-c", str(self.pd / "p.yml"), *a], capture_output=True, text=True)
    def _labels(self): return {int(k): (v.get("label") or "") for k, v in json.loads(self._pq("status", "--json").stdout).get("tasks", {}).items()}

    def test_adoption_matches_the_path_and_verifies_the_edit(self):
        """Catches (finding 6): adopt relabelled another project's job by bare unit name, reported adoptions pueue edit never
        made, did not adopt a gate job of this project's unit, and left its editor script in the state dir."""
        d = Path(tempfile.mkdtemp(prefix="janus-adopt-"))
        (d / "janus.toml").write_text(f'[project]\nname = "t"\nid = "proj"\n[hosts.here]\nlocal = true\npueue_config = "{self.pd}/p.yml"\n')
        (d / "out").mkdir(); (d / "briefs").mkdir(); (d / "board/agents").mkdir(parents=True)
        self._pq("add", "-s", "-l", "unit:u1", "-w", str(d), "--", "true")
        self._pq("add", "-s", "-l", "unit:u1", "-w", str(self.pd), "--", "true")
        self._pq("add", "-s", "-l", "gate:land-u1-linux", "-w", str(d), "--", "true")
        ids = sorted(self._labels())
        saved = janus.all_units; janus.all_units = lambda cfg: {"u1": {}}
        try: out = "\n".join(janus.adopt_items(d, dry=False, config=d / "janus.toml"))
        finally: janus.all_units = saved
        labs = self._labels()
        self.assertEqual(labs[ids[0]], "unit:u1@proj", out)
        self.assertEqual(labs[ids[1]], "unit:u1", "another project's job was relabelled: " + out)
        self.assertEqual(labs[ids[2]], "gate:land-u1-linux@proj", "this project's gate job was not adopted: " + out)
        self.assertEqual(list((d / ".janus").glob(".pueue-label-*.py")), [], "the editor script was left behind")

    def test_the_unit_record_follows_a_renamed_tmux_session(self):
        """Catches (finding 6): the tmux session was renamed but the unit record kept no tmux field, so unit_tmux(u) still
        looked for the old name."""
        d = Path(tempfile.mkdtemp(prefix="janus-mig-")); (d / "janus.toml").write_text('[project]\nname = "t"\nid = "proj"\n')
        saved_units = []
        class B:
            def __init__(self, cfg): pass
            def units(self): return [{"id": "t-1", "name": "u1"}]
            def save(self, u): saved_units.append(dict(u))
        def fake_sh(cmd, **k):
            if cmd[:2] == ["pueue", "status"]: return 0, json.dumps({"tasks": {}}), ""
            if cmd[:2] == ["tmux", "list-sessions"]: return 0, "orch-u1\n", ""
            return 0, "", ""
        real_sh, real_beads, real_units = janus.sh, janus.Beads, janus.all_units
        janus.sh = fake_sh; janus.Beads = B; janus.all_units = lambda cfg: {"u1": {}}
        try: janus.adopt_items(d, dry=False, config=d / "janus.toml")
        finally: janus.sh, janus.Beads, janus.all_units = real_sh, real_beads, real_units
        self.assertEqual([u.get("tmux") for u in saved_units], ["janus-u1@proj"])


if __name__ == "__main__": unittest.main()
