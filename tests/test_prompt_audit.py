"""The prompt audit after rig29: what janus tells agents and the orchestrator, checked against what the code does (a local agent
never told to commit, a bare board.sh on no PATH, findings naming a command the orchestrator does not have), what every session
start loads (a 197 KB handoff, a 94 KB memory), the one nudge for a client without a Stop hook, and old briefs.
Standard library only; no network, no spend. Run by ./check (it discovers tests/test_*.py)."""
import contextlib, io, json, os, time, unittest
from pathlib import Path
from unittest import mock

import janusload
from test_janus import janus, project

FAR = '\n[hosts.far]\nssh = "u@far"\nroot = "/r"\n'
MEM = [("Instructions", "2026-11-01", "tests", "run one targeted check", None), ("Decisions", "2026-11-02", "q308", "option A for q308", None),
       ("Facts", "2026-11-03", None, "the rig has two cameras", "janus hosts"), ("Pitfalls", "2026-11-04", "oom", "rustc needs 4 GB", None)]


class WhatAnAgentIsTold(unittest.TestCase):
    def test_a_local_agent_is_told_to_commit_and_how_to_reach_the_board(self):
        """Catches: a local agent ending DONE with uncommitted work (janus land refuses such a tree; only the remote header had a git
        paragraph), `board.sh` named bare (on no PATH), 'the lane' named to an agent that has none, FAILED missing from the verdicts."""
        d, cfg = project(FAR); (d / ".git").mkdir(); B = str(janus.JANUS_SELF.parent / "board.sh")
        h = janus.brief_header(cfg, "here", cfg.hosts["here"], "u1", str(d), True, report="/x/REPORT.md")
        self.assertIn("commit your work on its current branch", h); self.assertIn("refuses a tree with uncommitted changes", h)
        self.assertIn(f"`{B} send <unit|orchestrator> <text>`", h); self.assertIn(f"`{B} read u1`", h); self.assertNotIn("`board.sh ", h)
        self.assertIn("`VERDICT: FAILED`", h); self.assertIn("--usb", h); self.assertIn("Do not run builds, tests or other heavy commands directly", h)
        self.assertNotIn("through the lane", h)
        (d / ".git").rmdir()
        self.assertNotIn("Git (janus)", janus.brief_header(cfg, "here", cfg.hosts["here"], "u1", str(d), True))   # no repository: no git rule
        r = janus.brief_header(cfg, "far", cfg.hosts["far"], "u1", "/r/worktrees/u1", False, report="/r/out/u1/REPORT.md")
        self.assertIn("`/r/bin/board.sh send <unit|orchestrator> <text>`", r)
        self.assertIn("'VERDICT: FAILED'", janus.agent_prompt(cfg, "u1", "/b.md", "here"))
        self.assertEqual(janus.remote_env(cfg, cfg.hosts["far"], "u1")["JANUS_BOARD"], "/r/bin/board.sh")

    def test_the_stop_hook_names_the_board_by_its_path(self):
        d, cfg = project(); rp = d / "R.md"; rp.write_text("VERDICT: PENDING\nworking\n"); out = io.StringIO()
        env = {"JANUS_REPORT": str(rp), "JANUS_BOARD": "/r/bin/board.sh", "JANUS_UNIT": "u1"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out): janus.agent_hook("Stop", {})
        self.assertIn("/r/bin/board.sh post u1 question <text>", json.loads(out.getvalue())["reason"])

    def test_an_agent_gets_the_binding_memory_and_a_pointer_to_the_rest(self):
        """Catches: every brief and every resume carrying the whole memory (94 KB in one project)."""
        d, cfg = project(FAR); janus.memory_write(cfg, MEM)
        h = janus.swarm_header(cfg, "here", cfg.hosts["here"], "u1", "/w", True)
        self.assertIn("run one targeted check", h); self.assertIn("rustc needs 4 GB", h)
        self.assertNotIn("option A for q308", h); self.assertNotIn("two cameras", h)
        self.assertIn(f"Decisions and facts (2 entries, not repeated here): {janus.memory_path(cfg)}", h)
        self.assertIn("/r/MEMORY.md", janus.swarm_header(cfg, "far", cfg.hosts["far"], "u1", "/r/w", False))
        self.assertIn("option A for q308", janus.session_start_text(cfg))   # the orchestrator still gets all of it


