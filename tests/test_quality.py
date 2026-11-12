"""The quality subsystem: brief lint, the report.json contract, the brief quality block, the gate test-signal, the pre-landing
review and janus soak. Each test names the failure it catches. Standard library only; no network, no API spend.

Run by ./check (it discovers tests/test_*.py)."""
import json, os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path

from janusload import load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    (d / "janus.toml").write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        [providers.api]
        enabled = true
        cap_usd = 10
        budget_per_unit = 1
        ledger = "out/ledger.tsv"
        key_file = "KEYFILE"
        [models.m]
        provider = "api"
        kind = "api"
        api_id = "test-model"
        ''').replace("KEYFILE", str(d / "key.env")) + extra)
    (d / "out").mkdir(); (d / "briefs").mkdir()
    return d, janus.Cfg(d)


class BriefLint(unittest.TestCase):
    """item 5: a brief without a tier, a scope, a report path or done-criteria is unfit to launch; launch refuses it when the
    project turns the lint on. The failure caught: a brief an agent cannot follow launching anyway."""

    def test_lint_names_every_missing_part(self):
        d, cfg = project()
        b = d / "briefs/b.md"; b.write_text("Please do the thing.\n")
        P = janus.lint_brief(cfg, b)
        self.assertEqual(len(P), 4, P)
        for w in ("tier", "scope", "report", "done"): self.assertTrue(any(w in x for x in P), P)
        b.write_text("# Brief\nTIER: NORMAL\nSCOPE: only a.py\nWrite REPORT.md in your out folder.\nPROOF: the unit test passes.\n")
        self.assertEqual(janus.lint_brief(cfg, b), [])

    def test_lint_flags_a_missing_project_path_not_the_tree_or_out(self):
        d, cfg = project(); b = d / "briefs/b.md"
        b.write_text(f"TIER: NORMAL\nSCOPE: all\nWrite REPORT.md.\nPROOF: x\nRead {d}/docs/gone.md and {d}/out/u/REPORT.md.\n")
        P = janus.lint_brief(cfg, b, cwd=d / "briefs")   # out/<unit> is created at launch, so it is not a missing path
        self.assertEqual(len(P), 1, P); self.assertIn("docs/gone.md", P[0])

    def test_launch_refuses_a_brief_that_fails_the_lint(self):
        d, cfg = project('\n[quality]\nbrief_lint = true\n'); cfg.run.dry = True
        (d / "briefs/u.md").write_text("do it\n")
        hs = {"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "100", "load": "0"}}
        real = janus.Beads.units; janus.Beads.units = lambda s: []
        try:
            a = type("A", (), {"unit": "u", "kind": "implement", "tier": "normal", "host": "here", "model": "m", "brief": None,
                               "cwd": str(d), "budget": None, "test_host": None, "allow_main": True, "of": None, "report": None,
                               "resume_of": None, "item": None, "probes": hs, "share_with": None, "dry_run": True})()
            with self.assertRaises(janus.Err) as e: janus.cmd_launch(cfg, a)
        finally: janus.Beads.units = real
        self.assertIn("fails the lint", str(e.exception)); self.assertIn("tier", str(e.exception))


class ReportJson(unittest.TestCase):
    """item 4: report.json wins over the report's prose and the prose is the fallback. The failure caught: a verdict read from
    stale prose when the unit wrote a structured report (or read from a report that is only PENDING)."""

    def test_verdict_prefers_report_json_and_falls_back(self):
        d, cfg = project(); o = d / "out/u"; o.mkdir(parents=True)
        (o / "REPORT.md").write_text("VERDICT: PENDING\nstep\nVERDICT: PENDING\n")
        (o / "report.json").write_text(json.dumps({"verdict": "DONE"}))
        self.assertEqual(janus.verdict(cfg, {"name": "u"}), "VERDICT: DONE")
        (o / "report.json").write_text("{ broken json")
        self.assertIsNone(janus.verdict(cfg, {"name": "u"}))                     # prose holds only PENDING: no verdict
        (o / "REPORT.md").write_text("body\nVERDICT: BLOCKED\n")
        self.assertEqual(janus.verdict(cfg, {"name": "u"}), "VERDICT: BLOCKED")  # clean fallback to the prose
        (o / "report.json").write_text(json.dumps({"verdict": "PENDING"}))
        self.assertIsNone(janus.verdict(cfg, {"name": "u"}))                     # a stated PENDING is not a verdict

    def test_report_summary_prefers_json_then_prose(self):
        self.assertEqual(janus.report_summary("VERDICT: PENDING\n", {"verdict": "DONE", "left": "all green"}), ("DONE", "all green"))
        self.assertEqual(janus.report_summary("Result: fixed the bug\nVERDICT: DONE\n"), ("DONE", "fixed the bug"))


class QualityBlock(unittest.TestCase):
    """item 1: every brief header carries the project's rules file by pointer, the fail-first test rule, run-only-affected-tests
    and the report.json contract. The failure caught: an agent never seeing the project's quality rules."""

    def test_header_and_prompt_carry_the_rules(self):
        d, cfg = project('\n[quality]\nrules_file = "briefs/QUALITY.md"\n')
        hd = janus.brief_header(cfg, "here", cfg.hosts["here"], "u", d, True)
        self.assertIn("briefs/QUALITY.md", hd)
        self.assertIn("names the failure it catches", hd)
        self.assertIn("run only the tests your change can affect", hd)
        self.assertIn("report.json", hd)
        self.assertIn("report.json", janus.agent_prompt(cfg, "u", "/b.md", "here"))


class TestSignal(unittest.TestCase):
    """item 3: a gate that takes long and has not failed in its last N runs is listed with its time. The failure caught: a slow
    always-passing step repeated forever with no one told to scope it."""

    def test_lists_a_long_always_passing_gate_only(self):
        d, cfg = project('\n[quality]\nsignal_runs = 3\nsignal_min_min = 10\n')
        t = "2026-01-01 00:00:00"
        (d / "out/gates.log").write_text(
            "".join(f"{t} slow EXIT_tests=0 RESULT: PASS\n" for _ in range(3))            # listed: 30 min, never failed
            + "".join(f"{t} fast EXIT_tests=0 RESULT: PASS\n" for _ in range(3))          # short: not listed
            + f"{t} flaky EXIT_tests=0 RESULT: PASS\n{t} flaky EXIT_tests=0 RESULT: PASS\n{t} flaky EXIT_tests=1 RESULT: FAIL\n"
            + "".join(f"{t} once EXIT_tests=0 RESULT: PASS\n" for _ in range(2)))          # too few runs to say
        cfg.jput("gate-times.json", {"slow": [30, 32, 28], "fast": [1, 2, 1], "flaky": [40, 41, 39], "once": [50, 50]})
        self.assertEqual([g for g, _, _ in janus.gate_signals(cfg, 3, 10)], ["slow"])
        F = janus.test_signal_findings(cfg)   # its runs/min_min come from [quality] (signal_runs, signal_min_min)
        self.assertEqual([k for k, _ in F], ["test-signal:slow"])
        self.assertIn("30", F[0][1]); self.assertIn("scope or trim", F[0][1])


class LandReview(unittest.TestCase):
    """item 2: a missing, stale or open pre-landing review blocks janus land until it passes or is overridden. The failure
    caught: slop and worthless tests landing unreviewed."""

    def setUp(self):
        self.d, self.cfg = project('\n[landing]\nbranch = "main"\n[quality]\nreview_model = "m"\n')
        self.r = self.d / "repo"; self.r.mkdir(); self.wt = self.d / "wt"
        self.g("init", "-q", "-b", "main"); (self.r / "a").write_text("1\n"); self.g("add", "a"); self.g("commit", "-qm", "base")
        self.g("worktree", "add", "-q", "-b", "wk/u", str(self.wt))
        self.u = {"id": "x-1", "name": "u", "cwd": str(self.wt), "tier": "normal"}
        self.real = (janus.Beads.units, janus.Beads.comment)
        janus.Beads.units = lambda s: [self.u]; janus.Beads.comment = lambda s, *a: None
        self.cfg.run.all_units = {"u": self.u}

    def tearDown(self): janus.Beads.units, janus.Beads.comment = self.real

    def g(self, *a, cwd=None): return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd or self.r), *a], capture_output=True, text=True)

    def head(self): return self.g("rev-parse", "HEAD", cwd=self.wt).stdout.strip()

    def review(self, **k):
        p = self.d / "out/u"; p.mkdir(parents=True, exist_ok=True); (p / "review.json").write_text(json.dumps({"head": self.head(), "findings": [], "override": None, **k}))

    def commit(self, f="b"):
        (self.wt / f).write_text("new\n"); self.g("add", f, cwd=self.wt); self.g("commit", "-qm", f"u: {f}", cwd=self.wt)

    def test_land_blocked_missing_stale_open_until_overridden(self):
        self.assertIn("no pre-landing review", janus.land_review(self.cfg, self.u))
        self.review(); self.assertIsNone(janus.land_review(self.cfg, self.u))                  # clean review at this head: lands
        self.review(findings=[{"kind": "slop", "where": "a:1", "why": "restates the code"}])
        self.assertIn("review found 1 issue", janus.land_review(self.cfg, self.u))
        self.review(findings=[{"kind": "slop", "where": "a:1", "why": "x"}], override="reviewed with the owner")
        self.assertIsNone(janus.land_review(self.cfg, self.u))
        self.review(); self.commit()                                                             # the tree moved since the review
        self.assertIn("stale", janus.land_review(self.cfg, self.u))

    def test_cmd_land_refuses_on_an_open_review(self):
        self.commit()
        a = type("A", (), {"unit": "u", "no_gate": True, "no_dispatch": True})()
        with self.assertRaises(janus.Err) as e: janus.cmd_land(self.cfg, a)
        self.assertIn("pre-landing review", str(e.exception))

    def test_review_run_writes_review_json_from_a_fake_model(self):
        d, cfg = self.d, self.cfg
        self.commit()
        fake = d / "rev.py"
        fake.write_text("import json\n"
                        "print(json.dumps({'total_cost_usd': 0.02, 'result': json.dumps({'findings': [{'kind': 'test', 'where': 't.py:9', 'why': 'no assertion'}]})}))\n")
        cfg.prov["api"]["cmd"] = [sys.executable, str(fake), "{prompt}"]   # the review run's command (the provider table)
        k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n"); k.chmod(0o600)
        rc, L = janus.review_unit(cfg, "u")
        rj = json.loads((d / "out/u/review.json").read_text())
        self.assertEqual(rj["findings"][0]["kind"], "test"); self.assertEqual(rc, 1); self.assertEqual(rj["head"], self.head())


class Soak(unittest.TestCase):
    """item 6: one soak pass exercises every read path, prints one line each, and writes nothing. The failure caught: a soak
    that mutates the live project it is checking."""

    def setUp(self):
        # fixtures for the read paths, and a unit list of our own: another test in the suite leaves Beads.units patched
        self.d, self.cfg = project()
        self.real = janus.Beads.units
        janus.Beads.units = lambda s: [{"name": "u", "host": "here", "model": "m", "cwd": str(self.d), "started": 0}]

    def tearDown(self): janus.Beads.units = self.real

    def test_one_pass_is_read_only_and_reports_each_step(self):
        d, cfg = self.d, self.cfg
        listing = lambda sub: sorted(str(p.relative_to(d)) for p in (d / sub).rglob("*") if p.is_file())
        before = {s: listing(s) for s in (".janus", "out")}
        rc, L = janus.cmd_soak(cfg, type("A", (), {"minutes": 0, "live": False})())
        self.assertEqual(rc, 0, L)
        self.assertEqual([l.split(":")[0] for l in L[:-1]], [f"soak {s}" for s in janus.STEPS])   # one line per step
        self.assertTrue(all(("PASS" in l) or ("FAIL" in l) for l in L[:-1]))
        # janus's state and the project's reports are untouched; board/.master etc. are written by any board read (unchanged live)
        self.assertEqual({s: listing(s) for s in (".janus", "out")}, before)

    def test_dry_does_not_wake_the_watcher_live_does(self):
        d, cfg = self.d, self.cfg; calls = []
        real = janus.cmd_watch; janus.cmd_watch = lambda cfg, a: calls.append(cfg.root)
        try:
            janus.cmd_soak(cfg, type("A", (), {"minutes": 0, "live": False})()); self.assertEqual(calls, [])
            janus.cmd_soak(cfg, type("A", (), {"minutes": 0, "live": True})()); self.assertEqual(calls, [cfg.root])
        finally: janus.cmd_watch = real


if __name__ == "__main__": unittest.main()
