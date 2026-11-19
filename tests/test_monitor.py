"""janus monitor: the read-only dashboard. Standard library only; no bd, ssh, herdr, curses terminal or network: one fake `sh`
answers bd and the host probes, one fake screen takes the keys. Each test names the failure it catches."""
import hashlib, json, os, re, subprocess, sys, threading, time, unittest
from pathlib import Path
from unittest import mock

from test_janus import janus, project, JANUS_PY

M = sys.modules["janus.monitor"]
FAR = '\n[hosts.far]\nssh = "far"\nroot = "/x"\n'
MUTATING_BD = {"update", "create", "close", "comments", "comment", "remember", "forget", "delete", "sync", "init", "reopen", "label", "dep", "import", "export"}


def probe_text(avail=8192, load="1.50", rj=2, pause="", tasks=None, waits=(1, 0), admit="yes", extra=""):
    return (f"avail_mb={avail}\nload={load}\nos=linux\ndisk_gb=100\nwait_jobs={waits[0]}\nwait_gates={waits[1]}\ncap_rj={rj}\ncap_rc=2\ncap_rm=1024\ncap_ra=0\ncap_ram=0\n"
            f"cap_pause={pause}\ncap_admit={admit}\n{extra}--pueue\n{json.dumps({'tasks': tasks or {}, 'groups': {}})}\n--tmux\n")


def task(i, status, group="agents"): return {"id": i, "group": group, "label": f"t{i}", "status": status}


def unit(name, **kw):
    rec = {"v": 2, "host": "here", "via": "pueue", "task": 1, "model": "m", "started": time.time() - 600, "cwd": f"/w/{name}", "brief": "briefs/x.md", **kw}
    return {"id": f"t-{name}", "title": f"unit:{name}", "status": "in_progress", "notes": json.dumps(rec)}


class World:
    """What bd and the hosts answer, and a record of every command run."""
    def __init__(self, d):
        self.d, self.calls, self.fail, self.block = d, [], set(), None
        self.units, self.closed, self.ready, self.backlog = [], [], [], []
        self.probes = {"here": probe_text(), "far": probe_text(avail=4096, load="0.20", rj=0)}
        self.agents = None
    def sh(self, cmd, cwd=None, inp=None, timeout=25, env=None, clean=False):
        self.calls.append(list(cmd))
        if cmd[0] == "bd":
            if "bd" in self.fail: return 1, "", "bd: dolt server unreachable (token=hunter2hunter2)"
            a = cmd[2:]; kind = a[0]
            out = (self.closed if "closed" in a else self.units if "unit" in a else self.backlog if "backlog" in a else None) if kind == "list" else self.ready if kind == "ready" else None
            return (0, json.dumps(out), "") if out is not None else (1, "", f"unexpected bd {a}")
        host = "here" if cmd[:2] == ["sh", "-c"] else "far" if cmd[0] == "ssh" else None
        if host:
            if self.block and host == "far": self.block.wait(30)
            if host in self.fail: return 255, "", "ssh: connect to host far port 22: Connection timed out"
            return 0, self.probes[host], ""
        return 0, "", ""
    def herdr(self, *a, **k): return None if "herdr" in self.fail else {"agents": self.agents or []}
    def patches(self): return [mock.patch.object(janus, "sh", self.sh), mock.patch.object(janus, "herdr", self.herdr)]
    def report(self, name, text): (self.d / ".janus" / "out" / name).mkdir(parents=True, exist_ok=True); (self.d / ".janus" / "out" / name / "REPORT.md").write_text(text)
    def board(self, text, mode="a"):
        (self.d / ".janus" / "board").mkdir(exist_ok=True)
        with open(self.d / ".janus/board/log.md", mode) as f: f.write(text)


