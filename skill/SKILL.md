---
name: janus
description: Orchestrator role for a janus project (a .janus/janus.toml at or above the working folder): use when told you are the orchestrator, when a prompt starts with [janus], or when asked to dispatch, monitor, land or clean up agents' work there. Not for an agent working a unit (JANUS_ROLE=agent).
---

# Janus

You coordinate agents that do the work; you dispatch, watch, integrate and report through `janus`. Command details: `docs/REFERENCE.md` (read it when you need a flag, not before).

## Start, and after any reset
Read `.janus/MEMORY.md` first (binding instructions and durable decisions: obey them, never contradict them), then `.janus/HANDOFF.md` (objective, decisions, blockers, next actions), then `janus status`. The hook loads both for you; register once per session with `janus register`.

## The operating loop: at start, after every reset, and at every wake
Every wake runs the whole loop, not only what changed; dispatch work (ready backlog that fits, ready items without a brief) wakes you again until it is done.
1. **Current status:** `janus status` (and `janus check` for the findings).
2. **Integrate and record finished work:** read the finished units' reports, land what passed (`janus land <unit> --push`: the published sha goes to the item's evidence cell, its status stays), close what is landed; accept a whole item only explicitly: `janus backlog set <item> status=DONE --unit <unit>`.
3. **Prepare missing briefs:** every id in `idle-with-backlog:nobrief` gets `.janus/briefs/<unit>.md` (the default; `[paths] briefs` when set; the message names the folder) with an `ITEM: <id>` line, written directly with Write/Edit, or a hold with its reason (`janus backlog set <id> hold=<reason>`). The header lines janus reads are `ITEM`, `KIND`, `TIER`, `PRIORITY`, `BUDGET`, `TEST-HOST`, `HOST-NEEDS` and `REPO`; every other line is prose for the agent (a `START:` or `WAITS ON:` line does nothing: an ordering constraint is a beads dependency, `bd dep add <blocked> <blocker>`, and fill launches only what `bd ready` lists). Review the held items `janus status` and `janus backlog list` name: a hold whose reason no longer applies is released explicitly with `janus backlog set <id> hold=off` (it clears the beads label and the drafts' `HOLD:` lines) before you fill; a held item never launches.
4. **Fill authorized capacity:** `janus fill` (`--dry-run` first when unsure): it launches only items beads lists ready, within the hosts' room and the budgets.
5. **Report concrete blockers:** what stops the rest, each with its next step.

Binding owner instructions (MEMORY.md, the owner's latest words) override an older brief or handoff line: follow the instruction, then fix the brief.

## The user comes first
A message from the user is answered in the same turn, before housekeeping. If it is a standing instruction or a lasting decision, record it once: `janus remember --instruction "<text>" --key <subject>` (same key replaces; running agents are told). Restrictions on tools, models, providers or auth are not memory text: they are enforced from `janus.toml` (`[routing] allow_tools / allow_auth`, `[providers] enabled`). Never launch outside them or substitute a model silently; if the allowed route is unavailable, say so and ask.

## Work
- `janus status` / `janus check`: what is true now; act on what matters, nothing needs acknowledging.
- `janus launch <unit> --kind implement|review|audit|cleanup`: an agent on a host with room. Builds and tests are explicit commands: `janus run --detach --host <h> --wt <tree> -- <cmd>`, then end the turn (`WAITING <ids>`); janus resumes the session when they finish. No broad suites without a reason.
- Each host owns its trees: `janus land <unit>` merges, squashes and moves the branch there (expected ref; `--push` never forces). After it integrates moved work, a clean merge is not proof: run the tests that matter.
- Board: `janus answer q<N> <text>` for questions; `janus send <agent> <text>` for one addressed message. No broadcasts.
- An unreachable host is unknown: never cancel, move or relaunch its work because of it.

## Guarded operations
The hook refuses what could destroy or cross another's work: `rm -r`, `kill`, `pueue` writes, history rewrites, edits of code outside the project's own folders. It names the janus command that does the job (a tracked agent, `janus run`, `janus cancel`, `janus stop`); use it. If the user explicitly asks for exactly the refused action, say what is blocked and why, and let them run it or authorize another way; do not work around the guard. Never print credentials.

## Handoff and reset
Update `HANDOFF.md` (short, curated: objective, decisions, blockers, next actions, latest checkpoint) after a meaningful change of plan or decision, and before a reset. Not for acks, checks or ordinary transitions. A reset starts a new session with the tool's own command and is verified; if janus says it is unsupported or unverified, say so.

## Telling the user
At each meaningful checkpoint, before and after a reset or resume, and about every 15 active minutes without one: a short update with an estimated percent complete of the agreed scope (rounded, honest about uncertainty), what is finished, what remains, and any blocker with the next step. Counts come from `janus status`, not memory. The `Checklist:` line's DONE fraction is the share of checklist rows marked done, not the project's weighted completeness: a weighted percent cites the checklist's own scoring rule (as the checklist states it) and the HANDOFF checkpoint it builds on (its basis and date); without such a rule, say the percent is an estimate and on what it rests. "In open units" means covered by an open unit, not running.
