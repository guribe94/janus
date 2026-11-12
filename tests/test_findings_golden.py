"""The golden findings test: the merge gate for moving check's code (section 7, Step 0).

One fixture project seeds ~25 conditions (an unreachable host, margins, back-pressure, another scheduler, a direct build, orphan
jobs and sessions, finished/limit/disabled-provider units, board blocks and learned posts, failed gates, a malformed ledger,
caps, spend, tidy, memory, instructions, beads down) and asserts the exact sorted list of finding keys collect_findings returns.
Everything outside the project is faked (probes, beads, herdr, the board's summary, the process table), so the list is the same
on every machine. A key that changes, disappears or appears fails here first. Standard library only; no network."""
import json, tempfile, textwrap, time, unittest
from pathlib import Path
from janusload import load

janus = load()

GOLDEN = [
    "beads", "board:u1:blocked", "board:x9:gone", "burner:501", "cap:far", "cap:total", "direct:far:77",
    "disk:far", "gate-common:EXIT_tests", "gate:g1@{t}", "gate:g2@{t}", "host:dead", "instructions",
    "learned:u1:1", "ledger:api", "ledger:api:implausible", "ledger:api:unreconciled", "localtest:502", "margin:far:disk", "memory-review:1", "memory-source:1",
    "orphan:job:here:7", "orphan:tmux:far:janus-zzz", "pressure:far", "provider:u3", "question:3", "report-due", "scheduler:far",
    "spend:api", "tidy:1", "unit:u2:finished", "unit:u2:limit",
]

# the watcher's urgent prefixes before the registry (janus.py URGENT): the registry must keep the same findings urgent
URGENT_BEFORE = ("unit:", "board:", "question:", "related:", "overlap:", "orphan:", "host:", "margin:", "stalled:", "spend:", "instructions", "beads",
                 "old-rule:", "burner:", "ledger:", "script:", "idle-backlog:", "landing:", "idle-with-backlog", "scheduler:", "pressure:",
                 "direct:", "budget80:", "waiting:", "waste", "report-due", "parked:", "decision-missing:", "owner-question:")

PS = (" 501     1   501  99.0 20:00 /opt/miner --all\n"                      # an orphaned CPU burner, 20 min
      " 502     1   502   1.0 00:30 cargo test -p x\n")                       # a test run outside the lanes