def snapshot(d):
    return {str(p.relative_to(d)): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha1(p.read_bytes()).hexdigest()) for p in sorted(d.rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    extra = FAR
    def setUp(self):
        self.d, self.cfg = project(self.extra); self.w = World(self.d); self.store = M.Store()
        for p in self.w.patches(): p.start(); self.addCleanup(p.stop)
        self.col = M.Collector(self.d, self.store, 5)
    def collect(self): self.col.collect_once()
    def model(self, offset=0):
        v = self.store.view()[0]; return M.derive(v, time.time() + offset, 5)
    def states(self, tab="Active", m=None):
        m = m or self.model(); return {r.cells[0]: (r.state + (" STALE" if r.stale else "")) if r.state else r.cells[1] for r in m.rows[tab]}


class States(Base):
    def test_every_state_keeps_its_meaning(self):
        """Catches: a queued unit shown as running, a parked one as finished, a process that merely exited shown as success, a failed
        process with a DONE report shown as done, a launch that failed shown as active, a closed unit mixed with finished ones."""
        w = self.w
        w.units = [unit("run", task=1), unit("que", task=2), unit("exit0", task=3), unit("good", task=4), unit("bad", task=5), unit("park", task=1, parked={"jobs": ["here:9"], "since": 1}),
                   unit("nolaunch", via="failed"), unit("far1", host="far", task=7), unit("blk", task=6)]
        w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}}), "2": task(2, "Queued"), "3": task(3, {"Done": {"result": "Success"}}),
                                             "4": task(4, {"Done": {"result": "Success"}}), "5": task(5, {"Done": {"result": "Failed"}}), "6": task(6, {"Running": {}})})
        w.probes["far"] = probe_text()   # task 7 is not in far's pueue: gone
        w.report("good", "# r\nVERDICT: DONE\n"); w.report("bad", "VERDICT: DONE\n"); w.report("blk", "x\nVERDICT: BLOCKED\n"); w.report("exit0", "# notes, no verdict yet\nsome notes\n")
        w.closed = [{**unit("old", task=1), "status": "closed", "closed_at": "2026-11-12T10:00:00Z"}]; w.report("old", "VERDICT: DONE\n")
        self.collect(); m = self.model()
        act, fin = self.states("Active", m), self.states("Ended", m)
        self.assertEqual(act, {"run": "working", "que": "queued", "park": "parked", "blk": "working"})   # a live process with a BLOCKED report is still running
        self.assertEqual(fin, {"good": "finished", "exit0": "ended:no-verdict", "bad": "failed", "nolaunch": "failed", "far1": "ended:no-verdict", "old": "closed:DONE"})
        self.assertEqual(m.rows["Ended"][-1].cells[0], "old")   # closed units after those still waiting to be collected
        why = lambda n: " ".join(next(r for r in m.rows["Ended"] if r.key == n).detail)
        self.assertIn("not success", why("exit0")); self.assertIn("not success", why("bad"))
        self.assertEqual(next(r for r in m.rows["Active"] if r.key == "park").detail[2], "Meaning:   waiting for job here:9: no longer in the queue")

    def test_each_state_says_in_plain_words_what_is_happening(self):
        """Catches (owner 2026-11-18): 'parked', 'idle' and 'queued' said nothing about what the unit waits for, how long it has
        waited or whether that is long: the reason sat in the details only. The cell now says it; the state code is unchanged."""
        w = self.w; t0 = int(time.time()) - 840
        w.units = [unit("run", task=1), unit("que", task=2), unit("park", task=1, parked={"jobs": ["here:9"], "since": 1}),
                   unit("hold", task=1, parked={"jobs": ["here:8"], "since": 1}), unit("idl", via="herdr")]
        job = lambda i, j: {**task(i, {"Running": {}}, "jobs"), "command": f"bash /x/runs/{j}.sh"}
        w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}}), "2": task(2, "Queued"), "9": job(9, "j9"), "8": job(8, "j8")}, pause="memory critical",
                                      extra=f"res=4242 1 512 {t0} cargo_test j9.res -\ncls=cargo_test 12 120 1\nwaitjob=j8\n")
        w.agents = [{"name": "idl", "agent_status": "idle", "pane_id": "p1"}]
        self.collect(); m = self.model(); cell = {r.key: r.cells[2] for r in m.rows["Active"]}
        self.assertEqual(self.states("Active", m), {"run": "working", "que": "queued", "park": "parked", "hold": "parked", "idl": "idle"})
        self.assertEqual(cell["run"], "agent working")
        self.assertTrue(cell["que"].startswith("waiting for a free agent slot"), cell["que"])
        self.assertEqual(cell["park"], "waiting for job here:9: running 14m, usually 2m")
        self.assertIn("waiting for job here:8: held by admission (memory critical)", cell["hold"])
        self.assertTrue(cell["idl"].startswith("agent stopped"), cell["idl"])
        self.assertEqual(next(r for r in m.rows["Active"] if r.key == "park").style, "warn")   # 7 times its class's usual time

    def test_a_unit_on_a_down_host_shows_its_last_report_not_unknown(self):
        """Catches: every unit of an unreachable host shown 'unknown' for days although its report, copied here, says DONE."""
        self.w.units = [unit("d", host="far", task=7), unit("n", host="far", task=8)]; self.w.fail.add("far"); self.w.report("d", "x\nVERDICT: DONE (landed)\n")
        self.collect(); self.assertEqual(self.states(), {"d": "host-down:DONE", "n": "unknown"})

    def test_each_task_shows_its_plan_progress_and_the_header_the_checklist_done(self):
        """The owner asked for a progress indicator per task and for total completeness on the monitor."""
        self.assertEqual(M.report_progress("VERDICT: PENDING\n- [x] a\n  - [ ] b\n* [X] c\n- [ ] d\n"), (2, 4))
        self.assertIsNone(M.report_progress("VERDICT: PENDING\nno plan\n"))
        self.w.units = [unit("p", task=1), unit("q", task=2)]
        self.w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}}), "2": task(2, {"Running": {}})})
        self.w.report("p", "VERDICT: PENDING\n- [x] a\n- [x] b\n- [ ] c\n"); self.w.report("q", "VERDICT: PENDING\n")
        self.collect(); m = self.model()
        self.assertEqual({r.cells[0]: r.cells[6] for r in m.rows["Active"]}, {"p": "2/3 66%", "q": "-"})
        m.done = (3, 4); scr = "\n".join("".join(t for t, _ in l) for l in M.render(m, M.UI(), 30, 160, time.time(), ""))
        self.assertIn("done 3/4", scr); self.assertIn("75%", scr); self.assertIn("PROGRESS", scr)

    def test_a_report_not_read_yet_is_unverified_not_no_verdict(self):
        """Catches: a unit's first screen, drawn before its report was read, saying 'no verdict' (or finished) of a report nobody read."""
        self.w.units = [unit("x", task=3)]; self.w.probes["here"] = probe_text(tasks={"3": task(3, {"Done": {"result": "Success"}})}); self.w.report("x", "VERDICT: DONE\n"); self.collect()
        view = {k: v for k, v in self.store.view()[0].items() if k != "reports-open"}
        self.assertEqual({r.cells[0]: r.state for r in M.derive(view, time.time(), 5).rows["Ended"]}, {"x": "ended:unverified"})
        self.assertEqual(self.states("Ended"), {"x": "finished"})

    def test_tool_model_admission_capability_freshness_and_the_handoff_checkpoint(self):
        """Catches: a unit row without its tool beside its model, a host row that hides why it does not admit, capabilities that read
        as 'none' when never discovered (or as fresh when old, or silently when the cache is unreadable), an overview without the
        handoff's current percentage."""
        w = self.w; w.units = [unit("a", model="m")]; w.probes["far"] = probe_text(admit="paused memory", rj=0)
        (self.d / ".janus/hosts-facts.json").write_text(json.dumps({"far": {"at": time.time() - 3 * 3600, "tools": ["claude", "codex", "git"], "logins": ["claude"]}}))
        (self.d / ".janus/HANDOFF.md").write_text("# HANDOFF\n## Objective\nship x\n## Next actions\nCheckpoint: ~60% complete - parser done; tests left; blocker none\n")
        self.collect(); m = self.model(); r = {x.cells[0]: x.cells for x in m.machines}
        self.assertEqual(r["far"][6], "blocked: paused memory"); self.assertEqual(r["here"][6], "admits")
        self.assertTrue(r["far"][7].startswith("claude+ codex ("), r["far"][7])
        self.assertTrue(r["here"][7].startswith("unknown"), r["here"][7])               # never discovered: unknown, not none
        self.assertEqual(m.rows["Overview"][0].cells[0], "Checkpoint"); self.assertIn("~60%", m.rows["Overview"][0].cells[1]); self.assertIn("tests left", m.rows["Overview"][0].cells[1])
        self.assertIn("/", next(x for x in m.rows["Active"] + m.rows["Ended"] if x.key == "a").cells[4])   # tool/model
        (self.d / ".janus/hosts-facts.json").write_text("{not json"); self.collect(); m = self.model()
        self.assertIn("unreadable", {x.cells[0]: x.cells for x in m.machines}["far"][7]); self.assertIn("unreadable", m.rows["Overview"][-1].cells[1])

    def test_machine_row_numbers(self):
        """Catches: wrong running/queued builds, agents, load, RAM, paused or unreachable on the machine table."""
        self.w.units = [unit("a", task=1), unit("b", task=2)]
        self.w.probes["here"] = probe_text(rj=3, load="2.50", avail=2048, tasks={"1": task(1, {"Running": {}}), "2": task(2, {"Running": {}}), "9": task(9, "Queued", "jobs")}, waits=(2, 1))
        self.w.probes["far"] = probe_text(pause="memory pressure 40")
        self.collect(); r = {x.cells[0]: x.cells for x in self.model().machines}
        self.assertEqual(r["here"][:6], ("here", "ok", "3/3", "2", "2.50", "2.0G"))   # queued: one pueue job + 2 waiting in admission
        self.assertEqual((r["far"][1], r["far"][3]), ("PAUSED", "0")); self.assertIn("memory pressure 40", r["far"][8])

    def test_backlog_queue_states(self):
        """Catches: queued backlog work shown as launched, an owner's item shown as ready, ready work missing."""
        w = self.w
        w.ready = [{"id": "p-1", "title": "plain ready work", "labels": []}, {"id": "p-2", "title": "unit:x", "labels": ["unit"]}, {"id": "b-3", "title": "Item three", "labels": ["backlog"]}]
        w.backlog = [{"id": "b-1", "title": "Item one", "labels": ["backlog", "status:todo"], "external_ref": "backlog:one"},
                     {"id": "b-2", "title": "Item two", "labels": ["backlog", "actor:owner"], "external_ref": "backlog:two"},
                     {"id": "b-3", "title": "Item three", "labels": ["backlog"], "external_ref": "backlog:three"},
                     {"id": "b-4", "title": "Item four", "labels": ["backlog"], "external_ref": "backlog:four"}]
        w.units = [unit("u", item="one")]; w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})})
        self.collect()
        self.assertEqual(self.states("Backlog"), {"plain ready work": "ready", "Item one": "open-unit", "Item two": "owner", "Item three": "ready", "Item four": "waiting"})

    def test_every_row_has_one_cell_per_column(self):
        """Catches: a tab whose rows have fewer cells than its columns (the Backlog tab crashed with IndexError once PROGRESS was added)."""
        w = self.w; w.board("2026-11-13 09:00 ann status: started\n"); (self.d / ".janus/HANDOFF.md").write_text("Checkpoint: ~10% done\n")
        w.ready = [{"id": "p-1", "title": "plain ready work", "labels": []}]
        w.backlog = [{"id": "b-1", "title": "Item one", "labels": ["backlog"], "external_ref": "backlog:one"}, {"id": "b-2", "title": "Item two", "labels": ["backlog", "actor:owner"], "external_ref": "backlog:two"}]
        w.units = [unit("u", item="one"), unit("f", host="far", task=2)]; w.closed = [{**unit("old"), "status": "closed", "closed_at": "2026-11-12T10:00:00Z"}]
        w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})}); self.collect(); m = self.model()
        for tab in M.TABS:
            cols = M.TAB_COLS.get(tab, M.WORK_COLS)
            self.assertTrue(m.rows[tab], tab)
            for r in m.rows[tab]: self.assertEqual(len(r.cells), len(cols), (tab, r.key, r.cells))
            for width in (60, 240):
                ui = M.UI(); ui.tab = M.TABS.index(tab); M.render(m, ui, 40, width, time.time(), "")
        for r in m.machines: self.assertEqual(len(r.cells), len(M.HOST_COLS), r.cells)
        self.assertEqual(m.counts["Overview"], len(m.rows["Overview"]))   # it read 0 while its rows showed

    def test_no_shape_size_or_key_crashes_the_screen(self):
        """Catches: an IndexError or the like in render for some data shape (nothing read yet, a down host with stranded units, relaunch,
        held and no-brief items, unreadable sources), terminal size, tab or mode (details, help, a filter that matches or not)."""
        w = self.w; shapes = [("nothing read", self.model())]
        w.board("2026-11-13 09:00 ann status: started\n2026-11-13 09:05 bob question: why?\n"); (self.d / ".janus/HANDOFF.md").write_text("Checkpoint: ~10% done\n")
        w.ready = [{"id": "p-1", "title": "plain ready work", "labels": []}, {"id": "b-1", "title": "Item one", "labels": ["backlog"]}, {"id": "b-4", "title": "Item four", "labels": ["backlog"]}]
        w.backlog = [{"id": "b-1", "title": "Item one", "labels": ["backlog"], "external_ref": "backlog:one"}, {"id": "b-2", "title": "Item two", "labels": ["backlog", "actor:owner"], "external_ref": "backlog:two"},
                     {"id": "b-3", "title": "Item three", "labels": ["backlog", "hold:owner says"], "external_ref": "backlog:three"}, {"id": "b-4", "title": "Item four", "labels": ["backlog"], "external_ref": "backlog:four"}]
        w.units = [unit("u", item="one", task=1), unit("d", host="far", task=7, item="one"), unit("n", host="far", task=8, relaunched="n-r2")]
        w.closed = [{**unit("old"), "status": "closed", "closed_at": "2026-11-12T10:00:00Z"}]; w.report("d", "x\nVERDICT: DONE\n")
        w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})}); self.collect(); shapes.append(("live", self.model()))
        w.fail.add("far"); (self.d / ".janus/hosts.json").write_text(json.dumps({"far": time.time() - 7200})); (self.d / ".janus/hosts-facts.json").write_text("{not json")
        self.collect(); shapes.append(("far down", self.model()))
        K = M.curses; keys = {"list": [], "end": [K.KEY_END], "details": [10], "details scrolled": [10, K.KEY_NPAGE, K.KEY_END], "help": [ord("?")],
                              "filter hit": [ord("/"), ord("i"), 10], "filter miss": [ord("/"), *map(ord, "zzzq"), 10]}
        for name, m in shapes:
            for tab in range(len(M.TABS)):
                for mode, ks in keys.items():
                    for h, wd in ((3, 24), (5, 30), (9, 40), (14, 80), (40, 120), (60, 300)):
                        ui = M.UI(); ui.tab = tab
                        try:
                            for k in ks: ui.key(M.filtered(m, ui.filt), k, h)
                            out = M.render(M.filtered(m, ui.filt), ui, h, wd, time.time(), "status")
                        except Exception as e: self.fail(f"{name}, {M.TABS[tab]}, {mode}, {h}x{wd}: {e!r}")
                        self.assertLessEqual(len(out), h, (name, M.TABS[tab], mode, h, wd))   # paint clips the width


