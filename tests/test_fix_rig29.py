"""rig29: 29 defects the collection-rig orchestrator reported against release 20261124-113728 (opencode agents over tmux on rigpc,
launch and routing, jobs, collect/close/land, the guard hook, tidy, the ledger). One test per failure, each naming what it catches.
Standard library only; no network, no spend. Run by ./check (it discovers tests/test_*.py)."""
import argparse, contextlib, io, json, os, re, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

import janusload
from test_janus import janus, project

FAR = '\n[hosts.far]\nssh = "u@far"\nroot = "/r"\nmemory_max = "3G"\n[models.oc]\nprovider = "api"\nkind = "opencode"\nmodel = "openrouter/x:free"\nargs = ["--standalone"]\n'


class OpencodeRunsOneProcessPerTurn(unittest.TestCase):
    """Items 1, 3, 5, 6, 7: janus started `opencode run <brief>` (one turn, then exit) as the only process of a tmux session and read
    it as Claude Code: the session died with each turn (resume could not find an opencode session id), the brief sat pasted and
    unsent, a working pane read idle (no 'esc to interrupt' footer), and resumes landed as drafts."""

    def test_a_turn_runs_on_janus_own_session_with_its_message_on_the_command_line(self):
        """Catches: no session id recorded, the brief pasted into a TUI, the agent's scope stopped whole by one OOM kill (item 2)."""
        d, cfg = project(FAR); h = cfg.hosts["far"]; mc = cfg.models["oc"]
        sid = janus.turn_session("free-bakeoff-particle-r2")
        self.assertTrue(sid.startswith("ses_janus_free_bakeoff_particle_r2_"), sid)   # opencode 2.4 refuses an id not starting with 'ses'
        inner = janus.turn_command(cfg, h, mc, "/r/worktrees/u", {"JANUS_UNIT": "u"}, sid, "/r/run/u.msg")
        self.assertIn(f"opencode run --standalone --model openrouter/x:free --session {sid} \"$(cat /r/run/u.msg)\"", inner)
        self.assertIn("-p MemoryMax=3G -p OOMPolicy=continue --", inner)
        cfg.models["oc"]["memory_max"] = "6G"
        self.assertIn("MemoryMax=6G", janus.turn_command(cfg, h, cfg.models["oc"], "/w", {}, sid, "/m"))   # [models.<m>] memory_max wins

    def start(self, dead="0 ", pid="4242"):
        d, cfg = project(FAR); h = cfg.hosts["far"]; x = []; r = []
        def X(cfg, h, s, **k): x.append((s, k.get("inp"))); return (0, pid + "\n", "")
        def hrun(h, s, **k): r.append(s); return (0, dead, "") if "pane_dead" in s else (0, "boom: bad model\n", "")
        with mock.patch.object(janus, "X", X), mock.patch.object(janus, "hrun", hrun), mock.patch("time.sleep", lambda s: None):
            try: return x, r, janus.start_turn(cfg, h, "janus-u@t", "cd /w && exec opencode run", "the brief", "/r/run/u.msg")
            except janus.Err as e: return x, r, e

    def test_a_turn_keeps_its_pane_and_never_pastes(self):
        """Catches: the session ending with the turn (no remain-on-exit) and the brief pasted instead of passed."""
        x, r, pid = self.start()
        self.assertEqual(pid, 4242)
        self.assertEqual(x[0][1], "the brief"); self.assertIn("/r/run/u.msg", x[0][0])                  # the message file, by stdin
        s = x[1][0]
        self.assertIn("set-option -w -t '=janus-u@t:' remain-on-exit on", s); self.assertIn("respawn-pane -k -t '=janus-u@t:'", s)
        self.assertNotIn("paste-buffer", " ".join(c for c, _ in x) + " ".join(r))
        self.assertEqual(self.start(dead="1 0")[2], 4242)                                                  # a short turn already over: fine
        e = self.start(dead="1 2")[2]
        self.assertIsInstance(e, janus.Err); self.assertIn("exited at once (status 2)", str(e)); self.assertIn("boom", str(e))

    def test_its_state_is_whether_its_process_lives(self):
        """Catches: a working opencode pane read idle (item 6); a turn's end read as a lost agent (item 3)."""
        out = "avail_mb=1\nreport=u1 1700000500\n--pueue\n{}\n--tmux\njanus-a@t\njanus-b@t dead 1700000000\njanus-c@t busy\n"
        with mock.patch.object(janus, "hrun", lambda *a, **k: (0, out, "")):
            p = janus.probe("far", {"ssh": "u@far", "_rd": "/r"})
        self.assertEqual(p["tmux_dead"], {"janus-b@t": "1700000000"}); self.assertEqual(p["reports"], {"u1": "1700000500"})
        st = lambda t, **k: janus.unit_state({"via": "tmux", "tmux": t, "host": "far", **k}, {"far": p}, {})
        self.assertEqual(st("janus-a@t", turns=True), "working")      # no footer to read: its process lives
        self.assertEqual(st("janus-a@t"), "idle")                     # a Claude pane still reads its footer
        u = {"via": "tmux", "tmux": "janus-b@t", "host": "far", "turns": True}
        self.assertEqual(janus.unit_state(u, {"far": p}, {}), "idle"); self.assertEqual(u["_turn_end"], "1700000000")
        self.assertEqual(st("janus-z@t", turns=True), "gone")

    def test_the_probe_reads_a_dead_pane_on_a_real_tmux(self):
        """Catches: the probe loop never run against tmux (its targets are exact: '=name:' is a window, '=name' is not)."""
        if subprocess.run(["sh", "-c", "command -v tmux"], capture_output=True).returncode: self.skipTest("no tmux")
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            sock = str(Path(d, "s")); tm = lambda *a: subprocess.run(["tmux", "-S", sock, *a], capture_output=True, text=True)
            tm("new", "-d", "-s", "x", "-x", "80", "-y", "20"); tm("set-option", "-w", "-t", "=x:", "remain-on-exit", "on"); tm("respawn-pane", "-k", "-t", "=x:", "exit 3")
            tm("new", "-d", "-s", "y", "-x", "80", "-y", "20", "sleep 30")
            time.sleep(0.5)
            try:
                loop = janus.PROBE.split('echo "--tmux"; ')[1].rsplit("; true", 1)[0].replace("tmux ", f"tmux -S {sock} ")
                o = subprocess.run(["sh", "-c", loop], capture_output=True, text=True).stdout
            finally: tm("kill-server")
        L = dict((l.split(" ", 1) + [""])[:2] for l in o.splitlines())
        self.assertEqual(L["y"], ""); self.assertRegex(L["x"], r"^dead \d{9,}$")

    def resume(self, running, u=None):
        d, cfg = project(FAR); x = []; r = []
        u = u or {"name": "u", "host": "far", "via": "tmux", "tmux": "janus-u@t", "model": "oc", "turns": True, "session": "ses_janus_u_1", "rcwd": "/r/worktrees/u"}
        def X(cfg, h, s, **k): x.append((s, k.get("inp"))); return (0, "77\n", "")
        def hrun(h, s, **k):
            r.append(s)
            if "'#{pane_dead}'" in s: return (0, "0\n" if running else "1\n", "")
            if "pane_dead_status" in s: return (0, "1 0", "")
            return (0, "VERDICT: PENDING\n", "") if s.startswith("cat ") else (0, "", "")
        with mock.patch.object(janus, "X", X), mock.patch.object(janus, "hrun", hrun), mock.patch.object(janus, "route_ok", lambda *a: (True, "")), \
             mock.patch.object(janus, "agent_res", lambda *a: None), mock.patch("time.sleep", lambda s: None):
            try: return x, r, u, janus.resume_unit(cfg, u, "your jobs finished")
            except janus.Err as e: return x, r, u, e

    def test_a_resume_is_a_new_turn_on_the_recorded_session(self):
        """Catches (items 1, 7): 'its opencode session id cannot be read here'; a resume pasted as a draft into a TUI."""
        x, r, u, res = self.resume(running=False)
        self.assertIn("a new turn on session ses_janus_u_1", res); self.assertEqual(u["pid"], 77)
        start = next(s for s, _ in x if "respawn-pane" in s)
        self.assertIn("--session ses_janus_u_1", start); self.assertIn("JANUS_REPORT=/r/out/u/REPORT.md", start)
        self.assertTrue(any(i and "your jobs finished" in i for _, i in x))                    # the message, through its file
        self.assertNotIn("paste-buffer", " ".join(s for s, _ in x))

    def test_a_resume_never_kills_a_running_turn(self):
        """Catches: respawn-pane -k ending a turn that still runs (the watcher or a manual resume while it works)."""
        x, r, u, e = self.resume(running=True)
        self.assertIsInstance(e, janus.Err); self.assertIn("still running", str(e))
        self.assertFalse(any("respawn-pane" in s for s, _ in x))

    def test_a_unit_launched_before_session_ids_is_named_for_relaunch(self):
        x, r, u, e = self.resume(running=False, u={"name": "u", "host": "far", "via": "tmux", "tmux": "janus-u@t", "model": "oc", "rcwd": "/w"})
        self.assertIsInstance(e, janus.Err); self.assertIn("--resume-of u", str(e))


