"""Findings as data (section 7, seam 1).

Every problem check reports is a Finding(key, text). Its family is the key up to the first ':'; FAMILIES is the registry of the
legal families and the one spec of how each is handled: urgent (the watcher prompts an idle orchestrator for it, and it holds a
context reset), ack_ttl (seconds an ack hides it; every ack also ends when its finding clears; 0: it cannot be acked). A key of
an unregistered family (a janus bug) is reported as an urgent `unregistered:<key>` finding (finding()), so a new key can never
silently become non-urgent, and one bad key cannot cost the pass its other findings (tests/test_fix50.py reads every key the
package can emit against the registry).
`janus check --json` serialises the findings with these properties; live_acks applies ack_ttl. It imports only context (the
bottom of the findings layer; the family modules beside it and check.py build on it)."""
from typing import NamedTuple
from janus.context import now

H = 3600


class Family(NamedTuple):
    doc: str
    urgent: bool = False
    ack_ttl: int = 4 * H
    recurs: bool = False   # the condition returns by nature (a host's load, new work in the repository): repeat acks earn no issue row


FAMILIES = {
    "age": Family("a lane job queued or running over its class age limit ([lanes] age)"),
    "backlog": Family("the checklist is not imported or changed since the import; ready items held: briefed units fill cannot place, items without briefs while no host has room"),
    "beads": Family("bd is unavailable: units cannot be read", urgent=True),
    "blocked": Family("a host's jobs wait in an admission that says no or is paused (the cause is named)", urgent=True, recurs=True),
    "board": Family("a board card is blocked, has a question (a project's own board), is gone or silent; a host's posts wait unsynced over 3 min (board:stale-sync)", urgent=True),
    "budget80": Family("a unit spent over [budget] warn_pct of its budget and its report is stale", urgent=True),
    "burner": Family("a CPU burner an agent started (or orphaned) outside the lanes on this machine", urgent=True),
    "cap": Family("more agents than the owner's total or per-host cap"),
    "decision-missing": Family("an owner instruction classified as a decision has no decision record", urgent=True),
    "direct": Family("an agent runs a build or test directly on a remote host, outside its lane", urgent=True),
    "context": Family("the orchestrator's context is over [project] reset_tokens and no reset was asked"),
    "disk": Family("a host is over its disk budget (tidy's last pass)"),
    "gate": Family("a gate failed in the last 12 h (its latest result)"),
    "gate-common": Family("the same failure in two or more gates: suspect the test or fixture"),
    "gate-setup": Family("every step of a gate failed at once: a setup problem, not the tests"),
    "git": Family("the project's repository is dirty, unpushed or has no upstream", recurs=True),
    "handoff": Family("the orchestrator notes are stale or too long, or the state folder has no HANDOFF.md; cannot be acked: write the notes", ack_ttl=0),
    "host": Family("a host is unreachable", urgent=True),
    "idle": Family("a host or lane has room while work waits"),
    "idle-backlog": Family("ready backlog items without briefs while hosts have room, or held by budget", urgent=True, ack_ttl=H),
    "idle-with-backlog": Family("ready backlog units with briefs would fit now (janus fill)", urgent=True),
    "instructions": Family("unclassified owner instructions in beads (an ack cannot clear it: close or remember each)", urgent=True, ack_ttl=0),
    "issue": Family("a finding key acked 3+ times with no issue row logged (every oddity is logged)"),
    "intent": Family("an open intent (janus intent) no unit works on and nothing moved for [intents] stale_h hours", urgent=True, ack_ttl=H),
    "known-failure": Family("a known failure with no open unit fixing it, or one that passes now"),
    "landing": Family("a finished DONE unit has no gate, or passed its gates and is not landed", urgent=True),
    "learned": Family("an agent's learned post awaits remember or reject"),
    "ledger": Family("a provider ledger is malformed, implausible or holds unreconciled runs", urgent=True),
    "localtest": Family("a test or build runs on this machine outside the lanes"),
    "machine": Family("a job or tmux session on a shared host with no project id (made before ids, or by hand): listed, never acted on"),
    "margin": Family("a host is over its disk or memory-pressure margins", urgent=True, ack_ttl=H // 2, recurs=True),
    "memory-long": Family("MEMORY.md holds more entries than [memory] cap_lines"),
    "memory-review": Family("a memory entry is older than [memory] review_days"),
    "memory-source": Family("a memory entry cites a path that is gone"),
    "memory-subject": Family("two memory entries share one subject"),
    "note": Family("an agent left a shared-file note on another agent's card (board.sh note)"),
    "old-rule": Family("jobs still wait under the old cap rule", urgent=True),
    "orchestrator": Family("the registered orchestrator's kind differs from janus.toml, or one the watcher started never registered"),
    "orphan": Family("a job, tmux session or agent pane with no open unit", urgent=True),
    "overlap": Family("two running units change the same file", urgent=True),
    "owner-question": Family("a question for the owner waits in .janus/owner-queue.md", urgent=True),
    "parked": Family("a parked unit's job can never finish, or its resume failed", urgent=True),
    "overrun": Family("a running job is far over its expected run time (its class's history on that host)"),
    "pressure": Family("a host's admission is paused by back-pressure", urgent=True, recurs=True),
    "provider": Family("a unit runs on a disabled provider"),
    "question": Family("an open board question", urgent=True),
    "quality": Family("a finished unit changed test files but named no failure each test catches (report.json)"),
    "related": Family("two units whose work meets (same file or module, branch, gate area or a learned post): janus introduce", urgent=True),
    "report-due": Family("a status report to the owner is due", urgent=True),
    "review": Family("a critical unit finished DONE with no review of its final code"),
    "rule-subject": Family("two standing rules (beads memories) on one subject"),
    "scheduler": Family("job-running work outside a host's janus lanes", urgent=True),
    "script": Family("a running script's file was changed after it started", urgent=True),
    "spend": Family("a provider's committed spend is at its cap or over 80/90 % of it, or an API process janus did not start", urgent=True),
    "stalled": Family("queued work with free slots, or an old queue that does not drain", urgent=True),
    "tab": Family("a tab janus opened for a unit is still open after the unit closed (janus tidy closes it)"),
    "test-signal": Family("a long gate/test step that has not failed in N runs: scope or trim it (deletion stays the owner's call)"),
    "thrash": Family("the watcher stopped and re-queued a job on a thrashing shared host"),
    "tidy": Family("what janus tidy can reclaim"),
    "unattended": Family("the unattended loop (autopilot, watcher fill) is off while work is open", ack_ttl=7 * 24 * H),
    "unit": Family("a unit is blocked, finished, or stopped by a limit without a verdict", urgent=True),
    "unregistered": Family("a finding whose family is not in the registry (a janus bug: register the family)", urgent=True),
    "waiting": Family("a live agent waits for its jobs' admission and pays for it", urgent=True, ack_ttl=0),
    "waste": Family("a run ended at its budget cap without a verdict", urgent=True),
    "waste-pause": Family("launch and fill are paused by the waste limit", urgent=True),
    "watcher": Family("the watcher is not running, or one of its steps keeps failing"),
}


class Finding(NamedTuple):
    key: str
    text: str

    @property
    def family(self): return family(self.key)
    @property
    def urgent(self): return FAMILIES[self.family].urgent
    @property
    def ackable(self): return FAMILIES[self.family].ack_ttl > 0

    def data(self):
        return {"key": self.key, "family": self.family, "text": self.text, "urgent": self.urgent, "ackable": self.ackable}


def family(key):
    """The registered family of a finding key; ValueError for a key no family covers."""
    f = key.split(":", 1)[0]
    if f not in FAMILIES: raise ValueError(f"finding {key!r}: family {f!r} is not in the registry (janus/findings/__init__.py)")
    return f


def finding(key, text):
    """A Finding. A key of an unregistered family becomes `unregistered:<key>`: shown and urgent, and the check goes on."""
    f = key.split(":", 1)[0]
    if f in FAMILIES: return Finding(key, text)
    return Finding(f"unregistered:{key}", f"{text} [janus bug: finding family {f!r} is not registered in janus/findings/__init__.py]")


def ack_ttl(key):
    """Seconds an ack of key lasts: its family's, 4 h for a key no family covers (an ack of any text is kept as before)."""
    return FAMILIES.get(key.split(":", 1)[0], Family("")).ack_ttl


def ackable(key): return ack_ttl(key) > 0


def repeat_is_issue(key):
    """True when repeated acks of key are a sign of something to fix. Not for a family whose condition returns by nature
    (recurs), nor for one that cannot be acked (rows 5, 6, 9, 10, 12 of a project's register: memory pressure on a host, a
    repository that new work dirties again, notes that only writing clears)."""
    n = key.split(":", 1)[0]
    if n == "issue": return False   # row 93: the detector's own findings: acks of issue:<key> raised issue:issue:<key>, without end
    f = FAMILIES.get(n, Family("")); return f.ack_ttl > 0 and not f.recurs


def live_acks(cfg, keys=None):
    """Acks still in force. keys: the findings seen now; an ack whose finding is gone is dropped (only a writer saves that)."""
    acks = {k: (v if isinstance(v, dict) else {"t": v}) for k, v in cfg.jget("acks.json").items()}
    return {k: v for k, v in acks.items() if now() - v["t"] < ack_ttl(k) and (keys is None or k in keys)}


__all__ = ["Family", "FAMILIES", "Finding", "family", "finding", "ack_ttl", "ackable", "repeat_is_issue", "live_acks"]