class ItemProgress(Base):
    extra = FAR + '\n[backlog]\nfile = "S.md"\nevidence_col = "evidence"\n'

    def test_which_work_serves_which_item_and_when_an_item_is_finished(self):
        """Catches (owner 2026-11-18): the Active tab never said which backlog item a unit served, the Backlog tab showed an item's
        age instead of its units, and an item whose every unit had landed DONE stayed 'ready' with nothing saying it was done."""
        w = self.w; (self.d / "S.md").write_text("| # | item | status | evidence |\n|---|---|---|---|\n| one | Item one | READY | - |\n| two | Item two | READY | - |\n")
        lab = ["backlog", "backlog-ready"]
        w.backlog = [{"id": "b-1", "title": "Item one", "labels": lab, "status": "open", "external_ref": "backlog:one"},
                     {"id": "b-2", "title": "Item two", "labels": lab, "status": "open", "external_ref": "backlog:two"}]
        w.ready = [{"id": "b-1", "title": "Item one", "labels": lab}, {"id": "b-2", "title": "Item two", "labels": lab}]
        w.units = [unit("one-c", item="one", task=1), unit("loose", task=1)]
        w.closed = [{**unit(n, item=it), "status": "closed", "closed_at": "2026-11-17T10:00:00Z"} for n, it in (("one-a", "one"), ("one-b", "one"), ("two-a", "two"))]
        for n in ("one-a", "one-b", "two-a"): w.report(n, "VERDICT: DONE (landed)\n")
        w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})}); self.collect(); m = self.model()
        item = {r.key: r.cells[1] for r in m.rows["Active"]}
        self.assertEqual((item["one-c"], item["loose"]), ("ONE", "-"))
        b = {r.cells[0]: r for r in m.rows["Backlog"]}
        self.assertEqual((b["Item one"].state, b["Item one"].cells[1], b["Item one"].cells[2]), ("open-unit", "in progress", "2 done, 1 open: one-c"))
        self.assertEqual((b["Item two"].state, b["Item two"].cells[2]), ("units-done", "1 done"))
        self.assertTrue(b["Item two"].cells[1].startswith("all units done"))
        self.assertEqual(m.rows["Backlog"][0].cells[0], "Item two")                                  # what needs closing comes first
        self.assertIn("loose", next(r for r in m.rows["Overview"] if r.key == "o:no-item").cells[1])