class EachTurnEndIsBroughtHere(unittest.TestCase):
    """Items 8 and 15: a tmux unit's output was brought here once ('tmux' never changes), so a later turn's WAITING or DONE stayed on
    its host and the local report read PENDING (or 11 lines behind) for backup_min."""

    def test_a_new_turn_end_or_a_newer_report_is_due(self):
        d, cfg = project(FAR); t = int(time.time())
        u = {"name": "u", "host": "far", "via": "tmux", "tmux": "t", "state": "idle", "saved": "tmux:100", "saved_at": t}
        self.assertFalse(janus.refresh_due(cfg, {**u, "_turn_end": "100"}))
        self.assertTrue(janus.refresh_due(cfg, {**u, "_turn_end": "200"}))
        self.assertTrue(janus.refresh_due(cfg, {**u, "state": "working", "_report_mtime": t + 60}))   # written on its host since

    def test_a_record_without_a_host_is_never_due(self):
        """Catches item 11: KeyError 'host' in refresh_due while a launch had created the issue but not yet written its record."""
        d, cfg = project(FAR)
        self.assertFalse(janus.refresh_due(cfg, {"name": "u1", "id": "x"}))
        self.assertEqual(janus.fetch_remote_outputs(cfg, [{"name": "u1", "id": "x", "state": "idle"}]), {})


class EndedWithoutAVerdict(unittest.TestCase):
    """Items 3, 4, 8: an agent that ended with its report PENDING read 'finished'; one that had committed read 'gone' alike."""

    def test_a_waiting_line_in_any_case_parks(self):
        d, cfg = project(FAR)
        for t in ("Waiting on far:3", "waiting: far:3", "- **WAITING** far:3"):
            self.assertEqual(janus.park_jobs(cfg, f"VERDICT: PENDING\nplan\n{t}\n")[1], ["far:3"], t)
        self.assertEqual(janus.park_jobs(cfg, "VERDICT: PENDING\nwaiting for a review\n")[1], [])   # prose without a job id: no park

    def test_an_ended_pending_unit_is_not_finished_and_names_its_commits(self):
        d, cfg = project(FAR); rep = d / ".janus/out/u/REPORT.md"; rep.parent.mkdir(parents=True); rep.write_text("VERDICT: PENDING\nplan\n- [ ] step\n")
        u = {"name": "u", "host": "far", "via": "tmux", "tmux": "t", "model": "oc", "started": time.time() - 60, "commits_since": 2}
        hs = {"far": {"ok": True, "tmux": [], "pueue": {"tasks": {}}}}
        F, _ = janus.unit_findings(cfg, hs, [u], {})
        keys = [k for k, _ in F]
        self.assertIn("unit:u:ended-pending", keys); self.assertNotIn("unit:u:finished", keys)
        self.assertIn("2 commit(s) on its host since launch", dict(F)["unit:u:ended-pending"])