class Golden(unittest.TestCase):
    def test_collect_findings_keys(self):
        d = Path(tempfile.mkdtemp(prefix="janus-golden-"))
        (d / "janus.toml").write_text(textwrap.dedent(f'''
            [project]
            name = "g"
            [paths]
            gate_log = "out/gates.log"
            [caps]
            total = 2
            host = {{ far = 1 }}
            [hosts.here]
            local = true
            [hosts.far]
            ssh = "nowhere"
            root = "/x"
            [hosts.dead]
            ssh = "nowhere"
            root = "/x"
            [providers.api]
            enabled = true
            cap_usd = 1
            budget_per_unit = 1
            ledger = "out/ledger.tsv"
            [providers.off]
            enabled = false
            [models.m]
            provider = "api"
            kind = "api"
            [models.o]
            provider = "off"
            kind = "claude"
            '''))
        (d / "out").mkdir()
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
        (d / "out/gates.log").write_text(f"{t} g1 EXIT_tests=1 RESULT: FAIL\n{t} g2 EXIT_tests=1 RESULT: FAIL\n")
        (d / "out/ledger.tsv").write_text("2026-01-01 00:00:00\tu1\trunning\t2\tu1.1\ngarbage\n")   # malformed; $2 committed of a $1 cap
        (d / "out/u2").mkdir(); (d / "out/u2/api.json").write_text(json.dumps({"subtype": "error_max_turns", "total_cost_usd": 0.1}))
        cfg = janus.Cfg(d); cfg.run.ro = True   # a reader: nothing is enforced, tuned or written
        cfg.f("tidy.json").write_text(json.dumps({"at": int(time.time()), "plan": [["disk", "far", "12 GB over", 0], ["run-files", "here", "x", 5]]}))
        cfg.f("MEMORY.md").write_text("# Session memory\n\n## Facts\n- 2020-01-01 a fact (source: gone/file.md)\n")
        bd = d / "board"; bd.mkdir(); (bd / "log.md").write_text("2026-01-01 00:00 u1 learned: x is y\n")
        units = [{"id": "g-1", "name": "u1", "host": "far", "via": "tmux", "model": "m", "started": int(time.time())},
                 {"id": "g-2", "name": "u2", "host": "here", "via": "pueue", "task": 1, "model": "m", "started": int(time.time())},
                 {"id": "g-3", "name": "u3", "host": "far", "via": "tmux", "model": "o", "started": int(time.time())}]
        tasks = {"1": {"id": 1, "group": "agents", "label": "u2", "status": {"Done": {"result": "Failed"}}},
                 "7": {"id": 7, "group": "agents", "label": "stray", "status": {"Running": {}}}}
        hs = {"here": {"name": "here", "ok": True, "avail_mb": "64000", "disk_gb": "500", "load": "0", "pueue": {"groups": {}, "tasks": tasks},
                       "tmux": [], "res": [], "hist": [], "sched": [], "direct": [], "waitjob": []},
              "far": {"name": "far", "ok": True, "avail_mb": "64000", "disk_gb": "1", "load": "0", "cap_pause": "psi 40", "pueue": {"groups": {}, "tasks": {}},
                      "tmux": ["janus-u1", "janus-u3", "janus-zzz"], "res": [], "hist": [], "sched": ["999 /usr/bin/oldq"],
                      "direct": ["77 77 u1 900 cargo build"], "waitjob": []},
              "dead": {"name": "dead", "ok": False, "err": "ssh: timeout"}}

        class FakeBeads:
            def __init__(self, cfg): pass
            def units(self): return [dict(u) for u in units]
            def run(self, *a): return [{"id": "g-9", "title": "ready work", "labels": []}] if a[:1] == ("ready",) else []
            def issues(self, label, status="open"): return [{"id": "g-8", "title": "instr: an owner instruction"}] if label == "instruction" else []
        saved = {k: getattr(janus, k) for k in ("Beads", "herdr", "board_rows", "board_questions", "proc_findings", "script_findings",
                                               "launchd_watcher_loaded", "rule_findings")}
        real_proc = janus.proc_findings
        try:
            janus.Beads = FakeBeads; janus.herdr = lambda *a, **k: None
            janus.board_rows = lambda cfg: {"u1": {"state": "blocked", "silent": False, "gone": False, "text": "needs a key"},
                                           "x9": {"state": "working", "silent": False, "gone": True, "text": ""}}
            janus.board_questions = lambda cfg: [("3", "u1", "2026-01-01 00:00", "which branch?")]
            janus.proc_findings = lambda cfg, ps_text=None, viol=None: real_proc(cfg, PS, viol)
            janus.script_findings = lambda: []
            janus.launchd_watcher_loaded = lambda *a: True
            janus.rule_findings = lambda cfg: []
            F = janus.collect_findings(cfg, hs, FakeBeads(cfg).units(), {}, "bd: no database")
        finally:
            for k, v in saved.items(): setattr(janus, k, v)
        want = sorted(k.format(t=t[11:]) for k in GOLDEN); self.maxDiff = None
        self.assertEqual(sorted(f.key for f in F), want)
        self.assertEqual([f.key for f in F if f.urgent], [f.key for f in F if f.key.startswith(URGENT_BEFORE)])
        self.assertEqual([f for f, x in janus.FAMILIES.items() if x.urgent], [f for f in janus.FAMILIES if (f + ":").startswith(URGENT_BEFORE)])
        self.assertEqual([f for f in janus.FAMILIES if not janus.ackable(f + ":x")], ["waiting"])


if __name__ == "__main__":
    unittest.main()