class Stranded(Base):
    """A host down past [capacity] down_after_min, or marked by the owner: the monitor says what janus does about it."""
    def down(self, n, minutes): (self.d / ".janus/hosts.json").write_text(json.dumps({n: time.time() - minutes * 60}))
    def mark(self, n, m): (self.d / ".janus/host-marks.json").write_text(json.dumps({n: {"mark": m, "at": time.time()}}))

    def test_a_long_down_host_is_dimmed_last_its_units_stranded_and_not_a_source_problem(self):
        """Catches: a host down for hours shown like one that just missed a probe ('?' everywhere, its raw ssh error cut off), its
        units a mix of unknown/host-down/parked, counted as a source problem forever, sorted among the live hosts."""
        w = self.w; w.units = [unit("d", host="far", task=7), unit("n", host="far", task=8, relaunched="n-r2"), unit("p", host="far", task=9, parked={"jobs": ["far:9"]}), unit("live", task=1)]
        w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})}); w.fail.add("far"); w.report("d", "x\nVERDICT: DONE (landed)\n"); w.board("2026-11-13 09:00 ann status: x\n")
        self.down("far", 10); self.collect(); m = self.model()
        self.assertEqual(self.states("Active", m), {"d": "host-down:DONE", "n": "unknown", "p": "parked", "live": "working"})   # briefly down: as before
        self.assertEqual(next(r for r in m.machines if r.cells[0] == "far").cells[1], "UNREACHABLE"); self.assertEqual(m.down, 0)
        self.down("far", 120); self.collect(); m = self.model()
        self.assertEqual(self.states("Active", m), {"d": "stranded:DONE", "n": "stranded->n-r2", "p": "stranded", "live": "working"})
        self.assertEqual([r.cells[0] for r in m.rows["Active"]][0], "live"); self.assertTrue(all(r.style == "dim" for r in m.rows["Active"][1:]))
        self.assertIn("relaunched as n-r2", " ".join(next(r for r in m.rows["Active"] if r.key == "n").detail))
        far = next(r for r in m.machines if r.cells[0] == "far")
        self.assertTrue(far.cells[1].startswith("DOWN 2h"), far.cells); self.assertEqual((far.cells[2], far.cells[3], far.cells[4], far.cells[5], far.cells[6]), ("-", "3 stranded", "-", "-", "-"))
        self.assertEqual(far.style, "dim"); self.assertIn("ssh timed out", far.cells[8]); self.assertNotIn("port 22", far.cells[8])
        self.assertEqual((m.down, m.problems), (1, 0)); self.assertIn("1 host(s) down", M.status_line(self.store.view(), m, time.time(), 5))
        self.assertIn("port 22", " ".join(m.rows["Overview"][-1].detail))   # the whole error is still on the screen somewhere
        self.assertIn("stranded", " ".join(r.cells[1] for r in m.rows["Overview"] if r.key == "o:work"))

    def test_marks_and_order(self):
        """Catches: a gone host shown among the working ones (or as ok because it answers), a returning host's units relaunched."""
        self.w.units = [unit("b", host="far", task=2)]; self.w.fail.add("far"); self.down("far", 300); self.mark("far", "returning"); self.collect(); m = self.model()
        far = next(r for r in m.machines if r.cells[0] == "far"); self.assertTrue(far.cells[1].startswith("RETURNING 5h"), far.cells)
        self.assertEqual(self.states("Active", m), {"b": "unknown"})   # it waits for its host
        self.w.fail = set(); self.w.units = [unit("a", task=1)]; (self.d / ".janus/hosts.json").write_text("{}"); self.mark("here", "gone"); self.collect(); m = self.model()
        self.assertEqual([r.cells[0] for r in m.machines], ["far", "here"]); self.assertEqual(m.machines[1].cells[1], "GONE")
        self.assertIn("marked gone", m.machines[1].cells[8]); self.assertEqual(self.states("Active", m), {"a": "stranded"})

    def test_a_launch_that_never_finished_is_stalled(self):
        """Catches: a unit 'starting' for hours shown like a launch in progress."""
        self.w.units = [unit("old", via="starting", started=time.time() - 3 * 3600), unit("new", via="starting", started=time.time() - 60)]
        self.collect(); m = self.model()
        self.assertEqual(self.states("Active", m), {"old": "starting STALLED", "new": "starting"})
        self.assertIn("never finished", " ".join(next(r for r in m.rows["Active"] if r.key == "old").detail))

    def test_backlog_says_which_items_are_relaunched_and_where_they_ran(self):
        """Catches: an item covered only by a dead host's unit shown as 'open-unit' (in progress) for days; the covering host hidden;
        owner items counted as queued agent work."""
        w = self.w
        w.ready = [{"id": "b-1", "title": "Item one", "labels": ["backlog"]}, {"id": "b-2", "title": "Item two", "labels": ["backlog"]}]
        w.backlog = [{"id": "b-1", "title": "backlog: Item one", "labels": ["backlog"], "external_ref": "backlog:one"}, {"id": "b-2", "title": "Item two", "labels": ["backlog"], "external_ref": "backlog:two"},
                     {"id": "b-3", "title": "Item three", "labels": ["backlog", "actor:owner"], "external_ref": "backlog:three"}]
        w.units = [unit("s1", host="far", task=7, item="one"), unit("s2", host="far", task=8, item="two"), unit("l2", task=1, item="two")]
        w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})}); w.fail.add("far"); self.down("far", 120); self.collect(); m = self.model()
        rows = {r.cells[0]: r for r in m.rows["Backlog"]}
        self.assertEqual({k: (r.state, r.cells[3]) for k, r in rows.items()}, {"Item one": ("relaunch", "far"), "Item two": ("open-unit", "here"), "Item three": ("owner", "-")})
        self.assertIn("Open now:  s1 (far, host down)", rows["Item one"].detail); self.assertEqual(m.rows["Backlog"][0].cells[0], "Item one")   # what fill launches next first
        ui = M.UI(); ui.tab = 1; scr = "\n".join("".join(t for t, _ in l) for l in M.render(m, ui, 30, 200, time.time(), ""))
        self.assertIn("[Backlog (2 agent, 1 owner)]", scr)