class LaunchAndRouting(unittest.TestCase):
    """Items 9, 10, 14."""
    CONF = ('\n[providers.off]\nenabled = false\n'   # project() has [providers.api] and [models.m]
            '[models.m2]\nprovider = "api"\nkind = "api"\napi_id = "y"\n'
            '[models.dis]\nprovider = "off"\nkind = "claude"\n[routing]\nnormal = ["dis", "m"]\nreview = ["m"]\n')

    def test_a_briefs_model_line_is_used_or_refused_never_replaced(self):
        """Catches item 9: `MODEL: ernie` in a brief was never read, so launch without --model started the routed default."""
        d, cfg = project(self.CONF); b = d / ".janus/briefs/u1.md"
        b.write_text("ITEM: X1\nMODEL: m2\nbody\n"); self.assertEqual(janus.brief_model(cfg, b), "m2")
        b.write_text("ITEM: X1\nbody\n"); self.assertIsNone(janus.brief_model(cfg, b))
        b.write_text("MODEL: nope\n")
        with self.assertRaises(janus.Err) as e: janus.brief_model(cfg, b)
        self.assertIn("[models.nope]", str(e.exception))
        b.write_text("MODEL: dis\n")
        with self.assertRaises(janus.Err) as e: janus.brief_model(cfg, b)
        self.assertIn("disabled", str(e.exception))
        b.write_text("ITEM: X1\nMODEL: m2\nbody\n"); cfg.run.dry = True
        a = argparse.Namespace(unit="u1", kind="implement", tier="normal", host="here", model=None, brief=str(b), cwd=None, budget=None, test_host=None,
                               allow_main=True, of=None, report=None, resume_of=None, item=None, probes={"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "100", "load": "0"}})
        with mock.patch.object(janus, "Beads", type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [], "run": lambda s, *a: [], "create": lambda s, *a: "x", "save": lambda s, u: None})), \
             mock.patch.object(janus, "herdr", lambda *a, **k: None), contextlib.redirect_stdout(io.StringIO()):
            try: janus.cmd_launch(cfg, a)
            except janus.Err: pass
        self.assertEqual(a.model, "m2")

    def test_a_review_never_takes_an_open_units_name(self):
        """Catches item 9: a --kind review under an open unit's name passed the duplicate check (two rows for one unit)."""
        d, cfg = project(self.CONF); b = d / ".janus/briefs/u1.md"; b.write_text("ITEM: X1\nbody\n")
        a = argparse.Namespace(unit="u1", kind="review", tier="normal", host=None, model="m", brief=str(b), cwd=None, budget=None, test_host=None,
                               allow_main=True, of=None, report=None, resume_of=None, item=None)
        with mock.patch.object(janus, "Beads", type("B", (), {"__init__": lambda s, c: None, "units": lambda s: [{"name": "u1", "id": "i", "host": "here"}], "run": lambda s, *a: []})):
            with self.assertRaises(janus.Err) as e: janus.cmd_launch(cfg, a)
        self.assertIn("already launched", str(e.exception)); self.assertIn("--kind review --of u1", str(e.exception))

    def test_a_routing_list_that_cannot_launch_is_named(self):
        """Catches item 10: [routing] normal led with a disabled provider and no fallback: fill stopped with 'no model available'
        while nothing in check or doctor said the config disabled its own route."""
        d, cfg = project(self.CONF)
        self.assertEqual(janus.route_unusable(cfg), [("normal", "dis", "provider off disabled")])
        F = dict(janus.orchestrator_findings(cfg)); self.assertIn("routing:normal", F); self.assertIn("put an enabled model first", F["routing:normal"])
        self.assertEqual(janus.family("routing:normal"), "routing")
        cfg.route["fallback"] = True; self.assertEqual(janus.route_unusable(cfg), [])

    def test_a_binary_built_for_another_os_is_not_copied(self):
        """Catches item 14: a Mac-built .firmware-tools/bin/elf2uf2-rs synced into a rigpc worktree."""
        d, cfg = project('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n')
        with tempfile.TemporaryDirectory() as t:
            for rel, head in ((".firmware-tools/bin/elf2uf2-rs", b"\xcf\xfa\xed\xfe...."), ("tools/linux-tool", b"\x7fELF...."), ("run.sh", b"#!/bin/sh\n"), ("data.bin", b"\xcf\xfa\xed\xfe")):
                f = Path(t, rel); f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(head); f.chmod(0o755 if rel != "data.bin" else 0o644)
            cfg.jput("hosts-facts.json", {"far": {"os": "linux"}})
            self.assertEqual(janus.foreign_excludes(cfg, "far", cfg.hosts["far"], t), ["--exclude", "/.firmware-tools/bin/elf2uf2-rs"])
            cfg.jput("hosts-facts.json", {"far": {"os": "mac"}})
            self.assertEqual(janus.foreign_excludes(cfg, "far", cfg.hosts["far"], t), ["--exclude", "/tools/linux-tool"])
            cfg.jput("hosts-facts.json", {})
            self.assertEqual(janus.foreign_excludes(cfg, "far", cfg.hosts["far"], t), [])   # unknown: nothing left out


