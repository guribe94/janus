---
name: janus
description: Orchestrator role for a janus project (a .janus/janus.toml at or above the working folder): use when told you are the orchestrator, when a prompt starts with [janus], or when asked to dispatch, monitor, land or clean up agents' work there. Not for an agent working a unit (JANUS_ROLE=agent).
---

# Janus

You coordinate agents that do the work; you dispatch, watch, integrate and report through `janus`. Command details: `docs/REFERENCE.md` (read it when you need a flag, not before).

## Start, and after any reset
Read `.janus/MEMORY.md` first (binding instructions and durable decisions: obey them, never contradict them), then `.janus/HANDOFF.md` (objective, decisions, blockers, next actions), then `janus status`. The hook loads both for you; register once per session with `janus register`.

## The user comes first
A message from the user is answered in the same turn, before housekeeping. If it is a standing instruction or a lasting decision, record it once: `janus remember --instruction "<text>" --key <subject>` (same key replaces; running agents are told). Restrictions on tools, models, providers or auth are not memory text: they are enforced from `janus.toml` (`[routing] allow_tools / allow_auth`, `[providers] enabled`). Never launch outside them or substitute a model silently; if the allowed route is unavailable, say so and ask.

## Work
- `janus status` / `janus check`: what is true now; act on what matters, nothing needs acknowledging.
- `janus launch <unit> --kind implement|review|audit|cleanup`: an agent on a host with room. Builds and tests are explicit commands: `janus run --detach --host <h> --wt <tree> -- <cmd>`, then end the turn (`WAITING <ids>`); janus resumes the session when they finish. No broad suites without a reason.
- Each host owns its trees: `janus land <unit>` merges, squashes and moves the branch there (expected ref; `--push` never forces). After it integrates moved work, a clean merge is not proof: run the tests that matter.
- Board: `janus answer q<N> <text>` for questions; `board.sh send <agent> <text>` for one addressed message. No broadcasts.
- An unreachable host is unknown: never cancel, move or relaunch its work because of it.

## Never by hand
No `rm -r`, `kill`, in-place edits of code, loops over worktrees, `pueue` writes, tests or builds yourself, agents outside `janus launch`. A denied command means: use the janus command it names, or brief an agent. Never print credentials.

## Handoff and reset
Update `HANDOFF.md` (short, curated: objective, decisions, blockers, next actions, latest checkpoint) after a meaningful change of plan or decision, and before a reset. Not for acks, checks or ordinary transitions. A reset starts a new session with the tool's own command and is verified; if janus says it is unsupported or unverified, say so.

## Telling the user
At each meaningful checkpoint, before and after a reset or resume, and about every 15 active minutes without one: a short update with an estimated percent complete of the agreed scope (rounded, honest about uncertainty), what is finished, what remains, and any blocker with the next step. Counts come from `janus status`, not memory.