class ReadOnly(Base):
    def test_nothing_is_written_and_nothing_mutating_runs_whoever_asks(self):
        """Catches: the monitor writing state (acks, handoff, hosts.json, locks, board marks) or running a mutating bd call, even when the
        caller is the registered orchestrator (check and status would be writers then)."""
        (self.d / ".janus" / "board").mkdir(); self.w.board("2026-11-13 10:00 ann question: [q1] which one?\n")
        (self.d / ".janus/board/questions").mkdir(); (self.d / ".janus/board/questions/q1.md").write_text("- id: q1\n- agent: ann\n- asked: 2026-11-13 10:00\n- state: open\n- text: which one?\n")
        self.cfg.f("session").write_text("SID-1"); self.w.units = [unit("a")]; self.w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})})
        before = snapshot(self.d); cfgs = []
        real = M.fresh_cfg; M.fresh_cfg = lambda root: (cfgs.append(real(root)), cfgs[-1])[1]
        self.addCleanup(setattr, M, "fresh_cfg", real)
        boom = [mock.patch.object(janus, n, side_effect=AssertionError(n)) for n in ("run_check", "board", "board_files", "sync_remote_outputs", "fetch_remote_outputs", "board_upkeep")]
        for p in boom: p.start(); self.addCleanup(p.stop)
        with mock.patch.dict(os.environ, {"JANUS_SESSION": "SID-1", "JANUS_ROLE": ""}):
            self.assertTrue(janus.is_orchestrator(self.cfg))   # the identity that makes check a writer
            self.collect(); self.collect()
        self.assertEqual(snapshot(self.d), before)
        self.assertFalse(self.cfg.f("write.lock").exists())
        self.assertEqual(len(cfgs), 2); self.assertIsNot(cfgs[0], cfgs[1]); self.assertTrue(all(c.run.ro and not c.run.dry for c in cfgs))   # fresh and read-only each time
        bd = [c for c in self.w.calls if c[0] == "bd"]; self.assertTrue(bd)
        for c in bd: self.assertEqual(c[1], "--readonly", c); self.assertFalse(MUTATING_BD & set(c[2:3]), c)
        self.assertFalse([c for c in self.w.calls if c[0].endswith("board.sh") or c[0] in ("rsync", "pueue", "tmux", "herdr")])   # no board command, no sync, nothing sent to pueue or tmux from here

    def test_a_missing_state_folder_is_not_created(self):
        """Catches: Cfg() quietly creating .janus in a folder that has none."""
        import shutil; shutil.rmtree(self.d / ".janus"); self.collect()
        self.assertFalse((self.d / ".janus").exists())
        self.assertIn("no .janus/janus.toml", self.store.view()[0]["config"].err); self.assertIn("no .janus/janus.toml", self.model().banners["Active"][0])

    def test_nothing_secret_reaches_the_screen(self):
        """Catches: a credential in a board post, a bd error or a host error printed on the screen or in details."""
        self.w.board("2026-11-13 10:00 ann status: set ANTHROPIC_API_KEY=sk-" "ant-abc123456789 and Authorization: Bearer tok-9f8e7d6c5b4a\n")
        self.w.fail = {"bd"}; self.collect()
        m = self.model(); ui = M.UI(); ui.tab = 3; ui.detail = True; ui.dkey = m.rows["Board"][0].key
        txt = "\n".join(t for line in M.render(m, ui, 40, 120, time.time(), "") for t, _ in line) + json.dumps(self.store.view()[0]["units"].err) + repr([r.detail for r in m.rows["Board"]])
        for s in ("abc123456789", "9f8e7d6c5b4a", "hunter2hunter2"): self.assertNotIn(s, txt)
        self.assertIn("***", txt)


class Recovery(Base):
    def test_stale_data_is_kept_with_its_age_and_error_then_recovers(self):
        """Catches: a failed source blanking the screen or showing zeros, a stale row looking fresh, a recovered source still flagged."""
        self.w.units = [unit("a")]; self.w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})}, avail=6144); self.w.board("2026-11-13 10:00 ann status: hello\n")
        self.collect(); self.assertEqual(self.model().problems, 0)
        self.w.fail = {"bd", "far"}; self.w.units = []; self.collect()
        m = self.model(offset=40)
        self.assertEqual(self.states("Active", m), {"a": "working STALE"}); self.assertTrue(m.rows["Active"][0].stale); self.assertIn("STALE", m.rows["Active"][0].cells[2])
        self.assertTrue(any("units: STALE, last good" in b and "dolt server unreachable" in b for b in m.banners["Active"]), m.banners)
        far = next(r for r in m.machines if r.cells[0] == "far"); self.assertEqual(far.cells[1], "UNREACHABLE"); self.assertEqual(far.cells[5], "4.0G"); self.assertIn("last good", far.cells[8])
        self.assertEqual(next(r for r in m.machines if r.cells[0] == "here").cells[1], "ok")
        self.assertEqual(len(m.rows["Board"]), 1)   # an unrelated source is untouched
        self.w.fail = set(); self.collect(); m = self.model()
        self.assertEqual(self.states("Active", m), {}); self.assertEqual(m.problems, 0); self.assertEqual(next(r for r in m.machines if r.cells[0] == "far").cells[1], "ok")
        self.assertFalse(any(m.banners.values()))

    def test_never_loaded_is_unknown_not_zero(self):
        """Catches: counts, builds, load, RAM and agents of a source that never answered shown as 0."""
        self.w.fail = {"bd", "far", "herdr"}; self.collect(); m = self.model()
        self.assertEqual(m.counts["Active"], None); self.assertEqual(m.counts["Ended"], None)
        far = next(r for r in m.machines if r.cells[0] == "far").cells
        self.assertEqual((far[1], far[2], far[3], far[4], far[5]), ("UNREACHABLE", "?/?", "?", "?", "?"))
        self.assertTrue(m.empty["Active"].startswith("bd") or "UNAVAILABLE" in m.empty["Active"], m.empty)
        ui = M.UI(); text = "\n".join(t for line in M.render(m, ui, 30, 100, time.time(), "") for t, _ in line)
        self.assertIn("Active (?)", text); self.assertIn("UNREACHABLE", text)

    def test_a_unit_on_an_unreachable_host_is_unknown_not_gone(self):
        """Catches: a host that did not answer turning its units into 'gone' / ended."""
        self.w.units = [unit("a", host="far", task=1)]; self.w.fail = {"far"}; self.collect()
        self.assertEqual(self.states("Active"), {"a": "unknown"})

    def test_herdr_failure_is_unknown_not_gone(self):
        """Catches: herdr's silence read as 'no agents': every herdr unit would look ended."""
        self.w.units = [unit("h", via="herdr")]; self.w.fail = {"herdr"}; self.collect()
        self.assertEqual(self.states("Active"), {"h": "unknown"})
        self.w.fail = set(); self.w.agents = [{"name": "h", "agent_status": "idle"}]; self.collect(); self.assertEqual(self.states("Active"), {"h": "idle"})


class Refresh(Base):
    def test_one_collection_at_a_time_on_interval_and_on_request(self):
        """Catches: overlapping collections, no automatic refresh, `r` not refreshing at once, the thread not stopping."""
        class Strict(M.Store):
            overlap = 0
            def begin(s, at):
                if s.running: Strict.overlap += 1
                super().begin(at)
        store = Strict(); col = M.Collector(self.d, store, 0.05); col.start(); self.addCleanup(col.stop)
        t0 = time.time()
        while store.runs < 3 and time.time() - t0 < 5: time.sleep(0.02)
        self.assertGreaterEqual(store.runs, 3, "no automatic refresh")
        col.interval = 600; time.sleep(0.4); n = store.runs; col.request(); t0 = time.time()
        while store.runs == n and time.time() - t0 < 3: time.sleep(0.02)
        self.assertGreater(store.runs, n, "r did not refresh at once")
        for _ in range(30): col.request()
        time.sleep(0.5); col.stop(); self.assertFalse(col.thread.is_alive()); self.assertEqual(Strict.overlap, 0)

    def test_a_new_collection_sees_new_data(self):
        """Catches: a refresh reusing the previous collection's units or ledger (it must start from nothing)."""
        self.collect(); self.assertEqual(self.states("Active"), {})
        self.w.units = [unit("n")]; self.w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}})}); self.collect(); self.assertEqual(self.states("Active"), {"n": "working"})