class Jobs(unittest.TestCase):
    """Items 12, 13, 25."""
    def ns(self, **k): return argparse.Namespace(**{"lane": "jobs", "subdir": None, "usb": False, "wt": "/w/t", "broad": None, **k})

    def test_usb_jobs_take_the_lock_on_every_runner(self):
        """Catches item 13: --usb took usb_lock only on a podman host; rigpc (no container) ran its Pico jobs together."""
        d, cfg = project('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n')
        plain = {"ssh": "u@far", "root": "/r", "_rd": "/r/runs"}
        for h in (plain, {**plain, "usb_lock": "/tmp/pico.lock"}, {"local": True, "_rd": str(d / "rd")}):
            js = janus.job_script(cfg, "far", h, self.ns(usb=True), "t", "make flash", "t-1")
            self.assertIn("flock 8", js, h); self.assertLess(js.index("flock 8"), js.index("cap.sh"), "the lock is taken after admission")
            self.assertNotIn("flock 8", janus.job_script(cfg, "far", h, self.ns(), "t", "make", "t-1"))
        self.assertIn('exec 8>"$RD/usb.lock"', janus.job_script(cfg, "far", plain, self.ns(usb=True), "t", "x", "t-1"))
        self.assertIn("exec 8>/tmp/pico.lock", janus.job_script(cfg, "far", {**plain, "usb_lock": "/tmp/pico.lock"}, self.ns(usb=True), "t", "x", "t-1"))
        with mock.patch.object(janus, "cap_nums", lambda *a: {"agmb": 1024, "cores": 4, "cpj": 1}):
            _, conf = janus.lane_runner(cfg, "far", {**plain, "_rd": "/r/runs"}, "u", "/r/worktrees/u")
        c = json.loads(conf); self.assertIn("flock 8", c["script_usb"]); self.assertNotIn("flock 8", c["script"]); self.assertIn("script_usb_broad", c)

    def test_a_remote_units_tree_is_addressed_by_host_and_path(self):
        """Catches item 12: `janus run --wt` from the Mac could not name a rigpc path (it had to exist here, and was synced over)."""
        d, cfg = project('\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n')
        units = [{"name": "other", "host": "far", "rcwd": "/r/worktrees/other"}, {"name": "u", "host": "far", "rcwd": "/r/worktrees/u"}]
        B = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: units})
        with mock.patch.object(janus, "Beads", B):
            a = self.ns(wt="far:/r/worktrees/u", host=None); self.assertEqual(janus.remote_tree(cfg, a, [], "u"), "/r/worktrees/u"); self.assertEqual(a.host, "far")
            self.assertIsNone(janus.remote_tree(cfg, self.ns(wt="/local/tree", host=None), [], None))
            with self.assertRaises(janus.Err) as e: janus.remote_tree(cfg, self.ns(wt="far:/r/worktrees/other", host=None), [], "u")
            self.assertIn("--unit other", str(e.exception))
            with self.assertRaises(janus.Err): janus.remote_tree(cfg, self.ns(wt="far:/r/x", host="here"), [], None)
        js = janus.job_script(cfg, "far", {"ssh": "u@far", "root": "/r", "_rd": "/r/runs"}, self.ns(wt="far:/r/worktrees/u"), "u", "make", "u-1", wd="/r/worktrees/u")
        self.assertIn("cd /r/worktrees/u &&", js)

    def test_a_stale_wait_file_is_neither_waiting_nor_old_rule_and_tidy_takes_it(self):
        """Catches item 25: a 2026-11-15 job's .wait outlived its task; its pid was reused, so it read as a job waiting under the old
        rule ([old-rule:mac]) once tidy took its .sh, tidy never removed it, and cancel said only 'no task 689 on mac'."""
        other = subprocess.Popen(["sh", "-c", "sleep 30; :", "unrelated"])
        try:
            with tempfile.TemporaryDirectory() as rd:
                Path(rd, "cargo_clean-1115.wait").write_text(f"{other.pid} 60 1700000000 500 1\n")   # a live pid that is not the job
                out = subprocess.run(["sh", "-c", janus.probe_extra({"_rd": rd, "local": True})], capture_output=True, text=True).stdout
                self.assertIn("wait_jobs=0\n", out); self.assertIn("old_rule=0\n", out)
                d, cfg = project(); h = {**cfg.hosts["here"], "_rd": rd}
                with mock.patch.object(janus, "all_units", lambda cfg: {}):
                    P = janus.tidy_host(cfg, "here", h, [], {})
                self.assertIn(("stale-wait", f"here:{rd}/cargo_clean-1115.wait"), [(k, t) for k, t, _, _ in P])
        finally: other.kill(); other.wait()

    def test_records_of_a_gone_task_are_tidied_and_cancel_clears_them(self):
        d, cfg = project(); b = cfg.f("runs") / "busy"; b.mkdir(parents=True); j = cfg.f("runs") / "jobs"; j.mkdir()
        for jid, ref in (("old-1115", "here:689"), ("live-1125", "here:7")):
            (b / jid).write_text(f"{ref}\t/w/t\n"); (j / f"{jid}.json").write_text("{}")
        with mock.patch.object(janus, "hrun", lambda h, s, **k: (0, json.dumps({"tasks": {"7": {"status": "Queued"}}}), "")):
            self.assertEqual([t for k, t, _, _ in janus.tidy_job_records(cfg)], ["old-1115"])
        for answer in ("{}", '{"tasks":null}', "invalid"):
            with mock.patch.object(janus, "hrun", return_value=(0, answer, "")):
                self.assertEqual(janus.tidy_job_records(cfg), [])
                with self.assertRaisesRegex(janus.Err, "unavailable"): janus.cancel_job(cfg, "here:689")
                self.assertTrue((b / "old-1115").exists())
        cmds = []
        with mock.patch.object(janus, "task_status", lambda h, t: {}), mock.patch.object(janus, "hrun", lambda h, s, **k: cmds.append(s) or (0, "", "")):
            out = janus.cancel_job(cfg, "here:689")
            self.assertIn("removed what janus kept", out[0]); self.assertFalse((b / "old-1115").exists()); self.assertTrue((b / "live-1125").exists())
            self.assertTrue(any("old-1115.wait" in c for c in cmds))
            with self.assertRaises(janus.Err): janus.cancel_job(cfg, "here:999")


