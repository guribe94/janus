"""rig29: 29 defects the collection-rig orchestrator reported against release 20261124-113728 (opencode agents over tmux on rigpc,
launch and routing, jobs, collect/close/land, the guard hook, tidy, the ledger). One test per failure, each naming what it catches.
Standard library only; no network, no spend. Run by ./check (it discovers tests/test_*.py)."""
import argparse, contextlib, io, json, os, subprocess, sys, tempfile, time, unittest
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


if __name__ == "__main__": unittest.main()