class WhatTheOrchestratorIsTold(unittest.TestCase):
    def test_no_finding_names_a_board_command_the_orchestrator_does_not_have(self):
        """Catches: 'answer with board.sh direct' (the orchestrator has janus answer and janus send; it called a release's board.sh by hand)."""
        pkg = Path(janus.JANUS_SELF).parent / "janus"
        for f in [*sorted((pkg / "findings").glob("*.py")), pkg / "board.py"]:
            self.assertNotRegex(f.read_text(), r"board\.sh direct", f.name)

    def test_a_long_handoff_or_memory_is_a_finding(self):
        """Catches: a handoff appended to at every checkpoint (197 KB, void workarounds read as current) and a memory nobody prunes,
        both loaded whole at every session start, with nothing measuring them."""
        d, cfg = project(); hp = janus.handoff_path(cfg)
        hp.write_text("x" * 10_000); self.assertEqual(janus.long_texts(cfg), [])
        hp.write_text("x" * 20_000); F = janus.long_texts(cfg)
        self.assertEqual([k for k, _ in F], ["handoff:long"]); self.assertIn("rewrite it in place", F[0][1]); self.assertIn("HANDOFF-archive.md", F[0][1])
        cfg.c["project"]["handoff_max_kb"] = 30; self.assertEqual(janus.long_texts(cfg), [])
        janus.memory_write(cfg, [("Decisions", "2026-11-02", f"k{i}", "y" * 1000, None) for i in range(60)])
        F = janus.long_texts(cfg); self.assertEqual([k for k, _ in F], ["memory:long"]); self.assertIn("60 entries", F[0][1]); self.assertIn("janus forget", F[0][1])
        self.assertEqual({janus.finding(k, t).family for k, t in (("handoff:long", ""), ("memory:long", ""))}, {"handoff", "memory"})   # registered
        self.assertIn("memory:long", [k for k, _ in janus.orchestrator_findings(cfg)])


class OneNudgeWithoutAStopHook(unittest.TestCase):
    """An opencode agent has no Stop hook: a turn that ended with neither a verdict nor a park only raised a finding."""
    def run_pass(self, rec, report="VERDICT: PENDING\nplan written\n", **live):
        d, cfg = self.d, self.cfg; rp = d / ".janus/out/u/REPORT.md"; rp.parent.mkdir(parents=True, exist_ok=True); rp.write_text(report)
        sent = self.sent
        class B:
            def __init__(s, c): pass
            def units(s): return [dict(rec)]
            def save(s, u): rec.clear(); rec.update({k: v for k, v in u.items() if k != "state" and not k.startswith("_")})
        with mock.patch.object(janus, "Beads", B), mock.patch.object(janus, "resume_unit", lambda cfg, u, msg, budget=None: sent.append(msg) or "resumed"):
            with cfg.writer(): janus.tend_parked(cfg, {"here": {"ok": True}}, [dict(rec, state="idle", **live)], lambda s: None, {})
        return rec

    def setUp(self): self.d, self.cfg = project(); self.sent = []

    def test_an_ended_turn_without_a_verdict_is_nudged_once(self):
        rec = {"id": "1", "name": "u", "host": "here", "via": "tmux", "model": "m", "turns": True}
        self.run_pass(rec, _turn_end="1700000000")
        self.assertEqual(len(self.sent), 1); self.assertIn("Not finished", self.sent[0]); self.assertIn("board.sh post u question", self.sent[0])
        self.assertFalse(self.sent[0].startswith("[janus]"))                        # resume_unit adds the mark: never twice
        self.assertEqual(rec["nudged"], 1700000000)
        self.run_pass(rec, _turn_end="1700000900")                                   # the nudge's own turn ended the same way: the finding stands
        self.run_pass({k: v for k, v in rec.items() if k != "nudged"}, _turn_end="1700000000")   # the same turn end, read again: once
        self.assertEqual(len(self.sent), 1)

    def test_only_a_hookless_unfinished_turn_is_nudged(self):
        base = {"id": "1", "name": "u", "host": "here", "via": "tmux", "model": "m"}
        self.run_pass(dict(base), _turn_end="1700000000")                                        # a Claude agent: its hook did it
        self.run_pass(dict(base, turns=True))                                                    # its turn still runs
        self.run_pass(dict(base, turns=True), "VERDICT: DONE\nexit code 0\n", _turn_end="1700000001")
        self.run_pass(dict(base, turns=True), "VERDICT: PENDING\nWAITING here:5\n", _turn_end="1700000002")
        self.assertEqual(self.sent, [])

    def test_any_other_resume_clears_the_mark(self):
        u = {"id": "1", "name": "u", "host": "here", "via": "tmux", "model": "nope", "nudged": 5}
        with self.assertRaises(janus.Err): janus.resume_unit(self.cfg, u, "your jobs finished")   # refused later (its model); the mark went first
        self.assertNotIn("nudged", u)