from test_dispatch import LandingBase, out


class CollectCloseLandBacklog(LandingBase):
    """Items 15-20."""

    def test_the_report_is_brought_here_when_the_bulk_copy_times_out(self):
        """Catches item 15: a 17-byte placeholder REPORT.md stayed here while the host held the full report (the one rsync of a
        bulky out/<unit>/ timed out)."""
        cfg = self.cfg; cfg.hosts["far"] = {"ssh": "u@far", "root": "/r"}; calls = []
        def sh(cmd, **k):
            calls.append(cmd); return (0, "", "") if any(str(x).endswith("REPORT.md") for x in cmd) else (124, "", "timeout")
        with mock.patch.object(janus, "sh", sh):
            u = {"name": "u", "host": "far", "via": "tmux"}
            self.assertFalse(janus.save_output(cfg, u))
        self.assertIn("u@far:/r/out/u/REPORT.md", calls[0]); self.assertIn("u@far:/r/out/u/report.json", calls[0])
        self.assertIn("timed out after 600 s", u["_sync_error"]); self.assertIn("its REPORT.md was brought here", u["_sync_error"])

    def test_force_close_of_a_root_unit_is_allowed_and_a_beads_failure_says_so(self):
        """Item 16: 'project root is shared' came from a release before 0a1fb74; today a unit in the project root may be force-closed,
        and a hook that cannot read beads says that, not the owner's rule."""
        root = {"id": "r1", "name": "a87", "cwd": str(self.cfg.root)}
        self.assertIsNone(janus.force_close_refusal(self.cfg, root, [root]))
        B = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: (_ for _ in ()).throw(janus.Err("bd list: bd: timeout"))})
        with mock.patch.object(janus, "Beads", B):
            why = janus.force_close_why(self.cfg, ["close", "a87", "--force"])
        self.assertIn("beads could not be read", why); self.assertNotIn("shared", why)

    def test_an_owner_accepted_item_needs_only_a_published_commit(self):
        """Catches item 17: A80, accepted by the owner, could not be set DONE: its last unit had no land record, janus land had no
        worktree, and --commit needed a sha the unit's report named."""
        self.w.issue("X1"); sha = git(self.wt, "rev-parse", "HEAD"); git(self.wt, "push", "-q", "origin", "HEAD:main")
        rep = self.d / ".janus/out/u/REPORT.md"; rep.parent.mkdir(parents=True, exist_ok=True); rep.write_text("VERDICT: DONE\n")
        A = lambda **k: type("A", (), {"action": "set", "args": ["X1", "status=DONE"], "unit": "u", "commit": None, "owner_accepted": None, **k})()
        with self.assertRaises(janus.Err) as e: out(janus.cmd_backlog, self.cfg, A(commit=sha[:9]))
        self.assertIn("--owner-accepted", str(e.exception))
        with self.assertRaises(janus.Err) as e: out(janus.cmd_backlog, self.cfg, A(owner_accepted="owner 11-24"))
        self.assertIn("needs --commit", str(e.exception))
        out(janus.cmd_backlog, self.cfg, A(commit=sha[:9], owner_accepted="owner accepted A80 on 2026-11-24"))
        self.assertIn(f"| X1 | one | DONE | {sha[:12]} |", (self.d / "S.md").read_text())
        self.assertIn("owner accepted: owner accepted A80", [x for x in self.w.writes if x[0] == "close"][0][3])
        with self.assertRaises(janus.Err) as e: out(janus.cmd_backlog, self.cfg, A(commit="f" * 12, owner_accepted="x"))   # still verified on the landing branch
        self.assertRegex(str(e.exception), "not on origin/main|cannot be verified")

    def test_acceptance_is_the_orchestrators_and_a_bulk_import_is_said_first(self):
        """Catches item 18: ten checklist rows closed as DONE in beads (close reason 'checklist: DONE (unit <x>, <sha>)', the acceptance's
        own) by units whose agents ran janus backlog set; and an import that closes many items at once did so silently."""
        self.w.issue("X1")
        with mock.patch.dict(os.environ, {"JANUS_ROLE": "agent"}):
            with self.assertRaises(janus.Err) as e: out(janus.cmd_backlog, self.cfg, type("A", (), {"action": "set", "args": ["X1", "status=DONE"], "unit": "u", "commit": None})())
        self.assertIn("orchestrator's", str(e.exception))
        self.w.issue("X2"); (self.d / "S.md").write_text((self.d / "S.md").read_text().replace("| X1 | one | READY |", "| X1 | one | DONE |").replace("| X2 | two | READY |", "| X2 | two | DONE |"))
        with self.assertRaises(janus.Err) as e: out(janus.cmd_backlog, self.cfg, type("A", (), {"action": "import", "args": [], "yes": False})())
        self.assertIn("2 open items done: X1, X2", str(e.exception)); self.assertEqual([x for x in self.w.writes if x[0] == "close"], [])
        out(janus.cmd_backlog, self.cfg, type("A", (), {"action": "import", "args": [], "yes": True})())
        self.assertEqual(sorted(x[1] for x in self.w.writes if x[0] == "close"), ["b-X1", "b-X2"])

    def test_one_item_without_a_row_does_not_skip_the_others(self):
        """Catches item 19: the writeback stopped at the first item without a row (cp12, cc-02), so every item after it got no evidence."""
        rep = self.d / ".janus/out/u/REPORT.md"; rep.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(janus, "unit_items", lambda cfg, us, *a: {"cc-02", "x1"}):
            rc, txt = self.land()
        self.assertEqual(rc, 0, txt); tip = self.u["published"]["commit"]
        self.assertIn(f"| X1 | one | READY | {tip[:12]} |", (self.d / "S.md").read_text())
        self.assertEqual(self.u["published"]["writeback"], "done"); self.assertIn("no row of cc-02", txt)

    def test_an_interrupted_close_can_be_undone_until_its_cleanup_began(self):
        """Catches item 20: a unit stuck 'closing' (its close died) with a PENDING verdict and unlanded changes: resume and collect
        refused it, a second close refused the same checks, and nothing cleared the mark."""
        dead = {"op": "close", "pid": 999999999, "at": 1}
        self.u.update(closing=True, busy=dead)
        _, txt = out(janus.cmd_close, self.cfg, type("A", (), {"unit": "u", "force": False, "reason": None, "abort": True})())
        self.assertIn("undone", txt)
        self.u.update(closing=True, busy=dead, removing=True)
        with self.assertRaises(janus.Err) as e: out(janus.cmd_close, self.cfg, type("A", (), {"unit": "u", "force": False, "reason": None, "abort": True})())
        self.assertIn("close u again", str(e.exception))