class FakeScr:
    def __init__(self, h, w, script): self.h, self.w, self.script, self.frames = h, w, list(script), []
    def getmaxyx(self): return self.h, self.w
    def erase(self): self.buf = [[" "] * self.w for _ in range(self.h)]
    def addstr(self, y, x, text, attr=0):
        if y >= self.h or x + len(text) > self.w: raise AssertionError(f"drew past the edge: row {y} col {x} len {len(text)} in {self.h}x{self.w}")
        self.buf[y][x:x + len(text)] = list(text)
    def refresh(self): self.frames.append(["".join(r).rstrip() for r in self.buf])
    def keypad(self, *a): pass
    def timeout(self, *a): pass
    def getch(self):
        if not self.script: return ord("q")
        k = self.script.pop(0)
        if callable(k): k(self); return -1   # a step between keys: the test acts, the loop redraws
        return k
    def text(self): return "\n".join(self.frames[-1])


class Spy:
    def __init__(self): self.n = 0
    def request(self): self.n += 1


def new_rows(text): return [l for l in text.splitlines() if re.match(r"^(> |  )NEW \d{4}-", l)]
def selected(text): return [l.split()[1] for l in text.splitlines() if l.startswith("> ")]


class Interaction(Base):
    K = M.curses
    def run_ui(self, scr, spy=None):
        with mock.patch.object(M.curses, "has_colors", return_value=False): attrs = M.make_attrs()
        return M.run_ui(scr, spy or Spy(), self.store, 5, attrs=attrs)
    def seed(self):
        """Rows in the order beta, gamma (working), delta (queued)."""
        w = self.w; w.units = [unit("beta", task=1), unit("delta", task=2), unit("gamma", task=3)]
        w.probes["here"] = probe_text(tasks={"1": task(1, {"Running": {}}), "2": task(2, "Queued"), "3": task(3, {"Running": {}})}); self.collect()
    def at(self, name): return lambda s: setattr(s, name, s.text())

    def test_tabs_selection_details_refresh_quit(self):
        """Catches: Tab/arrows/Enter/r/q not doing their job, details not showing the unit, q needing more than one press or reading on."""
        self.seed(); spy = Spy(); K = self.K
        scr = FakeScr(30, 110, [K.KEY_DOWN, self.at("s1"), 10, self.at("s2"), 27, self.at("s2b"), 9, self.at("s3"), K.KEY_BTAB, ord("r"), ord("4"), self.at("s4"), K.KEY_LEFT, self.at("s5"), ord("q"), ord("x")])
        self.assertEqual(self.run_ui(scr, spy), 0)
        self.assertEqual(selected(scr.s1), ["gamma"]); self.assertIn("[Active (3)]", scr.s1)
        self.assertIn("Details 2/3", scr.s2); self.assertIn("Name:      gamma", scr.s2); self.assertIn("Via:       pueue  task 3", scr.s2); self.assertNotIn("delta", scr.s2.split("Details")[1])
        self.assertEqual(selected(scr.s2b), ["gamma"])   # Esc: back to the list, same row
        self.assertIn("[Backlog (0)]", scr.s3); self.assertEqual(spy.n, 1); self.assertIn("[Board (?)]", scr.s4); self.assertIn("[Ended (0)]", scr.s5)
        self.assertEqual(scr.script, [ord("x")])   # q ended it: nothing after was read
        self.assertIn("READ-ONLY", scr.frames[0][0]); self.assertIn("RUN/QUEUED", scr.frames[0][1]); self.assertIn("working", scr.frames[0][-1] + "\n".join(scr.frames[0]))

    def test_selection_follows_its_row_through_reorders_and_insertions(self):
        """Catches: the selection staying on a screen position (so on another unit) when a refresh reorders rows or adds one above it."""
        self.seed(); K = self.K
        def refresh(s):
            self.w.units = [unit("alpha", task=3), unit("beta", task=1), unit("delta", task=2), unit("gamma", task=3)]
            self.w.probes["here"] = probe_text(tasks={"1": task(1, "Queued"), "2": task(2, {"Running": {}}), "3": task(3, {"Running": {}})}); self.collect()
        scr = FakeScr(30, 110, [K.KEY_DOWN, K.KEY_DOWN, self.at("a"), refresh, self.at("b")])
        self.run_ui(scr)
        self.assertEqual(selected(scr.a), ["delta"]); self.assertEqual(selected(scr.b), ["delta"])
        order = [l[2:].split()[0] if l[:2] in ("> ", "  ") and l[2:].strip() else "" for l in scr.b.splitlines()]
        self.assertLess(order.index("alpha"), order.index("delta")); self.assertGreater(order.index("beta"), order.index("delta"))   # beta, now queued, sank below it

    def test_selection_survives_when_its_row_leaves_and_details_say_so(self):
        """Catches: the selection becoming blank when its row leaves, details silently showing the neighbour or vanishing."""
        self.seed(); K = self.K
        def gone(s): self.w.units = [u for u in self.w.units if "gamma" not in u["title"]]; self.collect()
        scr = FakeScr(30, 110, [K.KEY_DOWN, 10, gone, self.at("a"), 27, self.at("b")])
        self.run_ui(scr)
        self.assertIn("no longer listed", scr.a); self.assertIn("Name:      gamma", scr.a); self.assertIn("Details -/2", scr.a)
        self.assertEqual(len(selected(scr.b)), 1); self.assertIn(selected(scr.b)[0], ("beta", "delta"))

    def test_new_board_messages_are_highlighted_until_selected(self):
        """Catches: old messages flagged new at start, a new message not flagged, the flag never clearing, the count missing, the four kinds not shown."""
        self.w.board("2026-11-13 09:00 ann status: started\n2026-11-13 09:05 ann question: [q1] which branch?\n2026-11-13 09:06 orchestrator direction: @ann [q1] use main\n2026-11-13 09:07 bob blocked: no disk\n")
        self.collect(); K = self.K
        def post(s): self.w.board("2026-11-13 10:00 ann done: VERDICT: DONE\n"); self.collect()
        scr = FakeScr(30, 110, [ord("4"), self.at("a"), post, self.at("b"), K.KEY_UP, self.at("c")])
        self.run_ui(scr)
        self.assertEqual(new_rows(scr.a), []); self.assertIn("[Board (4)]", scr.a)
        for kind in ("UPDATE", "QUESTION", "BLOCKER", "ANSWER"): self.assertIn(kind, scr.a)
        self.assertIn("orchestrator -> ann", scr.a); self.assertIn("ann ", [l for l in scr.a.splitlines() if "started" in l][0])   # a directed message names its recipient; an ordinary post keeps its sender alone
        self.assertIn("Board (5, 1 new)", scr.b); self.assertEqual(len(new_rows(scr.b)), 1); self.assertIn("VERDICT: DONE", new_rows(scr.b)[0]); self.assertIn("no disk", [l for l in scr.b.splitlines() if l.startswith("> ")][0])
        self.assertEqual(new_rows(scr.c), []); self.assertIn("[Board (5)]", scr.c)   # selected: read

    def test_unselected_new_messages_stay_flagged(self):
        """Catches: the flag clearing for messages the user has not reached."""
        self.w.board("2026-11-13 09:00 ann status: started\n"); self.collect()
        def post(s): self.w.board("2026-11-13 10:00 ann status: one\n2026-11-13 10:01 ann status: two\n"); self.collect()
        scr = FakeScr(30, 110, [ord("4"), post, self.at("a"), self.at("b")])
        self.run_ui(scr); self.assertEqual(len(new_rows(scr.b)), 2); self.assertIn("2 new", scr.b)

    def test_a_growing_memory_group_keeps_its_selection_and_flags_each_new_delivery(self):
        """Catches: a delivery joining a [memory] group marked read unseen (no NEW, no count), the selection jumping off the group as it grows."""
        mem = lambda to: f"2026-11-13 09:05 orchestrator direction: @{to} {MEM}\n"
        self.w.board("2026-11-13 09:00 ann status: started\n" + mem("a")); self.collect(); K = self.K
        def post(to): return lambda s: (self.w.board(mem(to)), self.collect())
        sel = lambda t: [l for l in t.splitlines() if l.startswith("> ")][0]
        scr = FakeScr(30, 110, [ord("4"), self.at("a"), post("b"), self.at("b"), K.KEY_DOWN, K.KEY_UP, self.at("c"), K.KEY_DOWN, post("c"), self.at("d"), 10, 10, self.at("e")])
        self.run_ui(scr)
        self.assertEqual(new_rows(scr.a), []); self.assertIn("[Board (2)]", scr.a); self.assertIn("MEMORY", sel(scr.a))
        self.assertIn("orchestrator -> 2 agents", sel(scr.b)); self.assertIn("Board (2, 1 new)", scr.b); self.assertEqual(len(new_rows(scr.b)), 1)   # still selected, the new delivery still flagged
        self.assertEqual(new_rows(scr.c), []); self.assertIn("[Board (2)]", scr.c)   # selected again: read
        self.assertIn("started", sel(scr.d)); self.assertIn("Board (2, 1 new)", scr.d); self.assertIn("3 agents", new_rows(scr.d)[0])   # not selected: flagged, the selection stays put
        self.assertIn("started", sel(scr.e)); self.assertIn("1 new", scr.e)

    def test_resize_small_terminals(self):
        """Catches: a crash or overflow on a small or resized terminal, the machine table crowding out the list."""
        self.seed(); self.w.fail = {"far"}; self.collect(); K = self.K; shots = {}
        size = lambda h, w: lambda s: (setattr(s, "h", h), setattr(s, "w", w))
        snap = lambda n: lambda s: shots.__setitem__(n, (s.h, s.w, s.text()))
        scr = FakeScr(30, 120, [snap("big"), size(12, 50), K.KEY_RESIZE, snap("small"), size(9, 60), K.KEY_RESIZE, snap("summary"), size(5, 30), K.KEY_RESIZE, snap("tiny"),
                                size(2, 10), K.KEY_RESIZE, snap("micro"), size(30, 100), K.KEY_RESIZE, snap("back")])
        self.run_ui(scr)
        for n, (h, w, t) in shots.items():
            lines = t.split("\n"); self.assertLessEqual(len(lines), h, n); self.assertTrue(all(len(l) <= w for l in lines), n)
        self.assertIn("UNREACHABLE", shots["big"][2]); self.assertIn("far", shots["big"][2])
        for n in ("small", "summary"): self.assertIn("UNREACHABLE", shots[n][2]); self.assertIn("beta", shots[n][2], n)   # the state survives narrowing; rows still show
        self.assertIn("HOSTS", shots["summary"][2]); self.assertNotIn("HOSTS", shots["small"][2])
        self.assertIn("beta", shots["tiny"][2]); self.assertIn("too small", shots["micro"][2]); self.assertIn("RUN/QUEUED", shots["back"][2])

    def test_monochrome_and_no_color(self):
        """Catches: meaning carried by colour only (the labels are text), NO_COLOR ignored, colour pairs in a monochrome terminal."""
        with mock.patch.object(M.curses, "has_colors", return_value=False): a = M.make_attrs()
        self.assertEqual((a["ok"], a["info"]), (0, 0)); self.assertTrue(all(a[k] & M.curses.A_BOLD for k in ("warn", "bad", "new")))
        with mock.patch.object(M.curses, "has_colors", return_value=True), mock.patch.dict(os.environ, {"NO_COLOR": "1"}), mock.patch.object(M.curses, "start_color", side_effect=AssertionError):
            self.assertEqual(M.make_attrs()["ok"], 0)
        self.w.fail = {"far"}; self.collect(); text = "\n".join(t for line in M.render(self.model(), M.UI(), 30, 100, time.time(), "") for t, _ in line)
        self.assertIn("UNREACHABLE", text)   # the state is a word, not only a colour

    def test_layout_never_exceeds_the_width(self):
        """Catches: columns wider than the terminal at any width."""
        for cols in (M.WORK_COLS, M.BOARD_COLS, M.HOST_COLS):
            for w in range(24, 200):
                lay = M.layout(cols, w); self.assertLessEqual(sum(x for _, x in lay) + len(lay) - 1, w, (w, lay))


