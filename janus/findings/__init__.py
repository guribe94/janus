"""Findings as data (section 7, seam 1).

Every problem check reports is a Finding(key, text). Its family is the key up to the first ':'; FAMILIES is the registry of the
legal families and the one spec of how each is handled: urgent (the watcher prompts an idle orchestrator for it, and it holds a
context reset), ack_ttl (seconds an ack hides it; every ack also ends when its finding clears; 0: it cannot be acked). A key of
an unregistered family is refused (finding() raises), so a new key can never silently become non-urgent or ackable.
`janus check --json` serialises the findings with these properties; live_acks applies ack_ttl. It imports only context (the
bottom of the findings layer; the family modules beside it and check.py build on it)."""
from typing import NamedTuple
from janus.context import now

H = 3600


class Family(NamedTuple):
    doc: str
    urgent: bool = False
    ack_ttl: int = 4 * H


FAMILIES = {
    "age": Family("a lane job queued or running over its class age limit ([lanes] age)"),
    "backlog": Family("the checklist is not imported, or changed since the last import"),
    "beads": Family("bd is unavailable: units cannot be read", urgent=True),
    "board": Family("a board card is blocked, has a question (a project's own board), is gone or silent", urgent=True),
    "budget80": Family("a unit spent over [budget] warn_pct of its budget and its report is stale", urgent=True),
    "burner": Family("a CPU burner an agent started (or orphaned) outside the lanes on this machine", urgent=True),
    "cap": Family("more agents than the owner's total or per-host cap"),
    "decision-missing": Family("an owner instruction classified as a decision has no decision record", urgent=True),
    "direct": Family("an agent runs a build or test directly on a remote host, outside its lane", urgent=True),
    "context": Family("the orchestrator's context is over [project] reset_tokens and no reset was asked"),
    "disk": Family("a host is over its disk budget (tidy's last pass)"),
    "gate": Family("a gate failed in the last 12 h (its latest result)"),
    "gate-common": Family("the same failure in two or more gates: suspect the test or fixture"),
    "git": Family("the project's repository is dirty, unpushed or has no upstream"),
    "handoff": Family("the orchestrator notes are stale or too long"),
    "host": Family("a host is unreachable", urgent=True),
    "idle": Family("a host or lane has room while work waits"),
    "idle-backlog": Family("ready backlog items without briefs while hosts have room, or held by budget", urgent=True),
    "idle-with-backlog": Family("ready backlog units with briefs would fit now (janus fill)", urgent=True),
    "instructions": Family("unclassified owner instructions in beads", urgent=True),
    "known-failure": Family("a known failure with no open unit fixing it, or one that passes now"),
    "landing": Family("a finished DONE unit has no gate, or passed its gates and is not landed", urgent=True),
    "learned": Family("an agent's learned post awaits remember or reject"),
    "ledger": Family("a provider ledger is malformed, implausible or holds unreconciled runs", urgent=True),
    "localtest": Family("a test or build runs on this machine outside the lanes"),
    "machine": Family("a job or tmux session on a shared host with no project id (made before ids, or by hand): listed, never acted on"),
    "margin": Family("a host is over its disk or memory-pressure margins", urgent=True, ack_ttl=H // 2),
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
    "pressure": Family("a host's admission is paused by back-pressure", urgent=True),
    "provider": Family("a unit runs on a disabled provider"),
    "question": Family("an open board question", urgent=True),
    "related": Family("two units whose work meets (same file or module, branch, gate area or a learned post): janus introduce", urgent=True),
    "report-due": Family("a status report to the owner is due", urgent=True),
    "rule-subject": Family("two standing rules (beads memories) on one subject"),
    "scheduler": Family("job-running work outside a host's janus lanes", urgent=True),
    "script": Family("a running script's file was changed after it started", urgent=True),
    "spend": Family("a provider's committed spend is at its cap or over 80/90 % of it, or an API process janus did not start", urgent=True),
    "stalled": Family("queued work with free slots, or an old queue that does not drain", urgent=True),
    "test-signal": Family("a long gate/test step that has not failed in N runs: scope or trim it (deletion stays the owner's call)"),
    "thrash": Family("the watcher stopped and re-queued a job on a thrashing shared host"),
    "tidy": Family("what janus tidy can reclaim"),
    "unit": Family("a unit is blocked, finished, or stopped by a limit without a verdict", urgent=True),
    "waiting": Family("a live agent waits for its jobs' admission and pays for it", urgent=True, ack_ttl=0),
    "waste": Family("a run ended at its budget cap without a verdict", urgent=True),
    "waste-pause": Family("launch and fill are paused by the waste limit", urgent=True),
    "watcher": Family("the watcher is not running"),
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
    """A Finding, refused unless its family is registered."""
    family(key); return Finding(key, text)


def ack_ttl(key):
    """Seconds an ack of key lasts: its family's, 4 h for a key no family covers (an ack of any text is kept as before)."""
    return FAMILIES.get(key.split(":", 1)[0], Family("")).ack_ttl


def ackable(key): return ack_ttl(key) > 0


def live_acks(cfg, keys=None):
    """Acks still in force. keys: the findings seen now; an ack whose finding is gone is dropped (only a writer saves that)."""
    acks = {k: (v if isinstance(v, dict) else {"t": v}) for k, v in cfg.jget("acks.json").items()}
    return {k: v for k, v in acks.items() if now() - v["t"] < ack_ttl(k) and (keys is None or k in keys)}


__all__ = ["Family", "FAMILIES", "Finding", "family", "finding", "ack_ttl", "ackable", "live_acks"]