def git(c, *a):
    return subprocess.run(["git", "-C", str(c), *a], capture_output=True, text=True, check=True).stdout.strip()


class OrphansAndTheHook(unittest.TestCase):
    """Items 21, 22, 23."""

    def test_a_session_no_open_unit_records_is_an_orphan_tidy_and_stop_can_end(self):
        """Catches item 21: tidy stopped only sessions of closed units recorded on that host; one with no record (a failed launch)
        or a record elsewhere held its reservation for days, janus stop said 'no open unit' and the hook refuses tmux kill."""
        cfg = self.cfg; pid = janus.tmux_name(cfg, "x").rpartition("@")[2]
        units = [{"name": "live", "tmux": f"janus-live@{pid}"}, {"name": "starting"}]
        o = lambda s: janus.orphan_session(cfg, s, units, {"old": {"status": "closed"}})
        self.assertEqual(o(f"janus-cc20-s2-board@{pid}"), "no open unit records it")
        self.assertEqual(o(f"janus-old@{pid}"), "its unit is closed")
        for keep in (f"janus-live@{pid}", f"janus-starting@{pid}", "janus-x@otherproject", "janus-nopid", "mine"): self.assertIsNone(o(keep), keep)
        ran = []
        B = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: units})
        with mock.patch.object(janus, "Beads", B), mock.patch.object(janus, "all_units", lambda cfg: {}), \
             mock.patch.object(janus, "hrun", lambda h, c, **k: ran.append(c) or (0, "", "")), contextlib.redirect_stdout(io.StringIO()):
            janus.cmd_stop(cfg, argparse.Namespace(unit=f"here:janus-cc20-s2-board@{pid}", force=False, orphan=True))
            with self.assertRaises(janus.Err): janus.cmd_stop(cfg, argparse.Namespace(unit=f"here:janus-live@{pid}", force=False, orphan=True))
        self.assertEqual(ran, [f'PATH="$HOME/.local/bin::$PATH"; tmux kill-session -t \'=janus-cc20-s2-board@{pid}\''])

    def setUp(self): self.d, self.cfg = project()

    def why(self, c, tool="Bash"):
        ti = {"command": c} if tool == "Bash" else {"file_path": c, "content": "x"}
        return janus.pretool_why(self.cfg, {"tool_name": tool, "tool_input": ti, "cwd": str(self.cfg.root)})

    def test_bd_with_global_flags_is_judged_by_its_subcommand(self):
        """Catches item 22: `bd -C .janus close <ids>` was refused as 'bd .janus by hand' (the -C value read as the subcommand)."""
        self.assertIsNone(self.why("bd -C .janus close x --reason stale"))
        self.assertIsNone(self.why("bd --db /x/beads.db list"))
        self.assertIn("bd delete", self.why("bd -C .janus delete x"))
        self.assertIn("janus bead close", janus.deny_alt(self.why("bd -C .janus delete x")))

    def test_bead_close_takes_only_beads_that_are_neither_units_nor_items(self):
        cfg = self.cfg; w = []
        rec = {"s1": {"id": "s1", "title": "sweep-backlog-b32", "labels": []}, "u1": {"id": "u1", "title": "unit:x", "labels": ["unit"]},
               "b1": {"id": "b1", "title": "backlog: A80", "labels": ["backlog"], "external_ref": "backlog:A80"}}
        B = type("B", (), {"__init__": lambda s, c: None, "run": lambda s, *a: w.append(a) or ([rec[a[1]]] if a[0] == "show" else {})})
        with mock.patch.object(janus, "Beads", B), contextlib.redirect_stdout(io.StringIO()):
            janus.cmd_bead(cfg, argparse.Namespace(action="close", ids=["s1"], reason="stale sweep"))
            self.assertIn(("close", "s1", "--reason", "stale sweep"), w)
            for i, hint in (("u1", "janus close x"), ("b1", "janus backlog set")):
                with self.assertRaises(janus.Err) as e: janus.cmd_bead(cfg, argparse.Namespace(action="close", ids=[i], reason="r"))
                self.assertIn(hint, str(e.exception))

    def test_in_place_edits_are_judged_by_their_files_as_edit_is(self):
        """Catches item 23: sed -i / perl -i were refused whatever they edited, though Edit may change the same brief."""
        self.assertIsNone(self.why("sed -i '' 's/a/b/' .janus/briefs/x.md"))
        self.assertIsNone(self.why("perl -0pi -e 's/a/b/' .janus/briefs/x.md"))
        self.assertIsNone(self.why("sed -i.bak -e 's/a/b/' .janus/janus.toml"))
        self.assertEqual(self.why("sed -i 's/a/b/' src/x.py"), "in-place edit")
        self.assertEqual(self.why("sed -i '' 's/a/b/' .janus/briefs/x.md src/x.py"), "in-place edit")   # every file must be allowed
        self.assertEqual(self.why("perl -pi -e 's/a/b/' /etc/hosts"), "in-place edit")

    def test_the_orchestrators_own_claude_memory_is_writable_and_nothing_else_there(self):
        """Catches item 23: Write to ~/.claude/projects/<project>/memory/ was refused while a shell redirect there was allowed."""
        mem = Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(self.cfg.root.resolve())) / "memory" / "note.md"
        self.assertIsNone(self.why(str(mem), tool="Write"))
        self.assertIsNotNone(self.why(str(Path.home() / ".claude/settings.json"), tool="Write"))
        self.assertIsNotNone(self.why(str(Path.home() / ".claude/projects/-other-project/memory/note.md"), tool="Write"))


