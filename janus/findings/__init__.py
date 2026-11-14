"""Findings as data.

Every problem check reports is a Finding(key, text), derived from what is observed now: it appears while its condition holds and
is gone when the condition clears. There is nothing to acknowledge, no deadline and no stored "open since". Its family is the key
up to the first ':'; FAMILIES is the registry of the legal families: a one-line doc and whether the watcher prompts an idle
orchestrator for it (urgent). A key of an unregistered family (a janus bug) is reported as an urgent `unregistered:<key>`
finding, so one bad key can never cost the pass its other findings. `janus check --json` serialises the findings."""
from typing import NamedTuple


class Family(NamedTuple):
    doc: str
    urgent: bool = False


FAMILIES = {
    "age": Family("a lane job queued or running over its class age limit ([lanes] age)"),
    "backlog": Family("the checklist is not imported; ready items held: briefed units fill cannot place"),
    "beads": Family("bd is unavailable: units cannot be read", urgent=True),
    "blocked": Family("a host's jobs wait in an admission that says no or is paused (the cause is named)", urgent=True),
    "board": Family("a board card is blocked, has a question, is gone or silent; a host's posts wait unsynced over 3 min", urgent=True),
    "cap": Family("more agents than the owner's total or per-host cap"),
    "disk": Family("a host is over its disk budget (tidy's last pass)"),
    "git": Family("the project's repository is dirty, unpushed or has no upstream"),
    "host": Family("a host is unreachable (an unknown observation: nothing is moved, cancelled or relaunched for it)", urgent=True),
    "idle": Family("a host or lane has room while work waits"),
    "idle-with-backlog": Family("ready backlog units with briefs would fit now (janus fill)", urgent=True),
    "ledger": Family("a provider ledger is malformed, implausible or holds unreconciled runs", urgent=True),
    "machine": Family("a job or tmux session on a shared host with no project id: listed, never acted on"),
    "margin": Family("a host is over its disk or memory-pressure margins", urgent=True),
    "old-rule": Family("jobs still wait under the old cap rule", urgent=True),
    "orchestrator": Family("the registered orchestrator's kind differs from janus.toml, or one the watcher started never registered"),
    "orphan": Family("a job, tmux session or agent pane with no open unit", urgent=True),
    "parked": Family("a parked unit's job can never finish, or its resume failed", urgent=True),
    "pressure": Family("a host's admission is paused by back-pressure", urgent=True),
    "provider": Family("a unit runs on a disabled provider"),
    "scheduler": Family("job-running work outside a host's janus lanes", urgent=True),
    "spend": Family("a provider's committed spend is at its cap or over 80/90 % of it, or an API process janus did not start", urgent=True),
    "stalled": Family("queued work with free slots, or an old queue that does not drain", urgent=True),
    "tab": Family("a tab janus opened for a unit is still open after the unit closed (janus tidy closes it)"),
    "thrash": Family("the watcher stopped and re-queued a job on a thrashing shared host"),
    "tidy": Family("what janus tidy can reclaim"),
    "unit": Family("a unit is blocked or finished", urgent=True),
    "unregistered": Family("a finding whose family is not in the registry (a janus bug: register the family)", urgent=True),
    "waiting": Family("a live agent waits for its jobs' admission", urgent=True),
    "watcher": Family("the watcher is not running, or one of its steps keeps failing"),
}


class Finding(NamedTuple):
    key: str
    text: str

    @property
    def family(self): return family(self.key)
    @property
    def urgent(self): return FAMILIES[self.family].urgent

    def data(self): return {"key": self.key, "family": self.family, "text": self.text, "urgent": self.urgent}


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


__all__ = ["Family", "FAMILIES", "Finding", "family", "finding"]
