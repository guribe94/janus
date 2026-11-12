"""Checks for orch.py, run before every release (./check, and install runs them against the copy it installs).
ORCH_PY names the orch.py under test (default: the one next to tests/). Standard library only; no network, no API spend."""
import importlib.util, json, os, stat, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

ORCH_PY = Path(os.environ.get("ORCH_PY") or Path(__file__).resolve().parent.parent / "orch.py")
spec = importlib.util.spec_from_file_location("orch", ORCH_PY); orch = importlib.util.module_from_spec(spec); spec.loader.exec_module(orch)

def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="orch-test-"))
    (d / "orch.toml").write_text(textwrap.dedent('''
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
    return d, orch.Cfg(d)

class Ledger(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]
    def rows(self): return (self.d / "out/ledger.tsv").read_text().splitlines()

    def test_one_running_one_done_idempotent(self):
        run = orch.reserve_budget(self.cfg, self.mc, "u1", 2)
        self.assertEqual(orch.reserve_budget(self.cfg, self.mc, "u1", 2, run), run)   # again: no second running row
        self.assertTrue(orch.ledger_done(self.cfg, self.pv, run, "u1", 0.5, 0))
        self.assertFalse(orch.ledger_done(self.cfg, self.pv, run, "u1", 0.5, 0))      # again: no second done row
        self.assertEqual(len(self.rows()), 2)
        self.assertAlmostEqual(orch.committed(self.cfg, self.pv), 0.5)
        with self.assertRaises(orch.Err): orch.reserve_budget(self.cfg, self.mc, "u1", 2, run)   # a finished run never re-runs

    def test_cap_per_run_id(self):
        run = orch.reserve_budget(self.cfg, self.mc, "u1", 2)
        for _ in range(3): orch.ledger_append(self.cfg, self.pv, ["2026-01-01 00:00:00", "u1", "3", "done rc=0", run])   # a looping writer
        runs, bad, _ = orch.ledger_read(self.cfg, self.pv)
        self.assertEqual(orch.spend(runs), 3.0)    # counted once per run id, not 9
        self.assertTrue(bad)                        # but the ledger is malformed ...
        with self.assertRaises(orch.Err) as e: orch.reserve_budget(self.cfg, self.mc, "u2", 1)   # ... so launches are refused
        self.assertIn("malformed", str(e.exception))
        self.assertFalse(orch.model_ok(self.cfg, "m")[0])

    def test_cap_refuses(self):
        orch.reserve_budget(self.cfg, self.mc, "u1", 9)
        with self.assertRaises(orch.Err): orch.reserve_budget(self.cfg, self.mc, "u2", 2)

    def test_repair(self):
        run = orch.reserve_budget(self.cfg, self.mc, "u1", 2)
        for _ in range(4): orch.ledger_append(self.cfg, self.pv, ["2026-01-01 00:00:00", "u1", "1", "done rc=0", run])
        orch.ledger_append(self.cfg, self.pv, ["garbage"])
        a = type("A", (), {"action": "check", "provider": None})()
        self.assertEqual(orch.cmd_ledger(self.cfg, a), 1)
        a.action = "repair"; self.assertEqual(orch.cmd_ledger(self.cfg, a), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(list((self.d / "out").glob("ledger.tsv.bak-*")))
        a.action = "check"; self.assertEqual(orch.cmd_ledger(self.cfg, a), 0)

class Secret(unittest.TestCase):
    def test_key_file_mode(self):
        d, cfg = project(); k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n")
        k.chmod(0o644)
        with self.assertRaises(orch.Err): orch.read_secret(cfg.prov["api"])
        k.chmod(0o600)
        self.assertEqual(orch.read_secret(cfg.prov["api"]), ("ANTHROPIC_API_KEY", "abc123"))

class ApiRun(unittest.TestCase):
    def test_fake_agent(self):
        """orch api-run with a fake agent CLI: the key reaches only the agent, the ledger gets one running and one done row."""
        d, cfg = project('\n[providers.api.x]\n')
        fake = d / "fake.py"
        fake.write_text("import json, os, sys\nk = os.environ.get('ANTHROPIC_API_KEY')\n"
                        "print(json.dumps({'total_cost_usd': 0.25, 'key_ok': k == 'abc123', 'argv_has_key': any('abc123' in a for a in sys.argv)}))\n")
        t = (d / "orch.toml").read_text().replace("[providers.api.x]\n", "").replace('enabled = true\ncap_usd', f'enabled = true\ncmd = ["{sys.executable}", "{fake}", "{{prompt}}"]\ncap_usd', 1)
        (d / "orch.toml").write_text(t); k = d / "key.env"; k.write_text("ANTHROPIC_API_KEY=abc123\n"); k.chmod(0o600)
        (d / "briefs/u.md").write_text("do nothing\n")
        env = {x: v for x, v in os.environ.items() if x not in ("ANTHROPIC_API_KEY", "ORCH_ROOT", "ORCH_UNIT", "ORCH_ROLE", "ORCH_REPORT")}
        r = subprocess.run([sys.executable, str(ORCH_PY), "api-run", "u", "--run", "u.1.1", "--model", "m", "--budget", "1",
                            "--brief", str(d / "briefs/u.md"), "--cwd", str(d)], cwd=d, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads((d / "out/u/api.json").read_text())
        self.assertTrue(out["key_ok"]); self.assertFalse(out["argv_has_key"])
        rows = (d / "out/ledger.tsv").read_text().splitlines()
        self.assertEqual([x.split("\t")[2] for x in rows], ["running", "0.25"])
        r = subprocess.run([sys.executable, str(ORCH_PY), "api-run", "u", "--run", "u.1.1", "--model", "m", "--budget", "1",
                            "--brief", str(d / "briefs/u.md"), "--cwd", str(d)], cwd=d, env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)   # the same run id again: refused, nothing written
        self.assertEqual(len((d / "out/ledger.tsv").read_text().splitlines()), 2)
        self.assertNotIn("abc123", r.stdout + r.stderr + (d / "out/ledger.tsv").read_text())

class Hooks(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project()
    def j(self, c): return orch.judge(self.cfg, c, str(self.d), orch.edit_ok_dirs(self.cfg))
    def test_data_is_not_commands(self):
        for c in ['git commit -m "close the worktrees while loop"', "git commit -m 'for each worktree, kill it'",
                  'git commit -m "$(cat <<\'EOF\'\nfix: while loop over worktrees; orch close --force; kill 1\nEOF\n)"',
                  'git commit -F - <<EOF\nrm -rf / ; kill 1; for x in worktrees; do git add; done\nEOF', 'echo "kill -9 1"',
                  'bd comment x-1 "kill the loop; rm -rf it"', 'git -C /x/worktrees/a commit -m "while"',
                  'ssh host pueue status', "ssh -o BatchMode=yes h 'pueue status --json' | jq .", 'ssh h pueue --color never status',
                  'ssh h "pueue group; pueue log 3"', 'for h in a b; do ssh $h pueue status; done']:
            self.assertIsNone(self.j(c), c)
    def test_commands_are_judged(self):
        for c in ['for d in worktrees/*; do git -C $d add -A; done', 'kill 5', 'ls | xargs rm -rf', 'bash -c "kill 3"',
                  'ssh h pueue kill 3', 'ssh h pueue parallel -g jobs 4', 'echo "$(kill 1)"', 'git push --force',
                  'orch api-run u --run x', 'cat <<EOF | bash\nkill 1\nEOF']:
            self.assertIsNotNone(self.j(c), c)

class Backlog(unittest.TestCase):
    def test_table_and_checkboxes(self):
        d, cfg = project('\n[backlog]\nfile = "S.md"\nready = ["READY", "FIX"]\n')
        (d / "S.md").write_text("# S\n| # | item | status | next |\n|---|---|---|---|\n| A1 | one | DONE | - |\n| A2 | two | FIX | x |\n"
                                "| A3 | three | NOT STARTED | y |\n## more\n- [ ] four things\n- [x] five\n")
        it = {i: (st, dn, rd) for i, t, st, dn, rd in orch.backlog_items(cfg)}
        self.assertEqual(it["A1"], ("DONE", True, False)); self.assertEqual(it["A2"], ("FIX", False, True))
        self.assertEqual(it["A3"], ("NOT STARTED", False, False)); self.assertEqual(it["four-things"][1:], (False, False))

class Resume(unittest.TestCase):
    def test_report_path(self):
        d, cfg = project(); (d / "out/orig").mkdir(); rep = d / "out/orig/REPORT.md"; rep.write_text("VERDICT: BLOCKED\nold\nVERDICT: BLOCKED\n")
        b = d / "briefs/r.md"; b.write_text(f"Continue the same report {rep} (line 1 PENDING).\n")
        a = type("A", (), {"report": None, "resume_of": None, "unit": "r"})()
        self.assertIsNone(orch.resume_report(cfg, a, b))   # never parsed from the brief's prose (a launch died on such a path)
        a.resume_of = "orig"; b.write_text("x\n"); self.assertEqual(orch.resume_report(cfg, a, b), rep.resolve())
        a.resume_of = None; a.report = str(rep); self.assertEqual(orch.resume_report(cfg, a, b), rep.resolve())
        u = {"name": "r", "report": str(rep), "resume": True, "started": time.time() + 5}
        self.assertIsNone(orch.verdict(cfg, u))   # the old verdict does not count
        u["started"] = time.time() - 5; rep.write_text("VERDICT: DONE\nnew\nVERDICT: DONE\n")
        self.assertEqual(orch.verdict(cfg, u), "VERDICT: DONE")

    def test_pueue_command_quoting(self):
        """pueue joins `add -- <args>` with spaces and runs sh -c on it: a value reaches the task intact only when quoted twice."""
        odd = "/x/(`B/out it's/REPORT.md"
        words = [sys.executable, "-c", "import sys; print(sys.argv[1])", odd]
        stored = subprocess.run(["sh", "-c", f"{sys.executable} -c 'import sys; print(\" \".join(sys.argv[1:]))' {orch.pq(words)}"],
                                capture_output=True, text=True).stdout.rstrip("\n")      # what pueue stores as the command
        self.assertEqual(subprocess.run(["sh", "-c", stored], capture_output=True, text=True).stdout.rstrip("\n"), odd)

class SelfSafety(unittest.TestCase):
    def test_in_place_edit_flagged_rename_not(self):
        d = Path(tempfile.mkdtemp()); s = d / "loop.sh"; s.write_text("#!/bin/bash\nsleep 30\necho done\n")
        p = subprocess.Popen(["/bin/bash", str(s)]); time.sleep(1.5)
        try:
            orch.atomic_write(s, "#!/bin/bash\nsleep 30\necho other\n", 0o755); time.sleep(1.1)
            self.assertFalse([k for k, _ in orch.script_findings() if k.startswith(f"script:{p.pid}:")])   # renamed: safe
        finally: p.kill(); p.wait()
        p = subprocess.Popen(["/bin/bash", str(s)]); time.sleep(1.5)
        try:
            with open(s, "r+") as f: f.seek(0, 2); f.write("echo appended\n")   # in place
            time.sleep(1.1)
            self.assertTrue([k for k, _ in orch.script_findings() if k.startswith(f"script:{p.pid}:")])
        finally: p.kill(); p.wait()

class Register(unittest.TestCase):
    def test_tree_guard(self):
        d = Path(tempfile.mkdtemp()); g = lambda *x: subprocess.run(["git", "-C", str(d), *x], capture_output=True)
        g("init", "-q", "-b", "main"); (d / "a").write_text("x\n"); g("add", "a"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a")
        _, cfg = project()
        self.assertIsNone(orch.tree_guard(cfg, d))
        (d / "a").write_text("<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n")
        self.assertIn("conflict markers", orch.tree_guard(cfg, d))
        (d / "a").write_text("x\n"); g("checkout", "-q", "-b", "wk"); g("checkout", "-q", "main")
        for i in range(11): (d / "a").write_text(f"{i}\n"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", str(i))
        g("checkout", "-q", "wk")
        self.assertIn("11 commits behind", orch.tree_guard(cfg, d))       # gates and land: refused
        self.assertIsNone(orch.tree_guard(cfg, d, behind=False))            # an agent's own jobs: never for being behind (item 5)
    def test_shim_labels_agent_jobs(self):
        self.assertIn('add -l "unit:$ORCH_UNIT"', orch.clean_pueue_script({"local": True}))
    def test_rg_patterns_are_data(self):
        d, cfg = project()
        for c in ['rg -n "rm -rf|kill -9" .', "grep -c 'pueue kill' log"]: self.assertIsNone(orch.judge(cfg, c, str(d), orch.edit_ok_dirs(cfg)), c)

def host_probe(**k):
    """A probe result as probe() returns it, for the capacity model."""
    return {"ok": True, "res": [], "hist": [], "sched": [], "direct": [], "pueue": {"tasks": {}}, "cap_pause": "", **k}

class Capacity(unittest.TestCase):
    """orch-fixes8: reservations, history, back-pressure, agents against build capacity (runs/cap.sh is the one rule)."""
    def setUp(self):
        self.d, self.cfg = project('\n[hosts.b]\nssh = "b"\ncores = 8\ncores_per_job = 2\nram_gb = 8\nservices_gb = 2\njob_gb = 2.5\nagent_gb = 1\nmin_disk_gb = 0\n')
        self.h = dict(self.cfg.hosts["b"], local=True, _rd=str(self.d / "rd")); (self.d / "rd").mkdir()
        (self.d / "rd/cap.sh").write_text(orch.cap_lib(self.cfg, "b", self.h))
    def sh(self, script, env=None):
        return subprocess.run(["sh", "-c", f'RD={self.d}/rd; . $RD/cap.sh\n' + script], capture_output=True, text=True, env={**os.environ, **(env or {})}).stdout
    def test_class_and_learned_reservation_match_python(self):
        self.assertEqual(self.sh('cap_class "cd processing && FOO=1 nice -n 10 cargo test -p x"').strip(), "cargo_test")
        self.assertEqual(self.sh('cap_mb cargo_test 2560 2560').strip(), "2560")              # nothing learned: job_gb
        hist = ["1\tcargo_test\t900\t60\t0", "2\tcargo_test\t1200\t300\t0", "3\tother\t9000\t5\t0"] + [f"{i}\tagent\t{v}\t9\t0" for i, v in enumerate((200, 700, 300))]
        (self.d / "rd/history.tsv").write_text("\n".join(hist) + "\n")
        for cls in ("cargo_test", "other", "agent", "none"):
            cap = 0 if cls == "agent" else 2560
            self.assertEqual(int(self.sh(f'cap_mb {cls} 2560 {cap}')), orch.class_mb(hist, cls, 2560, cap), cls)
        self.assertEqual(orch.class_mb(hist, "cargo_test", 2560, 2560), 1380)   # max 1200 + 15 %
        self.assertEqual(orch.class_mb(hist, "agent", 1024, 0), 345)            # median 300 + 15 %
    def test_reservations_admission_and_pause(self):
        holder = subprocess.Popen(["sleep", "60"])
        try:
            (self.d / "rd/a.res").write_text(f"{holder.pid} 6 3000 1 cargo_test\n"); (self.d / "rd/gone.res").write_text("999999 2 9000 1 x\n")
            fake = 'cap_measure() { PSI=${T_PSI:-0}; LOAD=${T_LOAD:-1}; DF=50; }\n'
            out = self.sh(fake + 'CAP_RO=; admit_ok cargo_test && echo ADMIT || echo WAIT; echo "$RJ $RC $RM"')
            self.assertIn("ADMIT", out); self.assertIn("1 6 3000", out)        # 6 + 2 of 8 cores, 3000 + 2560 of 6144 MB: fits
            self.assertFalse((self.d / "rd/gone.res").exists())                # a reservation whose process ended is removed
        finally: pass
        try:
            (self.d / "rd/b.res").write_text(f"{holder.pid} 2 100 1 x\n")      # 8 of 8 cores reserved: the next job waits
            self.assertIn("WAIT", self.sh(fake + 'admit_ok cargo_test && echo ADMIT || echo WAIT'))
            (self.d / "rd/b.res").unlink()
            out = self.sh(fake + 'admit_ok cargo_test && echo ADMIT || echo WAIT; echo "P=$PAUSE"', {"T_PSI": "12.5"})
            self.assertIn("WAIT", out); self.assertIn("memory PSI", out)       # back-pressure pauses admission ...
            self.assertTrue(holder.poll() is None)                             # ... and kills nothing
            out = self.sh(fake + 'HOLD=0; admit_ok cargo_test && echo ADMIT || echo WAIT; echo "P=$PAUSE"', {"T_LOAD": "11"})
            self.assertIn("load 11 over 1.25 x 8", out)
            out = self.sh(fake + 'HOLD=60; CAP_RO=; admit_ok x && echo ADMIT || echo WAIT', {"T_LOAD": "11"})   # load must be held HOLD s
            self.assertIn("ADMIT", out)
        finally: holder.kill(); holder.wait()
    def test_job_script_admits_runs_records(self):
        """A Mac (local) jobs-lane job (register item 5): waits for admission, holds its reservation, records its peak."""
        holder = subprocess.Popen(["sleep", "60"])
        rd = self.d / "rd"; (rd / "h.res").write_text(f"{holder.pid} 8 100 1 x\n")   # all cores reserved by another job
        a = type("A", (), {"lane": "jobs", "subdir": None, "wt": str(self.d), "usb": False})()
        cmd = sys.executable + " -c 'import os, time; b = bytearray(os.urandom(60 * 1024 * 1024)); time.sleep(6)'"   # incompressible: macOS compresses zero pages under pressure
        js = orch.job_script(self.cfg, "b", self.h, a, "t", cmd, "t-1"); (rd / "t-1.sh").write_text(js)
        p = subprocess.Popen(["bash", str(rd / "t-1.sh")], env={**os.environ, "ORCH_POLL_S": "1"}, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            time.sleep(3)
            self.assertIn("waiting for admission", (rd / "t-1.log").read_text()); self.assertFalse((rd / "t-1.res").exists())
            holder.kill(); holder.wait(); time.sleep(3)
            self.assertTrue((rd / "t-1.res").exists())                         # admitted: its reservation is held while it runs
            self.assertEqual(p.wait(timeout=30), 0)
        finally:
            holder.kill()
            if p.poll() is None: os.killpg(p.pid, 9)
        self.assertFalse((rd / "t-1.res").exists())
        f = (rd / "history.tsv").read_text().split("\t")
        self.assertTrue(f[1].startswith("python")); self.assertGreaterEqual(int(f[2]), 50)   # class and its peak (60 MB held)
    def test_launch_host_option(self):
        """review-fixes8 #1: --host placed the agent with a 2-tuple where choose() unpacks 3."""
        d, cfg = project()
        a = type("A", (), {"host": "here", "test_host": None, "unit": "u", "kind": "review"})()
        hs = {"here": {"name": "here", "ok": True, "avail_mb": "8000", "disk_gb": "100"}}
        hn, b, why = orch.choose(cfg, a, d / "briefs/none.md", cfg.models["m"], [], hs)
        self.assertEqual((hn, b), ("here", "here"))
    def test_build_room_and_when(self):
        p = host_probe(cap_rj="2", cap_rc="4", cap_rm="5000", hist=[f"{int(time.time()) - 100}\tcargo_test\t2000\t600\t0"],
                       res=[f"1 2 2300 {int(time.time()) - 60} cargo_test a.res"])
        units = [{"name": "u1", "host": "b", "build": "b"}]
        hc = orch.host_cap(self.cfg, "b", p, units)
        self.assertEqual(hc["slots"], 2)                                       # 6144 MB - 1 agent 1024 MB over 2300 MB per job; cores 4
        self.assertTrue(orch.build_room(self.cfg, "b", hc)[0])                 # 1 agent for 2 slots: a free reservation
        units += [{"name": f"u{i}", "host": "mac", "build": "b"} for i in (2, 3)]
        hc = orch.host_cap(self.cfg, "b", p, units); ok, why = orch.build_room(self.cfg, "b", hc)
        self.assertTrue(ok, why); self.assertIn("queue position", why)          # 3 agents, 2 slots: a queue position (1 of 2 running jobs)
        units += [{"name": "u4", "host": "mac", "build": "b"}]
        hc = orch.host_cap(self.cfg, "b", p, units); ok, why = orch.build_room(self.cfg, "b", hc)
        self.assertFalse(ok); self.assertIn("should free about", why)          # 4 agents = 2 x slots: refused, and says when
        hc = orch.host_cap(self.cfg, "b", dict(p, cap_pause="memory PSI avg10 30 over 10"), units[:1])
        self.assertFalse(orch.build_room(self.cfg, "b", hc)[0])

class Fill(unittest.TestCase):
    def test_fill_until_capacity_or_budget(self):
        d, cfg = project('\n[backlog]\nfile = "S.md"\n[routing]\nnormal = ["m"]\ncritical = ["m"]\n'
                         '[hosts.b]\nssh = "b"\ncores = 4\ncores_per_job = 2\nram_gb = 8\nservices_gb = 2\njob_gb = 2\nagent_gb = 1\n')
        (d / "S.md").write_text("| # | item | status |\n|---|---|---|\n| X1 | one | READY |\n| X2 | two | READY |\n| X3 | three | READY |\n| X4 | four | DONE |\n")
        for n, it in (("x1-a", "X1"), ("x2-a", "X2"), ("x3-a", "X3")): (d / f"briefs/{n}.md").write_text(f"# {n}\nITEM: {it}\nTEST-HOST: b\n")
        cfg.hosts["here"].update(ram_gb=64, cores=16)
        orch.Beads.run = lambda self, *a: (_ for _ in ()).throw(orch.Err("no beads"))   # no beads here: the checklist is the backlog
        hs = {"here": host_probe(name="here"), "b": host_probe(name="b")}
        plan, stops = orch.fill_plan(cfg, hs, [])
        self.assertEqual([x[0] for x in plan], ["x1-a", "x2-a"])               # b has 2 build slots (4 cores / 2): two agents ...
        self.assertTrue(all(x[4] == "b" for x in plan))
        self.assertIn("x3-a", stops[0])                                        # ... but no more queue position: nothing runs yet
        cfg.prov["api"]["cap_usd"] = 1.5                                       # budget_per_unit 1: the second does not fit
        plan, stops = orch.fill_plan(cfg, hs, [])
        self.assertEqual(len(plan), 1); self.assertIn("budget", stops[0])

class Budget(unittest.TestCase):
    def test_median_of_kind_and_tier_plus_margin(self):
        d, cfg = project('\n[budget]\nmargin_pct = 50\n')
        cfg._all_units = {f"u{i}": {"name": f"u{i}", "kind": "implement", "tier": "critical"} for i in range(4)}
        for i, c in enumerate((1.0, 2.0, 3.0, 9.0)):
            orch.ledger_append(cfg, cfg.prov["api"], ["t", f"u{i}", "running", 5, f"r{i}"]); orch.ledger_append(cfg, cfg.prov["api"], ["t", f"u{i}", c, "done rc=0", f"r{i}"])
        b, why = orch.budget_for(cfg, cfg.models["m"], "implement", "critical")
        self.assertEqual(b, 4.0); self.assertIn("median $2.50", why)            # median 2.5 + 50 % = 3.75, rounded up to $0.50
        self.assertEqual(orch.budget_for(cfg, cfg.models["m"], "review", "normal")[0], 1)   # no history: budget_per_unit

class Land(unittest.TestCase):
    def setUp(self):
        self.d, self.cfg = project('\n[landing]\nbranch = "main"\n')
        self.r = self.d / "repo"; self.r.mkdir(); self.wt = self.d / "wt"
        self.g("init", "-q", "-b", "main"); (self.r / "a").write_text("1\n2\n3\n"); self.g("add", "a"); self.g("commit", "-qm", "base")
        self.g("worktree", "add", "-q", "-b", "wk/u", str(self.wt))
        self.u = {"id": "x-1", "name": "u", "cwd": str(self.wt), "tier": "normal"}
        orch.Beads.units = lambda s: [self.u]; orch.Beads.comment = lambda s, *a: None
        self.cfg._all_units = {"u": self.u}
    def g(self, *a, cwd=None): return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd or self.r), *a], capture_output=True, text=True)
    def a(self, **k): return type("A", (), {"unit": "u", "no_gate": True, "no_dispatch": True, **k})()
    def test_squash_land_and_conflict_stop(self):
        (self.wt / "b").write_text("new\n"); self.g("add", "b", cwd=self.wt); self.g("commit", "-qm", "u: add b", cwd=self.wt)
        (self.r / "c").write_text("main moved\n"); self.g("add", "c"); self.g("commit", "-qm", "main: c")   # main moves: merge needed
        self.assertEqual(orch.cmd_land(self.cfg, self.a()), 0)
        self.assertEqual(self.g("log", "--format=%s", "-1").stdout.strip(), "u: land wk/u (1 commit(s), squashed)")
        self.assertEqual(sorted(p.name for p in self.r.iterdir() if p.name != ".git"), ["a", "b", "c"])   # the main checkout followed
        self.assertEqual(self.g("rev-list", "--count", "main").stdout.strip(), "3")                        # one squashed commit
        (self.wt / "a").write_text("1\nX\n3\n"); self.g("commit", "-qam", "u: a", cwd=self.wt)
        (self.r / "a").write_text("1\nY\n3\n"); self.g("commit", "-qam", "main: a")
        head = self.g("rev-parse", "HEAD", cwd=self.wt).stdout
        self.assertEqual(orch.cmd_land(self.cfg, self.a()), 3)                 # conflict: stops ...
        self.assertEqual(self.g("rev-parse", "HEAD", cwd=self.wt).stdout, head)   # ... with the tree untouched
        self.assertNotEqual(self.g("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=self.wt).returncode, 0)
    def test_land_once(self):
        """review-fixes8 #2: after a squash-land the unit counts as landed (its tree is on main), and a second land is refused."""
        (self.wt / "b").write_text("new\n"); self.g("add", "b", cwd=self.wt); self.g("commit", "-qm", "u: b", cwd=self.wt)
        (self.r / "c").write_text("main moved\n"); self.g("add", "c"); self.g("commit", "-qm", "main: c")
        self.assertIs(orch.landed(self.cfg, self.u), False)
        self.assertEqual(orch.cmd_land(self.cfg, self.a()), 0)
        self.assertIs(orch.landed(self.cfg, self.u), True)
        with self.assertRaises(orch.Err) as e: orch.cmd_land(self.cfg, self.a())
        self.assertIn("already landed", str(e.exception))
        self.assertEqual(self.g("rev-list", "--count", "main").stdout.strip(), "3")   # no duplicate squash commit
    def test_gate_required(self):
        (self.wt / "b").write_text("new\n"); self.g("add", "b", cwd=self.wt); self.g("commit", "-qm", "u: b", cwd=self.wt)
        with self.assertRaises(orch.Err) as e: orch.cmd_land(self.cfg, self.a(no_gate=False))
        self.assertIn("final_gate", str(e.exception))

class Probe(unittest.TestCase):
    def test_scheduler_and_capacity_report(self):
        """One scheduler per host (item 1): another job-running process shows in the probe; the probe carries cap.sh's report."""
        d, cfg = project('\n[hosts.here]\n'.replace("[hosts.here]", "") )
        h = dict(cfg.hosts["here"], schedulers=["sleep 4242"]); cfg.hosts["here"] = h
        s = subprocess.Popen(["sleep", "4242"])
        try:
            Path(h["_rd"]).mkdir(parents=True, exist_ok=True); (Path(h["_rd"]) / "x.res").write_text(f"{s.pid} 1 700 1 cargo_test\n")
            p = orch.probe("here", h, ro=True, cfg=cfg)
            self.assertTrue(p["ok"], p); self.assertTrue(any(str(s.pid) in x for x in p["sched"]), p["sched"])
            self.assertEqual((p["cap_rj"], p["cap_rm"]), ("1", "700")); self.assertTrue(p["res"])
        finally: s.kill(); s.wait()

class Park(unittest.TestCase):
    """orch-fixes9 item 1: a WAITING line parks a unit on its jobs; PENDING is no verdict; a resume continues the session."""
    def test_waiting_line_jobs_and_states(self):
        d, cfg = project(); r = d / "out/u/REPORT.md"; r.parent.mkdir(parents=True)
        r.write_text("VERDICT: PENDING\nstep 1 done\nWAITING here:7 here:8 nohost:9\n")
        u = {"name": "u", "started": 0}
        self.assertIsNone(orch.verdict(cfg, u))   # PENDING is not a verdict (close and park relied on that)
        self.assertEqual(orch.waiting_line_jobs(cfg, u), ["here:7", "here:8"])   # unknown hosts ignored
        u["handled_jobs"] = ["here:7"]; self.assertEqual(orch.waiting_line_jobs(cfg, u), ["here:8"])
        hs = {"here": {"ok": True, "waitjob": ["wt-1"], "pueue": {"tasks": {
            "7": {"status": {"Done": {"result": "Success"}}}, "8": {"status": {"Running": {}}, "command": "bash /x/runs/wt-1.sh", "label": "unit:u"},
            "9": {"status": {"Queued": {}}, "label": "unit:u"}}}}}
        self.assertEqual(orch.job_state(hs, "here:7"), ("done", 0))
        self.assertEqual(orch.job_state(hs, "here:8")[0], "admission")   # running, but still in orch's admission wait
        self.assertEqual(orch.job_state(hs, "here:9")[0], "queued")
        self.assertEqual(orch.job_state(hs, "here:5")[0], "gone")
        self.assertEqual(sorted(orch.unit_jobs(hs, "u")), ["here:8", "here:9"])
        r.write_text("VERDICT: DONE\nexit codes: 0\nWAITING here:8\n"); self.assertEqual(orch.verdict(cfg, u), "VERDICT: DONE")

    def test_wrapup_once_when_capped_without_verdict(self):
        """item 2: a run that ended at its budget cap with no final verdict is wrapped up once; not with a verdict, not twice."""
        d, cfg = project(); o = d / "out/u"; o.mkdir(parents=True); (o / "REPORT.md").write_text("VERDICT: PENDING\nstep 1\n")
        (o / "api.json").write_text(json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": 1.0}))
        u = {"name": "u", "via": "pueue", "model": "m", "state": "failed", "verdict": None, "started": 0}
        self.assertTrue(orch.wrap_due(cfg, u))
        self.assertFalse(orch.wrap_due(cfg, {**u, "wrapup": "u.r2.1"}))
        self.assertFalse(orch.wrap_due(cfg, {**u, "verdict": "VERDICT: BLOCKED"}))
        self.assertFalse(orch.wrap_due(cfg, {**u, "state": "working"}))
        (o / "api.json").write_text(json.dumps({"subtype": "success"})); self.assertFalse(orch.wrap_due(cfg, u))

    def test_orphan_jobs(self):
        """item 3: a job whose owner is gone: closed unit, ended agent not parked, ended wrapper; never an idle herdr agent's or a
        parked unit's job."""
        d, cfg = project(); run = lambda: {"status": {"Running": {}}}
        hs = {"here": {"ok": True, "pueue": {"tasks": {
            "1": {**run(), "label": "unit:closed"}, "2": {**run(), "label": "unit:api"}, "3": {**run(), "label": "unit:idle"},
            "4": {**run(), "label": "unit:parked"}, "5": {**run(), "label": "run:x"}, "6": {"status": {"Done": {"result": "Success"}}, "label": "unit:closed"}}}}}
        units = [{"name": "api", "via": "pueue", "state": "done"}, {"name": "idle", "via": "herdr", "state": "idle"},
                 {"name": "parked", "via": "pueue", "state": "parked", "_park": ["here:4"]}]
        wd = d / ".orch/runs/wrap"; wd.mkdir(parents=True); (wd / "w-1").write_text("here:5 x-1\n")   # its wrapper is not running
        got = dict(orch.orphan_jobs(cfg, hs, units))
        self.assertEqual(sorted(got), ["here:1", "here:2", "here:5"], got)
        self.assertEqual(orch.stop_job_cmd({"runner": "podman"}, "j1").split(";")[0], "podman stop -t 5 orch-run-j1 >/dev/null 2>&1")
        self.assertIn("orch-j1.scope", orch.stop_job_cmd({}, "j1"))

    def test_uncollected_guard(self):
        """item 4: a remote copy with changes or output that collect has not brought here refuses stop/close; a fresh copy does not."""
        d, cfg = project(); r = d / "remote"
        for x in ("base/u", "worktrees/u", "out/u"): (r / x).mkdir(parents=True)
        (r / "base/u/f").write_text("a\n"); (r / "worktrees/u/f").write_text("a\n"); (r / "out/u/REPORT.md").write_text("VERDICT: PENDING\n")
        cfg.hosts["r"] = {"ssh": "nowhere", "root": str(r), "_rd": str(r / "runs")}
        real = orch.hrun; orch.hrun = lambda h, script, **k: real({"local": True}, script, **k)
        try:
            u = {"name": "u", "host": "r", "via": "tmux"}
            self.assertIsNone(orch.uncollected(cfg, u))                        # nothing written yet
            (r / "worktrees/u/f").write_text("a\nb\n"); self.assertIn("not collected", orch.uncollected(cfg, u))
            u["collected"] = orch.fingerprint(cfg, u)["fp"]; self.assertIsNone(orch.uncollected(cfg, u))   # as collect records it
            (r / "out/u/REPORT.md").write_text("VERDICT: PENDING\nstep 2\n"); self.assertIn("not collected", orch.uncollected(cfg, u))
        finally: orch.hrun = real

    def test_resume_argv_and_no_ack(self):
        mc = {"api_id": "x"}
        a = orch.api_argv({}, mc, 1, "go", "S1")
        self.assertEqual(a[:5], ["claude", "--bare", "-p", "--resume", "S1"]); self.assertEqual(a[-1], "go")
        self.assertEqual(orch.api_argv({"cmd": ["c", "{prompt}"], "resume_cmd": ["c", "resume", "{session}", "{prompt}"]}, mc, 1, "go", "S1"), ["c", "resume", "S1", "go"])
        with self.assertRaises(orch.Err): orch.api_argv({"cmd": ["c", "{prompt}"]}, mc, 1, "go", "S1")   # cannot guess how to resume
        d, cfg = project()
        with self.assertRaises(orch.Err): orch.cmd_ack(cfg, type("A", (), {"problem": "waiting:u", "decision": "x"})())

class Report(unittest.TestCase):
    def test_outcomes_waste_pause_and_checkpoint(self):
        """item 6: spend by outcome; capped-without-verdict spend over waste_pause % pauses launch until acked; report-due."""
        d, cfg = project('\n[report]\nevery_h = 0\nat = ["00:00"]\n'); mc = cfg.models["m"]; pv = cfg.prov["api"]
        for n, cost, sub, rep in (("ok", 1.0, "success", "VERDICT: DONE\nexit codes 0\n"), ("cap", 3.0, "error_max_budget_usd", "VERDICT: PENDING\n"),
                                  ("half", 1.0, "error_max_budget_usd", "VERDICT: PENDING\nstep 1 done\nstep 2 done\n")):
            run = orch.reserve_budget(cfg, mc, n, 5, f"{n}.1"); orch.ledger_done(cfg, pv, run, n, cost, 0)
            o = d / "out" / n; o.mkdir(); (o / "REPORT.md").write_text(rep); (o / f"api-{run}.json").write_text(json.dumps({"subtype": sub}))
        orch.reserve_budget(cfg, mc, "lost", 2, "lost.1")
        cfg._all_units = {n: {"name": n, "run": f"{n}.1", "status": "closed"} for n in ("ok", "cap", "half", "lost")}
        got = {x[2]: x[4] for x in orch.run_outcomes(cfg)}
        self.assertEqual(got, {"ok": "verdict", "cap": "capped-nothing", "half": "capped-partial", "lost": "unknown"})
        self.assertIn("waste pause", "refused (waste pause): " + (orch.waste_paused(cfg) or ""))   # 4 of 7 dollars capped: over 25 %
        self.assertIsNotNone(orch.waste_paused(cfg))
        orch.cmd_ack(cfg, type("A", (), {"problem": "waste-pause", "decision": "cause: budgets too small; fixed"})())
        self.assertIsNone(orch.waste_paused(cfg))   # the orchestrator acked the cause
        self.assertIn("report-due", [k for k, _ in orch.report_findings(cfg, [])])
        cfg.touch("report_sent"); self.assertNotIn("report-due", [k for k, _ in orch.report_findings(cfg, [])])

class FalseFindings(unittest.TestCase):
    """item 7: findings that were false."""
    def test_localtest_skips_a_shim_whose_child_is_orch_run(self):
        d, cfg = project(); cfg._viol = []
        shim = " 10     1    10   0.0    01:00 sh -c run-tests.sh cargo test -p x\n"
        self.assertIn("localtest:10", [k for k, _ in orch.proc_findings(cfg, shim)])          # a build outside the lanes: flagged
        child = shim + " 11    10    10   0.0    00:59 python3 /x/orch.py run --host h --wt /w -- cargo test -p x\n"
        self.assertNotIn("localtest:10", [k for k, _ in orch.proc_findings(cfg, child)])     # its child is orch run: a lane call

    def test_git_dirty_ignores_state_files(self):
        d, cfg = project(); g = lambda *x: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *x], capture_output=True)
        (d / ".orch/acks.json").write_text("{}"); g("init", "-q"); g("add", "-A"); g("commit", "-qm", "a")
        (d / ".orch/acks.json").write_text('{"x": 1}'); (d / ".orch/open.json").write_text("{}")   # an ack, a check: orch's own
        self.assertNotIn("git:dirty", [k for k, _ in orch.git_findings(cfg)])
        (d / ".orch/history.md").write_text("notes\n")                                       # the orchestrator's notes count
        self.assertIn("git:dirty", [k for k, _ in orch.git_findings(cfg)])

    def test_idle_lane_uses_the_admission_verdict(self):
        """row 48: jobs admitted inside their scripts (waiting there) are held by admission: the lane is not idle."""
        d, cfg = project('\n[capacity]\nmin_disk_gb = 100000\n'); h = cfg.hosts["here"]
        p = orch.probe("here", h, ro=True, cfg=cfg)
        self.assertIn("disk", p["cap_admit"])                      # the admission's own verdict: blocked by the disk floor
        p["pueue"] = {"groups": {"jobs": {"status": "Running", "parallel_tasks": 4}}, "tasks": {
            "1": {"group": "jobs", "status": {"Running": {}}, "label": "unit:u"}, "2": {"group": "jobs", "status": {"Queued": {}}, "label": "unit:u"}}}
        p["wait_jobs"] = "1"
        self.assertEqual([k for k, _ in orch.lane_findings(cfg, {"here": p}) if k.startswith("idle:")], [])
        p["wait_jobs"] = "0"; p["cap_admit"] = "yes"   # nothing held by admission, it would admit one, pueue's limit holds the queue: idle
        p["pueue"]["groups"]["jobs"]["parallel_tasks"] = 1
        self.assertIn("idle:here:jobs", [k for k, _ in orch.lane_findings(cfg, {"here": p})])

class Generic(unittest.TestCase):
    def test_no_project_details_in_code(self):
        import re
        bad = re.compile(r"example-project|app|linux1|linux2|fable|user|/Users/|/srv/|d435|orch-anthropic-api|claude-(opus|sonnet|haiku)", re.I)
        here = ORCH_PY.parent
        for f in [ORCH_PY, here / "install", *sorted((here / "skill").glob("*")), here / "example.orch.toml"]:
            if f.is_file():
                hits = [f"{f.name}:{i}" for i, l in enumerate(f.read_text().splitlines(), 1) if bad.search(l)]
                self.assertEqual(hits, [], f"project details in {f}")

if __name__ == "__main__": unittest.main()