MEM = "[memory] new decision [db]: use postgres (MEMORY.md has the whole list; no reply needed)"


class MemoryGroups(Base):
    def board_rows(self, text):
        self.w.board(text); self.collect(); return self.model().rows["Board"]

    def test_identical_memory_directions_are_one_row_with_each_recipient(self):
        """Catches: one [memory] change sent to every running unit flooding the board with a row per unit, a group hiding who got it
        or when, the log or the collected deliveries changed, ordinary posts, questions and answers drawn differently."""
        log = "".join(f"2026-11-13 09:05 orchestrator direction: @{u} {MEM}\n" for u in ("a", "b", "c"))
        log = "2026-11-13 09:00 ann status: started\n2026-11-13 09:01 ann question: [q1] which db?\n" + log + "2026-11-13 09:06 orchestrator direction: @ann [q1] use main\n"
        rows = self.board_rows(log)
        self.assertEqual([r.cells[2:4] for r in rows], [("ANSWER", "orchestrator -> ann"), ("MEMORY", "orchestrator -> 3 agents"), ("QUESTION", "ann"), ("UPDATE", "ann")])
        g = rows[1]; d = "\n".join(g.detail)
        self.assertIn("sent to 3 recipients", d); self.assertIn(MEM, g.detail[-1])
        for u in ("a", "b", "c"): self.assertIn(f"  {u}  2026-11-13 09:05", g.detail)
        self.assertEqual(len(g.keys), 3); self.assertEqual(g.key, g.keys[0])
        self.assertEqual((self.d / ".janus/board/log.md").read_text(), log)   # the log keeps a line per delivery
        self.assertEqual(sum(1 for m in self.store.view()[0]["board"].data["msgs"] if m["full"]), 3)
        self.assertEqual(rows[0].keys, ()); self.assertEqual(rows[0].detail[-1], "[q1] use main")   # ordinary rows as before

    def test_long_memory_texts_alike_for_1000_characters_stay_apart_and_show_whole(self):
        """Catches: grouping on the 1000-character cut text, merging two changes that differ only at their ends; details showing the cut text."""
        a, b = "[memory] " + "x" * 1200 + " END-A", "[memory] " + "x" * 1200 + " END-B"
        rows = self.board_rows(f"2026-11-13 09:05 orchestrator direction: @u1 {a}\n2026-11-13 09:05 orchestrator direction: @u2 {b}\n")
        self.assertEqual(len(rows), 2); self.assertEqual({r.detail[-1] for r in rows}, {a, b})
        self.assertTrue(all(r.cells[3] == f"orchestrator -> {to}" for r, to in zip(rows, ("u2", "u1"))))

    def test_distinct_revisions_questions_and_resends_stay_separate(self):
        """Catches: a change and its revision merged, the same text a minute later merged, a resend to the same unit counted as another
        recipient, a memory line that answered an open question (so [qN] first) folded in, another author's identical text merged."""
        r = lambda t, to, x, who="orchestrator": f"2026-11-13 {t} {who} direction: @{to} {x}\n"
        rev = MEM.replace("postgres", "sqlite")
        rows = self.board_rows(r("09:05", "a", MEM) + r("09:05", "a", rev) + r("09:05", "b", MEM)    # A, then its revision B, then A again: three changes
                               + r("09:07", "a", MEM) + r("09:08", "a", MEM) + r("09:08", "b", MEM) + r("09:08", "b", MEM)
                               + r("09:09", "c", "[q4] " + MEM) + r("09:09", "d", MEM, who="other") + r("09:09", "e", MEM))
        self.assertEqual([(r.cells[2], r.cells[3]) for r in rows],
                         [("MEMORY", "orchestrator -> e"), ("MEMORY", "other -> d"), ("ANSWER", "orchestrator -> c"), ("MEMORY", "orchestrator -> b"), ("MEMORY", "orchestrator -> 2 agents"),
                          ("MEMORY", "orchestrator -> a"), ("MEMORY", "orchestrator -> b"), ("MEMORY", "orchestrator -> a"), ("MEMORY", "orchestrator -> a")])
        self.assertIn("sqlite", rows[-2].detail[-1])


