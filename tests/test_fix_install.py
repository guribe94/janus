"""fix-install items 7-14: the issues found installing janus on a live project, each test naming the failure it catches.
Standard library only; no network, no API spend."""
import contextlib, io, json, os, re, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path
from unittest import mock

from janusload import JANUS_PY, conf, load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-fix-"))
    conf(d).write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        ''') + textwrap.dedent(extra))
    (d / ".janus" / "out").mkdir(); (d / ".janus" / "briefs").mkdir()
    return d, janus.Cfg(d)


def probe_of(**k):
    return {"ok": True, "res": [], "hist": [], "sched": [], "direct": [], "pueue": {"tasks": {}}, "cap_pause": "", "cap_admit": "yes", "tmux": [], **k}


class Item11Verdict(unittest.TestCase):
    """Item 11: WAITING/PENDING is never a final verdict; a recorded park wins over a verdict."""
    def test_waiting_and_pending_are_not_final_verdicts(self):
        """Catches: units.verdict/verdict_line excluded only PENDING, so a report ending 'VERDICT: WAITING' read as finished and
        its jobs were cancelled as orphans."""
        d, cfg = project()
        (d / ".janus/out/u1").mkdir(parents=True); (d / ".janus/out/u1/REPORT.md").write_text("VERDICT: PENDING\nbody\nWAITING here:1\nVERDICT: WAITING\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        self.assertEqual(janus.verdict_line({"verdict": "WAITING"}), (None, True))
        self.assertEqual(janus.verdict_line({"verdict": "PENDING"}), (None, True))
        self.assertEqual(janus.verdict_line({"verdict": "DONE"}), ("VERDICT: DONE", True))

    def test_a_recorded_park_wins_over_a_verdict(self):
        """Catches: a unit whose run was resumed and then parked was judged finished (its jobs became orphan:job)."""
        d, cfg = project(); (d / ".janus/out/u1").mkdir(parents=True); (d / ".janus/out/u1/REPORT.md").write_text("VERDICT: DONE\nall good\n")
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "parked": {"jobs": ["here:9"]}}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u["state"], "parked"); self.assertEqual(u["_park"], ["here:9"])
        self.assertNotIn("unit:u1:finished", [k for k, _ in F])

    def test_remote_ended_unit_report_is_synced_before_orphan_judging(self):
        """Catches (item 11 addendum): a remote unit's local report has no WAITING line until collect, so check judged it orphaned
        and its jobs were cancelled."""
        d, cfg = project("\n[hosts.far]\nssh = \"far\"\nroot = \"/home/x\"\n")
        (d / ".janus/out/u1").mkdir(parents=True); (d / ".janus/out/u1/REPORT.md").write_text("VERDICT: PENDING\n")
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "started": 0, "state": "done"}
        hs = {"far": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        real = janus.save_output
        janus.save_output = lambda cfg, u: ((d / ".janus/out/u1/REPORT.md").write_text("VERDICT: PENDING\nWAITING far:9\n"), True)[1]
        try:
            janus.sync_remote_outputs(cfg, [u], type("B", (), {"save": lambda self, u: None})())
            F, _ = janus.unit_findings(cfg, hs, [u], {})
        finally: janus.save_output = real
        self.assertEqual(u["_park"], ["far:9"]); self.assertEqual(u["state"], "parked")
        self.assertNotIn("orphan:job:far:9", [k for k, _ in janus.orphan_job_findings(cfg, hs, [u], set())])
        import inspect
        self.assertIn("sync_remote_outputs(cfg, units, bd, fetched)", inspect.getsource(janus.collect_findings))


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


class Item8ThrashExplicit(unittest.TestCase):
    def test_a_thrash_stop_is_written_to_the_job_log_and_names_the_requeue(self):
        """Catches (finding 8): a shared-host PSI stop/re-queue was recorded only in the watcher log and the check's finding,
        never in the stopped job's own log, and the finding did not say the job was re-queued on the same host (still over the
        limit); the over-since clock was not reset, so each pass stopped the next newest job."""
        import json as _json
        d, cfg = project('\n[hosts.far]\nssh = "x@far"\nroot = "/r"\nshared = true\nthrash_psi = 10\nthrash_min = 5\n'
                         'job_log = "/r/runs/job.log"\n'); cfg.run.ro = False
        jd = cfg.f("runs") / "jobs"; jd.mkdir(parents=True)
        (jd / "new-2.json").write_text(_json.dumps({"host": "far", "task": "2", "lane": "jobs", "unit": "u", "wt": str(d),
                                                    "cmd": "make", "at": 200, "pinned": True}))
        t = {"status": {"Running": {"start": "2026-01-01T11:00:00"}}, "command": "bash /r/runs/new-2.sh", "label": "unit:u@t", "group": "jobs"}
        hs = {"far": {"ok": True, "psi": "12.5", "pueue": {"tasks": {"2": t}}}, "here": {"ok": True}}
        cfg.jput("thrash.json", {"far": int(time.time()) - 600})
        calls = []; hl = []
        saved = (janus.cancel_job, janus.cmd_run, janus.host_cap, janus.board, janus.hrun)
        janus.cancel_job = lambda cfg, ref, force=False: calls.append(("cancel", ref)) or [f"cancelled {ref}"]
        janus.cmd_run = lambda cfg, a: calls.append(("run", a.host)) or print("far:9")
        janus.host_cap = lambda *a: {"free": 0, "pause": "memory PSI 12.5 over 10"}; janus.board = lambda *a, **k: (0, "", "")
        janus.hrun = lambda h, s, **k: (hl.append(s), (0, "", ""))[1]
        try:
            out = janus.tend_thrash(cfg, hs, [], lambda m: None)
            self.assertTrue(any("SAME host" in l for l in out), f"the finding does not name the re-queue target: {out}")
            log = (cfg.f("runs") / "new-2.log").read_text()
            self.assertIn("janus thrash", log); self.assertIn("far:2", log); self.assertIn("far:9", log)
            self.assertTrue(any("job.log" in s and "janus thrash" in s for s in hl), hl)
            n = len(calls); janus.tend_thrash(cfg, hs, [], lambda m: None)   # the clock was reset: no immediate second stop
            self.assertEqual(len(calls), n, "the over-since clock was not reset, so the next pass stopped another job")
        finally:
            janus.cancel_job, janus.cmd_run, janus.host_cap, janus.board, janus.hrun = saved


class Item9ResumeCard(unittest.TestCase):
    def test_resume_posts_the_card_as_waiting_with_the_new_task(self):
        """Catches: a unit resumed with janus resume --message whose run is queued in pueue (agents slot full) showed board GONE
        (relaunch it), because resume did not post the card with the new run."""
        d, cfg = project('\n[providers.api]\nenabled = true\n[models.m]\nprovider = "api"\nkind = "api"\n')
        b = d / ".janus/briefs/u1.md"; b.write_text("# Brief: u1\n")
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


class Item7BlockedLane(unittest.TestCase):
    def test_a_normally_queued_job_is_not_a_blocked_lane(self):
        """Catches (finding 7): `blocked:<host>:<lane>` fired whenever a job waited in admission, even when admission said yes
        (a job merely queued behind an older one), with the advice to cancel and re-queue it."""
        d, cfg = project()
        hs = {"here": {"ok": True, "pueue": {"groups": {"jobs": {"parallel_tasks": 1, "status": "Running"}},
                                            "tasks": {"1": {"status": {"Running": {}}, "label": "unit:x@t"}}},
                       "wait_jobs": "1", "cap_admit": "yes", "cap_pause": "", "cap_rc": "8", "cap_rm": "2048", "cap_ram": "0",
                       "cap_psi": "0", "res": [], "hist": [], "sched": [], "direct": [], "tmux": []}}
        F = dict(janus.lane_findings(cfg, hs))
        self.assertNotIn("blocked:here:jobs", F, f"a normally queued job read as a blocked lane: {F}")
        hs["here"]["cap_pause"] = "memory PSI avg10 12 over 10"   # a paused host IS a blocked lane
        self.assertIn("blocked:here:jobs", dict(janus.lane_findings(cfg, hs)))


class Item7CallerWait(unittest.TestCase):
    def test_only_a_task_itself_held_in_admission_is_reported(self):
        """Catches (finding 2, fix-install6): the caller reported a normally running job as 'held by admission' whenever the
        host's admission said no (`cap_admit` is the answer for ONE MORE job, not for this task; a heavy job on a full host made
        its own caller get the notice every 300 s), and probed the host for every waited job. Report only a task whose OWN
        admission-wait file exists; never probe one that started and runs normally."""
        d, cfg = project(); h = cfg.hosts["here"]
        def run_case(state, wait, adm):
            states = [state, {"status": {"Done": {"result": "Success"}}}]
            def fake_run(cmd, **k): raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))
            calls = []
            saved = (subprocess.run, janus.task_status, janus.probe, janus.hrun)
            subprocess.run = fake_run
            janus.task_status = lambda h_, tid: states.pop(0) if states else {"status": {"Done": {"result": "Success"}}}
            janus.probe = lambda n, h_, **k: (calls.append("probe"), {"cap_admit": adm, "cap_pause": ""})[1]
            janus.hrun = lambda h_, script, **k: (0, "y\n" if wait else "", "")
            out = io.StringIO()
            try:
                with contextlib.redirect_stderr(out): rc = janus.finish_job(cfg, "here", h, 7, "j-1", quiet=True)
            finally: subprocess.run, janus.task_status, janus.probe, janus.hrun = saved
            return rc, out.getvalue(), calls
        rc, msg, calls = run_case({"status": {"Running": {}}}, True, "disk")     # in admission: reported, probed for the reason
        self.assertEqual(rc, 0); self.assertIn("held by admission", msg); self.assertEqual(calls, ["probe"])
        rc, msg, calls = run_case({"status": {"Running": {}}}, False, "cores")   # running normally: silent, no probe
        self.assertEqual(rc, 0); self.assertEqual(msg, "", "a normally running task was reported")
        self.assertEqual(calls, [], "a task that started and runs normally was probed")
        rc, msg, calls = run_case({"status": {"Queued": {}}}, False, "yes")      # not started yet: silent
        self.assertEqual(rc, 0); self.assertEqual(msg, "", "a task not yet started was reported")

    def test_a_held_task_whose_admission_answer_is_yes_does_not_read_yes(self):
        """Catches (finding C): when the task waited in admission and admission's answer for ONE MORE job was yes (a yield to
        older jobs, or a class heavier than job), the notice read 'held by admission after 300s (yes)'."""
        d, cfg = project(); h = cfg.hosts["here"]
        states = [{"status": {"Running": {}}}, {"status": {"Done": {"result": "Success"}}}]
        saved = (subprocess.run, janus.task_status, janus.probe, janus.hrun)
        def fake_run(cmd, **k): raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))
        subprocess.run = fake_run
        janus.task_status = lambda h_, tid: states.pop(0) if states else {"status": {"Done": {"result": "Success"}}}
        janus.probe = lambda n, h_, **k: {"cap_admit": "yes", "cap_pause": ""}
        janus.hrun = lambda h_, script, **k: (0, "y\n", "")
        out = io.StringIO()
        try:
            with contextlib.redirect_stderr(out): janus.finish_job(cfg, "here", h, 7, "j-1", quiet=True)
        finally: subprocess.run, janus.task_status, janus.probe, janus.hrun = saved
        msg = out.getvalue()
        self.assertIn("held by admission", msg)
        self.assertNotIn("(yes)", msg, msg)


class Item10GiveUpResidues(unittest.TestCase):
    """Finding 10: the give-up clock residues -- a jobs job yielding to a gates job said "no reason reported", and nothing read
    exit 121 when a unit was resumed."""

    def test_a_resumed_unit_is_told_what_exit_121_means(self):
        """Catches (finding 10): nothing read exit 121, so a resumed unit got 'exit 121' with no explanation beyond the log."""
        d, cfg = project()
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "model": "m", "started": 0, "budget": 2.0}
        saved = janus.job_result; janus.job_result = lambda cfg, hs, j, host: (121, "/runs/j.log")
        try: msg = janus.resume_message(cfg, {"here": {"ok": True}}, u, ["here:9"])
        finally: janus.job_result = saved
        self.assertIn("never admitted", msg, msg)

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


class Item16NewWorktree(unittest.TestCase):
    def test_new_worktree_makes_a_fresh_branch(self):
        """Catches (item 16b): janus launch had no fresh-worktree option (without --cwd it defaulted to the project state root)."""
        base = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-u16b-")); R = base / "repo"; R.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main"); (R / "f").write_text("x\n"); g("add", "."); g("commit", "-qm", "a")
        d, cfg = project(); wt = d / ".janus/worktrees/u1"
        janus.new_worktree(R, wt, "u1", "main")
        self.assertTrue((wt / "f").is_file())
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, "u1")


class Item16Budget(unittest.TestCase):
    def test_budget_ignores_tiny_and_verdictless_runs(self):
        """Catches (item 16c): the default budget for implement/normal was $0.50 from a median of $0.09 over 23 runs that ended
        without a verdict, so a real unit started at a cap (the waste cause)."""
        d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 15\nledger = ".janus/out/ledger.tsv"\nkey_file = "k"\n[models.m]\nprovider = "api"\nkind = "api"\n')
        mc = cfg.models["m"]; runs = {}; au = {}
        for i in range(23):
            n = f"tiny{i}"; (d / ".janus" / "out" / n).mkdir(parents=True); (d / ".janus" / "out" / n / "REPORT.md").write_text("VERDICT: PENDING\n")
            runs[f"{n}.1"] = {"done": True, "cost": 0.09, "unit": n}; au[n] = {"name": n, "kind": "implement", "tier": "normal", "run": f"{n}.1", "status": "closed"}
        for i in range(3):
            n = f"real{i}"; (d / ".janus" / "out" / n).mkdir(parents=True); (d / ".janus" / "out" / n / "REPORT.md").write_text("VERDICT: DONE\ndone\n")
            runs[f"{n}.1"] = {"done": True, "cost": 2.0, "unit": n}; au[n] = {"name": n, "kind": "implement", "tier": "normal", "run": f"{n}.1", "status": "closed"}
        cfg.run.all_units = au
        real = janus.ledger_read; janus.ledger_read = lambda cfg, pv: (runs, [], [])
        try: b, why = janus.budget_for(cfg, mc, "implement", "normal")
        finally: janus.ledger_read = real
        self.assertGreaterEqual(b, 2.0, f"the tiny/verdictless runs skewed the budget: {b} ({why})")


class Item31BudgetOvershoot(unittest.TestCase):
    def test_a_running_run_counts_at_its_budget_not_its_reservation(self):
        """Catches (item 31): a unit launched with --budget 1.5 read 'spent $1.80 of $1.5' (its own report step 4) because
        unit_spent counted the ledger's `running` row -- the RESERVATION (the budget plus [budget] overshoot_pct, 20 %) -- as
        money the unit had spent, while the run's own api.json reported $0.10."""
        d, cfg = project('\n[providers.api]\nledger = ".janus/out/ledger.tsv"\n[budget]\novershoot_pct = 20\n')
        cfg.models["m"] = {"provider": "api", "kind": "api", "api_id": "m"}
        u = {"name": "u1", "model": "m", "run": "u1.1", "runs": ["u1.1"], "budget": 1.5}
        held = janus.reserved(cfg, 1.5)
        self.assertEqual(held, 1.8, "the ledger holds the reservation while a run is open")
        runs = {"u1.1": {"running": [1], "done": [], "budget": held, "cost": None, "unit": "u1"}}
        real = janus.ledger_read; janus.ledger_read = lambda cfg, pv: (runs, [], [])
        try: spent = janus.unit_spent(cfg, u)
        finally: janus.ledger_read = real
        self.assertLessEqual(spent, 1.5, f"the reservation (${held}) was counted as spend: the unit reads over its own ${1.5} budget")


class FakeBeads:
    """A beads stand-in for a launch fixture: no database, no unit ever recorded."""
    def __init__(self, cfg): pass
    def units(self): return []
    def create(self, title, *a): return title
    def run(self, *a): return []
    def save(self, u): pass
    def comment(self, *a): pass


def launch_cfg(extra=""):
    d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 1\nledger = ".janus/out/ledger.tsv"\nkey_file = "k"\n'
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
        d, cfg = project(); (d / ".janus/out/u1").mkdir(parents=True)
        (d / ".janus/out/u1/REPORT.md").write_text("VERDICT: PENDING\nVERDICT: DONE - items 1-6\n(more work)\nWAITING here:9\nVERDICT: WAITING\n")
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        (d / ".janus/out/u1/report.json").write_text(json.dumps({"verdict": "DONE"}))   # a stale json beside the prose
        self.assertIsNone(janus.verdict(cfg, {"name": "u1", "host": "here", "started": 0}))
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0}
        hs = {"here": probe_of(pueue={"tasks": {"5": {"status": {"Done": {"result": "Success"}}}}})}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        self.assertEqual(u.get("_park"), ["here:9"]); self.assertNotIn("unit:u1:finished", [k for k, _ in F])


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
        """Catches (finding 2): `resume --message` with a Running agent task started a second agent on the same session in the
        same tree; and the old refusal offered --force -- the one path that still started the second agent beside it."""
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(self.d / "wt")}
        with self.assertRaises(janus.Err) as e: self._run(u, state="working", job="running", message="go")
        self.assertIn("still working", str(e.exception)); self.assertNotIn("--force", str(e.exception))
        self.assertEqual(self.calls, [], "a second agent was started beside the running one")
        with self.assertRaises(janus.Err): self._run(u, state="working", job="running", message="go", force=True)
        self.assertEqual(self.calls, [], "--force started a second agent beside the running one")

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

    def test_a_review_unit_sharing_the_tree_is_not_another_units_tree(self):
        """Catches (finding 6, fix-install6): the H9 exception was for the RESUMED unit's kind only, so a unit whose tree held
        its own review unit (which shares the tree it reviews by design) could not resume: `resume work1 --message answer` was
        refused, with --force too."""
        W = self.d / "wt"; W.mkdir()
        work = {"id": "t-1", "name": "work1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(W)}
        rev = {"id": "t-2", "name": "rev1", "host": "here", "via": "pueue", "task": 6, "model": "m", "started": 0, "cwd": str(W), "kind": "review"}
        self._run(work, units=[work, rev], state="gone", job="done", message="answer")
        self.assertEqual(len(self.calls), 1, "a unit could not resume in a tree its own reviewer shares")

    def test_resume_refuses_an_agent_task_that_is_not_known_ended(self):
        """Catches (finding 6, fix-install6): the guard refused only working/queued, so a Paused, Stashed or Locked agent task (a
        live agent janus never pauses itself) and an unknown state (a failed probe) were resumed, starting a second agent on the
        same session."""
        for st in ("Paused", "Stashed", "Locked", "unknown"):
            u = {"id": "t-1", "name": "u1", "host": "here", "via": "pueue", "task": 5, "model": "m", "started": 0, "cwd": str(self.d / "wt")}
            with self.assertRaises(janus.Err) as e: self._run(u, state=st, job="done", message="go")
            self.assertIn("u1", str(e.exception))
            self.assertEqual(self.calls, [], f"a {st} agent task was resumed")

    def test_hand_resume_works_for_tmux_a_blocked_herdr_unit_and_a_review_unit(self):
        """Catches (finding 2): a tmux unit read as 'working' for as long as its session existed, a herdr unit blocked on a
        question could not be answered, and a review unit that shares the tree it reviews was refused -- so the routine answer
        became --force, the one path that double-starts an agent."""
        W = self.d / "wt"; W.mkdir()
        tm = {"id": "t-1", "name": "u1", "host": "here", "via": "tmux", "model": "m", "started": 0, "cwd": str(W)}
        self._run(tm, state="working", job="done", message="continue")
        self.assertEqual(len(self.calls), 1, "a tmux unit's hand resume was refused"); self.calls.clear()
        hd = {"id": "t-2", "name": "u2", "host": "here", "via": "herdr", "pane": "p", "model": "m", "started": 0, "cwd": str(W)}
        self._run(hd, state="blocked", job="done", message="answer")
        self.assertEqual(len(self.calls), 1, "a blocked herdr unit's answer was refused"); self.calls.clear()
        rv = {"id": "t-3", "name": "u3", "host": "here", "via": "pueue", "task": 7, "model": "m", "started": 0, "cwd": str(W), "kind": "review"}
        other = {"id": "t-4", "name": "u4", "host": "here", "cwd": str(W)}
        self._run(rv, units=[rv, other], state="gone", job="done", message="go")
        self.assertEqual(len(self.calls), 1, "a review unit could not resume in the tree it reviews")


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
        saved = {k: getattr(janus, k) for k in ("Beads", "uncollected", "X", "save_task_log", "board", "herdr")}
        janus.Beads = self._beads(u)
        janus.uncollected = lambda cfg, u: "its copy on far has changes or output not collected yet"
        calls = []; janus.X = lambda cfg, h, cmd, **k: (calls.append(cmd), (0, "", ""))[1]
        janus.save_task_log = lambda *a, **k: None; janus.board = lambda *a, **k: None
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


class Item3AdmitClock(unittest.TestCase):
    def test_the_give_up_clock_starts_when_the_job_is_blocked_not_at_enqueue(self):
        """Catches (finding 3): the give-up clock read JANUS_TQ (the enqueue time), so a job that had queued long behind other
        work was dropped on its first blocked poll (rc 124, the same as a command's own timeout) without ever running. The clock
        now counts only the time spent blocked, and a never-admitted job exits 121, distinct from a timed-out command (124)."""
        import argparse
        d, cfg = project('\n[capacity]\nmin_disk_gb = 99999\n')
        h = cfg.hosts["here"]; janus.write_cap(cfg, "here", h)
        r = argparse.Namespace(host="here", lane="jobs", unit=None, wt=str(d), subdir=None, usb=False, priority=0, no_wait=True,
                               allow_unmerged=False, command=["--", "true"], broad=None)
        old = os.environ.get("JANUS_TQ"); os.environ["JANUS_TQ"] = str(int(time.time()) - 7300)   # the job queued 7300 s
        try: js = janus.job_script(cfg, "here", h, r, "u", "true", "j-1")
        finally:
            if old is None: os.environ.pop("JANUS_TQ", None)
            else: os.environ["JANUS_TQ"] = old
        t0 = time.time()
        p = subprocess.run(["bash"], input=js, text=True, capture_output=True, timeout=60,
                           env={**os.environ, "JANUS_POLL_S": "1", "JANUS_ADMIT_MAX_S": "3", "JANUS_ADMIT_REPORT_S": "1"})
        self.assertNotEqual(p.returncode, 124, "a queued job was dropped as if its command had timed out: " + p.stdout + p.stderr)
        self.assertEqual(p.returncode, 121, p.stdout + p.stderr)
        self.assertGreaterEqual(time.time() - t0, 2.0, "it was dropped before its blocked clock ran out: " + p.stdout)
        # finding 7: assert the BEHAVIOUR (clock ran out, exit 121), not a wall-clock string. The script's clock is whole
        # seconds and one poll can straddle a second, so the count is 3, 4, ... -- never the queued 7300 s.
        m = re.search(r"not admitted after (\d+)s blocked \(limit 3s", p.stdout)
        self.assertIsNotNone(m, "the give-up message did not say it was not admitted after Ns blocked: " + p.stdout)
        self.assertGreaterEqual(int(m.group(1)), 3, p.stdout)
        self.assertLess(int(m.group(1)), 60, "the clock counted the 7300 s queue wait, not the time blocked: " + p.stdout)
        self.assertNotIn("after 73", p.stdout, "the clock started at enqueue, not at the first blocked poll: " + p.stdout)


class Item5HandoverOrder(unittest.TestCase):
    def test_a_handover_is_refused_before_the_successor_starts(self):
        """Catches (finding 5): launch accepted the predecessor's tree, started the successor, and only then refused to close the
        predecessor (its Err became a 'note:'), so two agents ran in one tree or the successor worked from a stale base."""
        base, R, g = git_repo("i5a")
        W = base / "wt"; g("worktree", "add", "-q", str(W), "-b", "u1")
        d, cfg = launch_cfg(); cfg.run.dry = True
        b = d / ".janus/briefs/u2.md"; b.write_text("TIER: NORMAL\nSCOPE: x\nREPORT.md\nDONE-WHEN: done\n")
        u1 = {"id": "t-1", "name": "u1", "host": "here", "cwd": str(W), "state": "working"}
        calls = []
        saved = {k: getattr(janus, k) for k in ("Beads", "herdr", "choose", "model_ok", "read_secret", "budget_for", "reserve_budget",
                                                "X", "task_status", "confirm_started", "board")}
        janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u1], "create": lambda s, *a: "t-9",
                                     "run": lambda s, *a: [], "save": lambda s, x: None, "comment": lambda s, *a: None})
        janus.choose = lambda *a, **k: ("here", "here", "ok")
        janus.model_ok = lambda cfg, m: (True, ""); janus.read_secret = lambda pv: None
        janus.budget_for = lambda *a, **k: (1.0, "stub"); janus.herdr = lambda *a, **k: None; janus.board = lambda *a, **k: None
        janus.reserve_budget = lambda *a, **k: (calls.append("reserve"), "u2.r1")[1]
        janus.X = lambda cfg, h, cmd, **k: (calls.append(str(cmd)[:30]), (0, "", ""))[1]
        janus.task_status = lambda h, t: {"status": {"Running": {}}}; janus.confirm_started = lambda *a, **k: ("running", "")
        import argparse
        ns = argparse.Namespace(unit="u2", kind="implement", tier="normal", host="here", model="m", brief=str(b), cwd=str(W),
                                budget=None, test_host=None, allow_main=True, of=None, report=None, resume_of="u1", item=None,
                                new_worktree=None, from_ref=None, probes={"here": probe_of()}, share_with=None)
        try:
            with self.assertRaises(janus.Err) as e: janus.cmd_launch(cfg, ns)
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn("u1", str(e.exception)); self.assertIn("still working", str(e.exception))
        self.assertEqual(calls, [], "the successor was started before the handover was refused")

    def test_close_refuses_a_handover_whose_state_cannot_be_told(self):
        """Catches (finding 5): a handover closed a predecessor whose state was 'unknown' (its pueue status or probe failed), so
        it might still be running in the tree the successor works in."""
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        u = {"id": "t-1", "name": "u1", "host": "far", "via": "pueue", "task": 5, "model": "m", "cwd": "/w/u1"}
        saved = {k: getattr(janus, k) for k in ("Beads", "uncollected", "probe_all", "herdr_agents", "unit_state", "X")}
        janus.Beads = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [u], "comment": lambda s, *a: None,
                                     "run": lambda s, *a: [], "save": lambda s, x: None, "close": lambda s, *a, **k: None})
        janus.uncollected = lambda cfg, u: None
        janus.probe_all = lambda cfg: {}; janus.herdr_agents = lambda cfg: {}; janus.unit_state = lambda u, hs, ag: "unknown"
        calls = []; janus.X = lambda cfg, h, cmd, **k: (calls.append(cmd), (0, "", ""))[1]
        try:
            with self.assertRaises(janus.Err) as e:
                janus.cmd_close(cfg, type("A", (), {"unit": "u1", "force": True, "handover": True, "reason": "handover"})())
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        self.assertIn("unknown", str(e.exception)); self.assertFalse(any("rm -rf" in c for c in calls))


class Item5ReportSync(unittest.TestCase):
    def _remote(self, state="done", via="pueue", **kw):
        d, cfg = project('\n[hosts.far]\nssh = "far"\nroot = "/home/x"\n')
        (d / ".janus/out/u1").mkdir(parents=True); (d / ".janus/out/u1/REPORT.md").write_text("VERDICT: PENDING\n")
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
        d, cfg = project('\n[providers.api]\nenabled = true\nbudget_per_unit = 15\nledger = ".janus/out/ledger.tsv"\nkey_file = "k"\n'
                         '[models.m]\nprovider = "api"\nkind = "api"\n')
        mc = cfg.models["m"]; runs = {}; au = {}
        def add(n, cost, kind, report=None, capped=False):
            (d / ".janus" / "out" / n).mkdir(parents=True)
            if report is not None: (d / ".janus" / "out" / n / "REPORT.md").write_text(report)
            if capped: (d / ".janus" / "out" / n / "api.json").write_text(json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": cost}))
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


class Item11CommitStateAdd(unittest.TestCase):
    def test_a_file_that_fails_to_add_is_an_error(self):
        """Catches (finding 11): commit-state reported success ('committing what was staged') while a file that failed to add
        every time was left out."""
        d = Path(tempfile.mkdtemp(prefix="janus-fix-")); conf(d).write_text('[project]\nname = "t"\n'); (d / ".janus" / "out").mkdir()
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
        b = d / ".janus/briefs/u1.md"; b.write_text("just some words with no markers\n")
        saved = janus.Beads, janus.herdr; janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None
        try:
            with self.assertRaises(janus.Err): janus.cmd_launch(cfg, self._ns("u1", brief=str(b), new_worktree=str(R)))
        finally: janus.Beads, janus.herdr = saved
        self.assertFalse((d / ".janus/worktrees/u1").exists(), "a refused launch left the worktree behind")
        self.assertNotEqual(g("rev-parse", "-q", "--verify", "u1").returncode, 0, "a refused launch left the branch behind")

    def test_cwd_with_new_worktree_is_an_error(self):
        """Catches (finding 12): --cwd given with --new-worktree was silently ignored."""
        base, R, g = git_repo("u12b")
        d, cfg = launch_cfg('\n[quality]\nbrief_lint = true\n')
        b = d / ".janus/briefs/u1.md"; b.write_text("just some words with no markers\n")
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
        b = d / ".janus/briefs/u1.md"; b.write_text("TIER: NORMAL\nSCOPE: x\nREPORT.md\nDONE-WHEN: done\n")
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


class Item30BriefPath(unittest.TestCase):
    def test_a_key_list_is_not_read_as_a_path(self):
        """Catches (item 30): the brief's path reader took the `/` between two KEY= items (`FMT=/CLIPPY=`) for the absolute
        path `/CLIPPY`, so a remote launch was refused for a path the brief never named."""
        self.assertEqual(janus.brief_paths("prints FMT=/CLIPPY= (a KEY= list in prose)"), [])
        self.assertEqual(janus.brief_paths("A=/x=/y= and B=/z"), ["/z"])   # `/x` `/y` are KEY= separators; `/z` is a value

    def test_a_real_path_is_still_read(self):
        """Catches the other direction: the fix must not drop the paths a brief does name (quoted, after `=`, trailing dot)."""
        self.assertEqual(janus.brief_paths("B=`/root/x` and P=/tmp/y and see /a/b.md."), ["/root/x", "/tmp/y", "/a/b.md"])


class Item33WakeRoot(unittest.TestCase):

    def test_check_from_a_non_root_cwd_names_the_registered_root(self):
        """Catches (item 33): `janus check` from a directory outside the root exited 2 with a message that never said where
        the root was (the live session ran in the project repo while the root was a subdirectory of it)."""
        d, cfg = project()
        (cfg.state / "session").write_text("sess-1\n")
        home = Path(tempfile.mkdtemp(prefix="janus-home-")); (home / ".config/janus").mkdir(parents=True)
        (home / ".config/janus/projects").write_text(str(cfg.root.resolve()) + "\n")
        away = Path(tempfile.mkdtemp(prefix="janus-away-"))
        r = subprocess.run([sys.executable, str(JANUS_PY), "check"], cwd=away, capture_output=True, text=True,
                           env={**os.environ, "JANUS_ROOT": "", "HOME": str(home), "CLAUDE_CODE_SESSION_ID": "sess-1"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(str(cfg.root.resolve()), r.stderr, "check did not say where the root is: " + r.stderr)


class Item29RemotePatch(unittest.TestCase):
    def test_the_patch_git_apply_takes_includes_new_and_deleted_files(self):
        """Catches (item 29): janus collect built a remote unit's patch with `diff -ruN | sed`, which wrote a new file as
        `--- a/<f>` with an epoch date and no `new file mode`, so `git apply` refused it ("does not exist in index"); a host
        without `patch` (asgard) could not take it either, and a new EMPTY file never appeared in the diff at all (the live
        unit had to recreate its two new crate files by hand)."""
        base = Path(tempfile.mkdtemp(prefix="janus-p29-")); b, w = base / "base/u", base / "worktrees/u"
        for d, files in ((b, {"keep": "a\n", "del": "b\n", "target/junk": "x\n"}), (w, {"keep": "a2\n", "new": "n\n", "empty": "", "sub/deep": "d\n"})):
            for rel, t in files.items(): (d / rel).parent.mkdir(parents=True, exist_ok=True); (d / rel).write_text(t)
        PATCH = getattr(janus, "PATCH_PY", None)
        if PATCH:   # the fix: the host's python3 builds the patch from the two trees
            p = subprocess.run([sys.executable, "-c", PATCH, "u", "base/u", "worktrees/u"], cwd=base, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr); P = p.stdout
        else:       # the base tip's janus collect: `diff -ruN <excludes> base/u worktrees/u | sed ...`
            q = "diff -ruN -x target -x .venv -x node_modules -x __pycache__ -x .git base/u worktrees/u | sed -e 's#^--- base/u/#--- a/#' -e 's#^+++ worktrees/u/#+++ b/#'"
            P = subprocess.run(q, shell=True, cwd=base, capture_output=True, text=True).stdout
        chk = base / "chk"; chk.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(chk), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)
        g("init", "-q", "-b", "main")
        for rel in ("keep", "del", "target/junk"): f = chk / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text((b / rel).read_text())
        g("add", "-A"); g("commit", "-qm", "base")
        r = subprocess.run(["git", "-C", str(chk), "apply", "-"], input=P, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + "\n--- patch ---\n" + P)
        self.assertIn("new file mode 100644", P); self.assertIn("deleted file mode 100644", P)
        self.assertIn("--- /dev/null", P); self.assertIn("+++ /dev/null", P)
        self.assertNotIn("1970-01-01", P); self.assertNotIn("1969-12-31", P)
        self.assertNotIn("a/target/junk", P, "an excluded directory (target) reached the patch")
        self.assertEqual((chk / "keep").read_text(), "a2\n"); self.assertFalse((chk / "del").exists())
        self.assertEqual((chk / "new").read_text(), "n\n"); self.assertTrue((chk / "sub/deep").is_file())
        self.assertTrue((chk / "empty").is_file() and (chk / "empty").stat().st_size == 0, "a new empty file did not arrive")


class Item27ParkedFinished(unittest.TestCase):
    def _hs(self, cfg, status):
        return {"here": probe_of(pueue={"tasks": {"9": {"status": status, "label": janus.tag(cfg, "unit:u1")}}})}
    def test_a_parked_unit_whose_jobs_finished_is_reported(self):
        """Catches (item 27): a unit parked on jobs that had all finished was invisible to `janus check` while the resume_error
        was not written yet (the watcher had not reached its next pass), so an orchestrator reading check could not see the unit
        waiting, nor wake it. Seen live: test-exec-race2 sat parked on asgard:182-184 and check was silent."""
        d, cfg = project()
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "herdr", "state": "idle", "parked": {"jobs": ["here:9"], "since": 1, "by": "janus park"}}
        F = janus.parked_findings(cfg, self._hs(cfg, {"Done": {"result": "Success"}}), u, ["here:9"])
        self.assertEqual([k for k, _ in F], ["parked:u1:finished"], F)
        self.assertIn("janus resume u1", F[0][1])
    def test_a_parked_unit_whose_job_cannot_finish_or_cannot_resume_keeps_its_own_finding(self):
        """Pins the neighbours: a job that can never finish, and a finished job whose resume failed, are each still reported
        (and a finished+failed resume is reported once, as :resume)."""
        d, cfg = project()
        u = {"id": "t-1", "name": "u1", "host": "here", "via": "herdr", "state": "idle"}
        F = janus.parked_findings(cfg, self._hs(cfg, "Paused"), u, ["here:9"])
        self.assertEqual([k for k, _ in F], ["parked:u1"])
        u2 = {**u, "resume_error": {"why": "under resume_min_usd", "at": 1}}
        F2 = janus.parked_findings(cfg, self._hs(cfg, {"Done": {"result": "Success"}}), u2, ["here:9"])
        self.assertEqual([k for k, _ in F2], ["parked:u1:resume"])
        F3 = janus.parked_findings(cfg, self._hs(cfg, "Running"), u, ["here:9"])   # a job still running: the normal park
        self.assertEqual(F3, [])


if __name__ == "__main__": unittest.main()


class Item35BoardAnswer(unittest.TestCase):
    """Item 35: the answer command check prints for a board question works as printed, from the orchestrator's shell."""
    def janus_cmd(self, d, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("BOARD_DIR", "JANUS_ROLE", "JANUS_ROOT")}
        return subprocess.run([sys.executable, str(JANUS_PY), *args], cwd=d, env=env, capture_output=True, text=True, timeout=60)

    def test_the_printed_answer_command_answers_the_question(self):
        """Catches: check printed `board.sh answer q1 <text>`, which died 'answer runs on the coordinating machine only' without
        BOARD_DIR, and `janus answer q1 <text>` died 'invalid int value'; the question stayed open."""
        d, cfg = project(); cfg.run.ro = False
        janus.board(cfg, "post", "a1", "status", "start"); janus.board(cfg, "post", "a1", "question", "which port?")
        text = dict(janus.board_findings(cfg, {"a1"}))["question:q1"]
        m = re.search(r"then (janus answer q1) <text>", text); self.assertTrue(m, f"the finding names no janus answer: {text}")
        r = self.janus_cmd(d, "answer", "q1", "use 8080")      # the printed command, <text> filled in, no BOARD_DIR in the environment
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([k for k, _ in janus.board_findings(cfg, {"a1"})], [])
        self.assertIn("use 8080", (d / ".janus/board/to/a1.md").read_text())
        self.assertNotEqual(self.janus_cmd(d, "answer", "q9", "x").returncode, 0)   # an unknown board question is refused, not ignored

    def test_an_old_project_board_script_is_flagged(self):
        """Catches: a pre-janus bin/board.sh left in the project (no answer subcommand: it prints its help) misled the orchestrator
        and check said nothing about it."""
        d, cfg = project(); (d / "bin").mkdir(); (d / "bin/board.sh").write_text("#!/bin/sh\ncase $1 in post) ;; *) echo help;; esac\n")
        self.assertEqual([k for k, _ in janus.board_findings(cfg, set())], [f"board:stale:{d / 'bin/board.sh'}"])
        (d / "bin/board.sh").unlink(); self.assertEqual(janus.board_findings(cfg, set()), [])


class LauncherLinks(unittest.TestCase):
    """Real asgard install: the release check passed and `current` switched, then finish_links exited because install-host had
    written a regular ~/.local/bin/janus; and install-host must never overwrite the link install made."""
    def test_install_migrates_only_its_own_launcher_and_install_host_keeps_the_link(self):
        """Catches: (1) install exits after switching current when ~/.local/bin/janus is janus's own generated launcher;
        (2) a user's unknown file there is replaced, or refused only AFTER the switch; (3) install-host rewrites an installed link."""
        src = Path(JANUS_PY).parent / "install"
        if not src.is_file(): self.skipTest("install is not shipped in the release copy; ./check runs this against the tree")
        home = Path(tempfile.mkdtemp(prefix="janus-home-")); (home / ".local/bin").mkdir(parents=True); (home / ".local/share/janus/releases/r1").mkdir(parents=True)
        (home / ".local/share/janus/releases/r1/janus.py").write_text("")
        ns = {"__name__": "install_under_test", "__file__": str(src)}
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(sys, "argv", ["install", "--dry-run"]), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(src.read_text().rsplit("\nmain()", 1)[0], str(src), "exec"), ns)
        J = ns["JANUS"]; ns["swap_link"](ns["CUR"], home / ".local/share/janus/releases/r1")
        J.write_text('#!/bin/sh\nexec python3 /srv/x/janus/janus.py "$@"\n')   # what install-host wrote
        self.assertTrue(ns["generated_launcher"](J))
        ns["DRY"] = False
        with contextlib.redirect_stdout(io.StringIO()): ns["finish_links"]()
        self.assertTrue(J.is_symlink() and os.readlink(J).endswith("current/janus.py"))      # migrated, no exit
        J.unlink(); J.write_text("#!/bin/sh\necho mine\n")                                   # a user's own file
        self.assertFalse(ns["generated_launcher"](J))
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()): ns["check_launcher"]()   # refused before any switch
        self.assertEqual(J.read_text(), "#!/bin/sh\necho mine\n")
        # install-host's line: keeps a link and an unknown file, writes where absent
        run = lambda: subprocess.run(["sh", "-c", janus.launcher_cmd(str(home / ".local/bin"), "/srv/x")], capture_output=True, text=True, env={**os.environ, "HOME": str(home)})
        run(); self.assertEqual(J.read_text(), "#!/bin/sh\necho mine\n")
        J.unlink(); J.symlink_to(home / ".local/share/janus/releases/r1/janus.py"); run(); self.assertTrue(J.is_symlink())
        J.unlink(); run(); self.assertTrue(ns["generated_launcher"](J)); self.assertTrue(os.access(J, os.X_OK))