class Tidy(unittest.TestCase):
    """Items 24, 26, 27."""

    def test_a_bulky_folder_of_many_small_files_goes_whole(self):
        """Catches item 24: a cargo target dir, an extracted tarball and repository snapshots (3.7 GB, each file under 50 MB) in closed
        units' output were invisible to tidy, while the hook refuses rm -r."""
        d, cfg = project(); od = d / ".janus/out"; old = time.time() - 30 * 86400
        def files(base, n):
            for i in range(n):
                q = base / f"deps/f{i}.rlib"; q.parent.mkdir(parents=True, exist_ok=True)
                with open(q, "wb") as h: h.truncate(1 << 20)
                os.utime(q, (old, old))
        files(od / "gone/target", 60); files(od / "gone/notes", 2); files(od / "live/target", 60); files(od / "keep/snap", 60)
        (od / "keep/snap/REPORT.md").write_text("x"); os.utime(od / "keep/snap/REPORT.md", (old, old))
        with mock.patch.object(janus, "all_units", lambda c: {"gone": {"status": "closed"}, "live": {"status": "closed"}, "keep": {"status": "closed"}}):
            P = janus.tidy_out_bulk(cfg, [{"name": "live"}])
        self.assertEqual(sorted(t for _, t, _, _ in P), [str(od / "gone/target"), str(od / "keep/snap/deps")])   # snap's REPORT.md stays, its bulk goes
        self.assertTrue((od / "keep/snap/REPORT.md").exists())
        # a folder with small results of its own (a run's logs, a checksum list) is opened up: its bulky subfolders go, its files stay
        files(od / "gone/e2e/run-dir", 60); (od / "gone/e2e/01-validate.out").write_text("ok\n"); os.utime(od / "gone/e2e/01-validate.out", (old, old))
        with mock.patch.object(janus, "all_units", lambda c: {"gone": {"status": "closed"}, "live": {"status": "closed"}, "keep": {"status": "closed"}}):
            P = janus.tidy_out_bulk(cfg, [{"name": "live"}])
        self.assertEqual(sorted(t for _, t, _, _ in P), [str(od / "gone/e2e/run-dir"), str(od / "gone/target"), str(od / "keep/snap/deps")])

    def test_a_tree_squash_landed_then_changed_on_main_is_landed(self):
        """Catches item 26: rigpc-hw-session was kept 'unlanded' though its files were byte-identical to its squash commit: the
        comparison ran against main's tip, where a later commit had changed one of them."""
        d, cfg = project('\n[paths]\nworktrees = "wt"\n[landing]\nbranch = "main"\n')
        g = lambda c, *a: subprocess.run(["git", "-C", str(c), *a], capture_output=True, text=True, check=True).stdout.strip()
        r = d / "repo"; r.mkdir(); g(r, "init", "-q", "-b", "main"); (r / "f").write_text("a\n"); g(r, "add", "f"); g(r, "commit", "-qm", "base")
        wt = d / "wt/rigpc-hw-session"; wt.parent.mkdir(); g(r, "worktree", "add", "-q", "-b", "wk/rigpc-hw-session", str(wt))
        (wt / "f").write_text("b\n"); g(wt, "commit", "-qam", "work")
        (r / "f").write_text("b\n"); g(r, "commit", "-qam", "rigpc-hw-session: land (squash)")      # the squash landing
        (r / "f").write_text("c\n"); g(r, "commit", "-qam", "later change to the same file")
        with mock.patch.object(janus, "all_units", lambda c: {}), mock.patch.object(janus, "herdr_agents", lambda c: {}), mock.patch.object(janus, "orphan_tabs", lambda *a: []):
            P = janus.tidy_plan(cfg, [], rows={})
        self.assertIn(("worktree", str(wt)), [(k, t) for k, t, _ in P])
        (wt / "f").write_text("d\n"); g(wt, "commit", "-qam", "more work after the landing")         # not landed: kept
        with mock.patch.object(janus, "all_units", lambda c: {}), mock.patch.object(janus, "herdr_agents", lambda c: {}), mock.patch.object(janus, "orphan_tabs", lambda *a: []):
            self.assertNotIn(str(wt), [t for _, t, _ in janus.tidy_plan(cfg, [], rows={})])

    def test_a_beads_timeout_still_runs_the_rules_that_need_no_unit(self):
        """Catches item 27: 'bd list: bd: timeout' under load made janus tidy exit doing nothing."""
        d, cfg = project(); cfg.run.dry = True
        B = type("B", (), {"__init__": lambda s, c: None, "units": lambda s: (_ for _ in ()).throw(janus.Err("bd list: bd: timeout"))})
        host = "runfiles 3\nstalewait /x/runs/j.wait\ntmux janus-u@" + janus.tmux_name(cfg, "x").rpartition("@")[2] + "\n"
        with mock.patch.object(janus, "Beads", B), mock.patch.object(janus, "hrun", lambda h, c, **k: (0, host, "")), \
             mock.patch.object(janus, "refuse_agent", lambda w: None), contextlib.redirect_stdout(io.StringIO()) as o:
            janus.cmd_tidy(cfg, argparse.Namespace(action=None, target=None, why=None, dry_run=True, run=None))
        t = o.getvalue()
        self.assertIn("beads could not be read", t); self.assertIn("would remove run-files here", t)
        self.assertNotIn("tmux", t.split("the rest runs", 1)[1])                                   # sessions need the open units: never judged blind
        cmds = []
        with mock.patch.object(janus, "sh", lambda cmd, **k: cmds.append(k.get("timeout")) or ((124, "", "bd: timeout") if len(cmds) == 1 else (0, "[]", ""))), \
             mock.patch("time.sleep", lambda s: None):
            self.assertEqual(janus.Beads(cfg).run("list"), [])
        self.assertEqual(cmds, [None, 90])                                                           # a read is asked once more, longer