class Cleanup(Base):
    def test_slow_ssh_never_blocks_keys_or_quit_and_children_are_ended(self):
        """Catches: the screen waiting for a probe stuck on ssh (keys dead, q ignored), processes left behind, the screen not drawn at once."""
        self.w.block = threading.Event(); self.addCleanup(self.w.block.set)
        killed, screens = [], []
        args = type("A", (), {"interval": 5.0})()
        def wrapper(fn): return fn(FakeScr(30, 100, [lambda s: time.sleep(0.3), lambda s: screens.append(s.text()), 9, ord("q")]))
        t0 = time.time(); import signal; handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
        with mock.patch.object(M.sys.stdin, "isatty", return_value=True), mock.patch.object(M.sys.stdout, "isatty", return_value=True), mock.patch("locale.setlocale"), \
             mock.patch.object(M.curses, "wrapper", wrapper), mock.patch.object(M, "kill_children", lambda: killed.append(1)), mock.patch.object(M, "make_attrs", lambda: {}):
            self.assertEqual(M.cmd_monitor(self.cfg, args), 0)
        self.assertLess(time.time() - t0, 3, "quit waited for the slow host")
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)), handlers)   # restored
        self.assertEqual(killed, [1]); self.assertRegex(screens[0], r"far\s+checking"); self.assertRegex(screens[0], r"here\s+ok")   # the fast host answered, the slow one is still 'checking'
        self.assertEqual(self.store.view()[0].get("host:far"), None)

    def test_children_never_read_the_terminal_and_the_patch_is_undone(self):
        """Catches: an ssh child reading the monitor's keystrokes; the runner patch leaking out of the monitor."""
        import janus.context as C, janus.platform as P
        seen = []; fake = lambda cmd, cwd=None, inp=None, timeout=25, env=None: (seen.append(inp), (0, "", ""))[1]
        real_p, real_c = P.run, C.run; P.run = C.run = fake
        try:
            with M.detached_stdin(): C.run(["true"]); P.run(["true"], inp="x")
            self.assertEqual(seen, ["", "x"]); self.assertIs(P.run, fake); self.assertIs(C.run, fake)
        finally: P.run, C.run = real_p, real_c

    def test_kill_children_ends_only_descendants(self):
        """Catches: quit leaving ssh/sh/bd children behind, or signalling processes that are not ours."""
        me = os.getpid(); table = {me: (1, 1, "x", "janus"), 501: (me, 1, "x", "ssh far"), 502: (501, 1, "x", "sh"), 600: (1, 1, "x", "other"), 601: (600, 1, "x", "child of other")}
        sent = []
        with mock.patch.object(M, "ps_table", lambda: table), mock.patch.object(M.os, "kill", lambda pid, sig: sent.append((pid, sig))):
            self.assertEqual(M.kill_children(), 2)
        self.assertEqual(sorted(p for p, _ in sent), [501, 502]); self.assertTrue(all(s == M.signal.SIGTERM for _, s in sent))

    def test_no_terminal_is_refused_cleanly(self):
        """Catches: a traceback, or a hang, when the monitor is run without a terminal."""
        env = {k: v for k, v in os.environ.items() if k not in ("JANUS_ROOT", "JANUS_UNIT", "JANUS_ROLE", "JANUS_REPORT")}
        r = subprocess.run([sys.executable, str(JANUS_PY), "monitor"], cwd=self.d, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(r.returncode, 1); self.assertIn("interactive terminal", r.stderr); self.assertNotIn("Traceback", r.stderr)


class Cli(unittest.TestCase):
    def test_interval_default_and_validation(self):
        """Catches: --interval missing, a default other than 5 seconds, an interval that would hammer every host, monitor not registered."""
        out = subprocess.run([sys.executable, str(JANUS_PY), "monitor", "--help"], capture_output=True, text=True).stdout
        self.assertIn("--interval", out); self.assertIn("default 5", out)
        for bad in ("0", "0.2", "-3", "abc"):
            r = subprocess.run([sys.executable, str(JANUS_PY), "monitor", "--interval", bad], capture_output=True, text=True); self.assertEqual(r.returncode, 2, bad)
        self.assertEqual((M.interval_arg("2.5"), M.interval_arg("5")), (2.5, 5.0))
        import ast
        src = ast.parse((Path(JANUS_PY).parent / "janus/cli.py").read_text())
        call = next(n for n in ast.walk(src) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "add" and n.args and getattr(n.args[0], "value", "") == "monitor")
        self.assertEqual(call.args[1].id, "cmd_monitor")
        opt = next(a for a in call.args[2:] if isinstance(a, ast.Call) and a.args[0].value == "--interval")
        self.assertEqual(next(k.value.value for k in opt.keywords if k.arg == "default"), 5.0)

    def test_scrub_masks_credentials_and_controls(self):
        """Catches: credential-shaped text or terminal escapes reaching the screen."""
        for s in ("TOKEN=abcd1234", "api_key: 'sk-live-0123456789'", "Authorization: Bearer abcdef123", "x ghp" "_0123456789abcdef0123 y", '"password": "p4ss"'):
            out = M.clean(s); self.assertIn("***", out, s); self.assertNotRegex(out, r"abcd1234|0123456789|abcdef123|p4ss")
        self.assertEqual(M.clean("a\x1b[31mred\x07\n b"), "a [31mred b"); self.assertEqual(M.clean("token expired"), "token expired")


if __name__ == "__main__": unittest.main()