class OldBriefs(unittest.TestCase):
    def test_old_briefs_nothing_reads_are_archived_and_the_rest_stay(self):
        """Catches: 5,657 files in one project's briefs folder, read at every check and fill, covered by no tidy rule."""
        d, cfg = project(); bd = d / ".janus/briefs"; old = time.time() - 30 * 86400
        def brief(n, text="do it\n", age=old):
            p = bd / n; p.parent.mkdir(exist_ok=True); p.write_text(text); os.utime(p, (age, age)); return p
        for n in ("closed-old.md", "recent-closed.md", "open-unit.md", "relaunched.md", "noitem.md", "drafts/d.md", "notes.txt"): brief(n)
        brief("ready-item.md", "ITEM: X1\n"); brief("closed-item.md", "ITEM: X2\n"); brief("young.md", age=time.time()); brief("held.md")
        au = {"closed-old": {"status": "closed", "_closed_at": "2026-01-01T00:00:00Z"},
              "recent-closed": {"status": "closed", "_closed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
              "open-unit": {"status": "in_progress"}, "relaunched": {"status": "closed", "_closed_at": "2026-01-01T00:00:00Z"}}
        units = [{"name": "open-unit", "id": "1"}, {"name": "relaunched-r2", "id": "2"}]
        iss = {"backlog:X1": {"status": "open"}, "backlog:X2": {"status": "closed"}}
        janus.hold_item(cfg, str(bd / "held.md"), "kept")
        class B:
            def __init__(s, c): pass
            def units(s): return units
        with mock.patch.object(janus, "backlog_issues", lambda c: iss), mock.patch.object(janus, "all_units", lambda c: au), mock.patch.object(janus, "Beads", B):
            self.assertEqual(sorted(f.name for f, _ in janus.old_briefs(cfg, units, au)), ["closed-item.md", "closed-old.md", "noitem.md"])
            P = janus.tidy_briefs(cfg, units, au)
            self.assertEqual([(k, t) for k, t, _, _ in P], [("brief", str(bd))]); self.assertIn("3 brief file(s)", P[0][2])
            self.assertEqual(janus.tidy_do(cfg, "brief", str(bd)), (0, "3 moved"))
        self.assertEqual(sorted(f.name for f in (d / ".janus/briefs-archive").iterdir()), ["closed-item.md", "closed-old.md", "noitem.md"])
        self.assertEqual(sorted(f.name for f in bd.iterdir()), ["drafts", "held.md", "notes.txt", "open-unit.md", "ready-item.md", "recent-closed.md", "relaunched.md", "young.md"])


if __name__ == "__main__":
    unittest.main()