class LedgerAndCodexHooks(unittest.TestCase):
    """Items 28, 29."""

    def test_check_names_the_implausible_run_and_an_accepted_one_leaves_the_finding(self):
        """Catches item 28: [ledger:claude_api:implausible] on every status for one run over twice its budget, while janus ledger
        check said 'valid' (two rules), and no way to record a reviewed overspend in an append-only ledger."""
        d, cfg = project(); pv = cfg.prov["api"]; mc = cfg.models["m"]
        janus.ledger_done(cfg, pv, janus.reserve_budget(cfg, mc, "flir", 2, "flir.r4.1"), "flir", 6.5, 0)
        with mock.patch.object(janus, "all_units", lambda c: {}):
            self.assertIn("ledger:api:implausible", dict(janus.ledger_findings(cfg, [])))
        A = lambda act, **k: argparse.Namespace(**{"action": act, "provider": None, "run": None, "why": None, **k})
        with contextlib.redirect_stdout(io.StringIO()) as o: janus.cmd_ledger(cfg, A("check"))
        self.assertIn("implausible: flir.r4.1 cost $6.50, over twice its budget $2", o.getvalue())
        with self.assertRaises(janus.Err): janus.cmd_ledger(cfg, A("accept", run="flir.r4.1"))          # a why is needed
        with contextlib.redirect_stdout(io.StringIO()): janus.cmd_ledger(cfg, A("accept", run="flir.r4.1", why="owner reviewed: long firmware run"))
        with mock.patch.object(janus, "all_units", lambda c: {}):
            self.assertNotIn("ledger:api:implausible", dict(janus.ledger_findings(cfg, [])))
        with contextlib.redirect_stdout(io.StringIO()) as o: janus.cmd_ledger(cfg, A("check"))
        self.assertNotIn("implausible", o.getvalue())

    def test_janus_hooks_keep_their_place_in_codex_hooks_json(self):
        """Catches item 29: set_hooks moved janus's entries to the end of each event list, so after another tool wrote the file
        every install reordered it; Codex keys trust by event:index and every codex agent stopped at 'N hooks need review'."""
        with tempfile.TemporaryDirectory() as t:
            f = Path(t, "hooks.json"); herdr = {"hooks": [{"type": "command", "command": "herdr hook PreToolUse"}]}
            jan = janus.hooks_json()["hooks"]
            f.write_text(json.dumps({"hooks": {ev: [jan[ev][0], herdr] for ev in jan}}, indent=2) + "\n"); before = f.read_bytes()
            self.assertFalse(janus.set_hooks(f, True, say=lambda *a: None)); self.assertEqual(f.read_bytes(), before)
            stale = {ev: [{"hooks": [{"type": "command", "command": "/old/janus hook " + ev, "timeout": 60}]}, herdr] for ev in jan}
            f.write_text(json.dumps({"hooks": stale}))
            self.assertTrue(janus.set_hooks(f, True, say=lambda *a: None))
            got = json.loads(f.read_text())["hooks"]
            for ev in jan: self.assertEqual(got[ev], [jan[ev][0], herdr], ev)                           # replaced where it stood
            f.write_text(json.dumps({"hooks": {"Stop": [herdr]}})); janus.set_hooks(f, True, say=lambda *a: None)
            self.assertEqual(json.loads(f.read_text())["hooks"]["Stop"][0], herdr)                       # a new entry goes last

    def test_codex_asking_to_review_hooks_fails_the_start_and_is_never_answered(self):
        sent = []
        with mock.patch("time.sleep", lambda s: None):
            why = janus.past_dialogs(lambda: "OpenAI Codex\n 2 hooks need review\n Review now / Later", lambda *k: sent.append(k))
        self.assertIn("/hooks", why); self.assertEqual(sent, [])


class BeadsReadsAreNeverCut(unittest.TestCase):
    def test_open_units_and_ready_work_are_read_without_bds_default_limit(self):
        """Catches (found cleaning up after rig29): bd list shows 50 and bd ready 100 unless told -n 0, so a project with more open
        units than that had units janus could not see (their tmux sessions then read as orphans to tidy) and ready work it never named
        (status counted 46 of 51 sweep beads)."""
        d, cfg = project(); calls = []
        with mock.patch.object(janus, "sh", lambda cmd, **k: calls.append(cmd) or (0, "[]", "")):
            janus.Beads(cfg).units(); cfg.run.reads = None; janus.ready_work(cfg)
        for c in calls: self.assertEqual(c[c.index("-n") + 1], "0", c)


if __name__ == "__main__": unittest.main()
