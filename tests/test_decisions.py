"""Decision records and the owner's decision queue (imp-adr).

Each test names the failure it catches: a record that ignores the template, a supersede that rewrites the old
record, a search that only looks at titles, a dry run that writes, a notify command that never runs, a classified
owner instruction closed with no record. The beads reads are faked, so no bd is needed. Standard library only.
"""
import argparse, tempfile, textwrap, time, unittest
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
        ''') + extra)
    (d / "out").mkdir(); (d / "briefs").mkdir()
    return d, janus.Cfg(d)


def ns(title, **k):
    return argparse.Namespace(title=title, by=k.pop("by", "owner"), context=k.pop("context", "what was true"),
                              options=k.pop("options", ["a - cheap", "b - safe"]), decision=k.pop("decision", "b is chosen"),
                              why=k.pop("why", "safety"), consequences=k.pop("consequences", None),
                              supersedes=k.pop("supersedes", None), instruction=k.pop("instruction", None), **k)


def fake_beads(instructions):
    class FakeBeads:
        def __init__(self, cfg): pass
        def run(self, *a): return []
        def issues(self, label, status="open"): return instructions if label == "instruction" else []
    return FakeBeads


class Records(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project()

    def test_record_is_filled_from_the_template_and_indexed(self):
        """Catches: a record missing a section of the template, or an INDEX.md that is never regenerated."""
        janus.cmd_decide(self.cfg, ns("Keys live in the keychain"))
        text = (self.cfg.f("decisions") / "0001-keys-live-in-the-keychain.md").read_text()
        for sec in ("## Context", "## Options considered", "## Decision", "## Why", "## Consequences"): self.assertIn(sec, text)
        self.assertIn("- Status: accepted", text); self.assertIn("- Decided by: owner", text)
        self.assertIn("- Date: " + time.strftime("%Y-%m-%d"), text)
        self.assertIn("1. a - cheap", text)
        self.assertIn("0001 | Keys live in the keychain", (self.cfg.f("decisions") / "INDEX.md").read_text())

    def test_supersede_changes_only_the_status_line(self):
        """Catches: superseding by rewriting the old record, instead of marking its one Status line."""
        janus.cmd_decide(self.cfg, ns("First"))
        p = self.cfg.f("decisions") / "0001-first.md"
        before = p.read_text()
        janus.cmd_decide(self.cfg, ns("Second", supersedes=1))
        after = p.read_text()
        diff = [(x, y) for x, y in zip(before.splitlines(), after.splitlines()) if x != y]
        self.assertEqual(diff, [("- Status: accepted", "- Status: superseded by 0002")])
        self.assertIn("- Supersedes: 0001-first", (self.cfg.f("decisions") / "0002-second.md").read_text())

    def test_search_looks_at_the_body_not_only_titles(self):
        """Catches: --grep that matches only titles and misses the reason inside the record."""
        janus.cmd_decide(self.cfg, ns("First", context="the token rotates weekly"))
        janus.cmd_decide(self.cfg, ns("Second", context="unrelated"))
        _, out = janus.cmd_decisions(self.cfg, argparse.Namespace(grep="rotates weekly"))
        self.assertEqual(len(out), 1); self.assertIn("0001", out[0])
        self.assertEqual(janus.cmd_decisions(self.cfg, argparse.Namespace(grep="nowhere"))[1], ["(no decision matches 'nowhere')"])

    def test_dry_run_writes_nothing(self):
        """Catches: a dry run that still creates the record or the index."""
        self.cfg.run.dry = True
        janus.cmd_decide(self.cfg, ns("Ghost"))
        self.assertFalse((self.cfg.f("decisions") / "0001-ghost.md").exists())
        self.assertFalse((self.cfg.f("decisions") / "INDEX.md").exists())
        self.assertFalse(janus.cmd_decide(self.cfg, ns("Ghost"))[1])

    def test_a_record_without_a_decision_is_refused(self):
        """Catches: a record whose Decision section is empty (a title with nothing decided)."""
        with self.assertRaises(janus.Err): janus.cmd_decide(self.cfg, ns("Empty", decision=""))

    def test_an_agent_cannot_write_a_decision(self):
        """Catches: a unit agent writing a decision record instead of proposing it on the board."""
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"JANUS_ROLE": "agent"}):
            with self.assertRaises(janus.Err): janus.cmd_decide(self.cfg, ns("By an agent"))
            with self.assertRaises(janus.Err): janus.cmd_ask(self.cfg, argparse.Namespace(question="?", options=[], recommend=None))


class Queue(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project()

    def ask(self, **k):
        return janus.cmd_ask(self.cfg, argparse.Namespace(question=k.pop("q", "Which branch lands first?"),
                                                          options=k.pop("options", ["feat/a", "feat/b"]), recommend=k.pop("recommend", "feat/a")))

    def test_ask_then_answer_closes_the_question(self):
        """Catches: an answered question that stays open, or an answer that replaces the whole queue."""
        self.ask()
        self.assertEqual([(q["n"], q["status"], q["recommend"]) for q in janus.owner_queue(self.cfg)], [(1, "open", "feat/a")])
        janus.cmd_answer(self.cfg, argparse.Namespace(id=1, answer="feat/a first", decide=False))
        q = janus.owner_queue(self.cfg)
        self.assertEqual(q[0]["status"], "answered"); self.assertEqual(q[0]["answer"], "feat/a first")
        with self.assertRaises(janus.Err): janus.cmd_answer(self.cfg, argparse.Namespace(id=1, answer="again", decide=False))

    def test_dry_run_ask_and_answer_write_nothing(self):
        """Catches: a dry run that adds the question, or closes it, on disk anyway."""
        self.cfg.run.dry = True
        self.ask()
        self.assertFalse(janus.queue_path(self.cfg).exists())
        self.cfg.run.dry = False; self.ask()
        self.cfg.run.dry = True
        janus.cmd_answer(self.cfg, argparse.Namespace(id=1, answer="feat/a first", decide=False))
        self.assertEqual(janus.owner_queue(self.cfg)[0]["status"], "open")

    def test_answer_decide_writes_the_record(self):
        """Catches: an important answer that closes the queue entry but leaves no decision record."""
        self.ask(); janus.cmd_answer(self.cfg, argparse.Namespace(id=1, answer="feat/a first", decide=True))
        rs = janus.decisions(self.cfg)
        self.assertEqual([(r["by"], r["status"]) for r in rs], [("owner", "accepted")])
        self.assertIn("feat/a first", rs[0]["text"])

    def test_the_notify_command_runs_with_the_question(self):
        """Catches: the configured [owner] notify command never being run, or run without the question."""
        d = Path(tempfile.mkdtemp(prefix="janus-test-"))
        (d / "janus.toml").write_text(textwrap.dedent('[project]\nname = "t"\n[hosts.here]\nlocal = true\n') + f'[owner]\nnotify = "cat > {d}/q.txt"\n')
        cfg = janus.Cfg(d)
        janus.cmd_ask(cfg, argparse.Namespace(question="Which branch?", options=["a"], recommend=None))
        self.assertIn("Which branch?", (d / "q.txt").read_text())


class Findings(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project()

    def test_decision_missing_for_a_classified_instruction_until_linked(self):
        """Catches: an owner instruction classified as a decision, with no record, passing check unnoticed."""
        ins = [{"id": "i-1", "labels": ["instruction", "decision"], "title": "instr: choose the store"}]
        saved = janus.Beads; janus.Beads = fake_beads(ins)
        try:
            self.assertIn("decision-missing:i-1", dict(janus.decision_findings(self.cfg)))
            janus.cmd_decide(self.cfg, ns("Keys live in the keychain", instruction="i-1"))
            self.assertNotIn("decision-missing:i-1", dict(janus.decision_findings(self.cfg)))
        finally: janus.Beads = saved

    def test_a_plain_instruction_is_not_flagged(self):
        """Catches: flagging every owner instruction, not only the ones classified as a decision."""
        ins = [{"id": "i-9", "labels": ["instruction"], "title": "instr: a routine ask"}]
        saved = janus.Beads; janus.Beads = fake_beads(ins)
        try: self.assertEqual([k for k, _ in janus.decision_findings(self.cfg) if k.startswith("decision-missing")], [])
        finally: janus.Beads = saved

    def test_an_open_owner_question_is_listed(self):
        """Catches: a queued question for the owner that check never surfaces."""
        saved = janus.Beads; janus.Beads = fake_beads([])
        try:
            janus.cmd_ask(self.cfg, argparse.Namespace(question="Which branch?", options=[], recommend=None))
            self.assertIn("owner-question:1", dict(janus.decision_findings(self.cfg)))
            janus.cmd_answer(self.cfg, argparse.Namespace(id=1, answer="a first", decide=False))
            self.assertNotIn("owner-question:1", dict(janus.decision_findings(self.cfg)))
        finally: janus.Beads = saved


class SessionStart(unittest.TestCase):
    def test_the_decisions_index_is_injected_at_session_start(self):
        """Catches: a new session that never learns what is already decided (the index is not injected)."""
        import io, json, os
        from contextlib import redirect_stdout
        from unittest import mock
        d, cfg = project()
        janus.cmd_decide(cfg, ns("Keys live in the keychain"))
        saved = (janus.session_root, janus.sh)
        janus.session_root = lambda sid: d
        janus.sh = lambda *a, **k: (0, "(prime)", "")
        try:
            with mock.patch.dict(os.environ, {"JANUS_ROLE": ""}), mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "sid", "cwd": str(d)}))):
                buf = io.StringIO()
                with redirect_stdout(buf): janus.cmd_hook("SessionStart")
            out = buf.getvalue()
        finally: janus.session_root, janus.sh = saved
        self.assertIn("Decisions (read the record before deciding anything it touches)", out)
        self.assertIn("0001 Keys live in the keychain", out)

    def test_the_instruction_comes_first_and_each_part_is_labelled_and_capped(self):
        """F2: the one instruction is first (the model weights the start most), every part is labelled, and each is capped on its
        own so a long handoff or bd prime cannot push it or a later part past the cut."""
        import io, json, os
        from contextlib import redirect_stdout
        from unittest import mock
        d, cfg = project()
        janus.cmd_decide(cfg, ns("Keys live in the keychain"))
        (d / ".janus/HANDOFF.md").write_text("<!-- LIVE:BEGIN -->\nlive\n<!-- LIVE:END -->\n## Orchestrator notes\nh\n" + "x" * 20000)
        (d / ".janus/MEMORY.md").write_text("# Session memory\n\n## Facts\n- 2026-11-12 the cache lives in /var/c\n")
        saved = (janus.session_root, janus.sh)
        janus.session_root = lambda sid: d
        janus.sh = lambda *a, **k: (0, "P" * 9000, "")
        try:
            with mock.patch.dict(os.environ, {"JANUS_ROLE": ""}), mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "sid", "cwd": str(d)}))):
                buf = io.StringIO()
                with redirect_stdout(buf): janus.cmd_hook("SessionStart")
            out = buf.getvalue()
        finally: janus.session_root, janus.sh = saved
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith("[janus] You are the orchestrator of t"))
        for lab in ("## 1 HANDOFF.md", "## 2 MEMORY.md", "## 3 Decisions", "## 4 Owner rules (bd prime)"):
            self.assertIn(lab, text)
        self.assertIn("cut: read .janus/HANDOFF.md", text)     # the handoff was cut on its own
        self.assertIn("cut: run bd prime", text)               # so was bd prime (it no longer reaches the cut as a whole)
        self.assertIn("0001 Keys live in the keychain", text)  # a later part survived a long earlier one


class Superseded(unittest.TestCase):
    """F12: the injected decision list never shows a reversed decision as current. The failure caught: a model acting on a
    decision its own replacement superseded."""
    def test_a_superseded_decision_is_marked(self):
        d, cfg = project()
        janus.cmd_decide(cfg, ns("First choice"))
        janus.cmd_decide(cfg, ns("Second choice", supersedes=1))
        t = janus.session_text(cfg)
        self.assertIn("0001 First choice (superseded by 0002)", t)
        self.assertIn("0002 Second choice", t)
        self.assertNotIn("0001 First choice\n", t)   # never as if current


if __name__ == "__main__":
    unittest.main()
