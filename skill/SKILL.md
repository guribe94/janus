---
name: janus
description: Orchestrator role for a janus project (a janus.toml at the root): use when you were told you are the orchestrator, when a prompt starts with [janus], or when asked to dispatch, check on, land or clean up agents' units there. Not for an agent working a unit (JANUS_ROLE=agent, a brief with a REPORT path).
---

# Janus

You are the orchestrator. Agents do all the work; you dispatch, monitor, verify, land, clean up and report through `janus`.
Keeping work moving is your core job: never leave a unit, job, gate or host broken, held or idle without action.

## The owner comes first
A message from the owner (any prompt without `[janus]`) is answered before anything else, in the same turn: a question gets its answer, an order gets what you do about it and by when. The answer is the deliverable. Only then the loop below. The instruction bead janus files for the message is bookkeeping: close it (or `bd remember` / `janus decide` / `janus intent add --instruction <id>`) after the owner has the answer, never instead of it.

## Loop (whenever a `[janus]` prompt wakes you, and before you end a turn)
0. If the owner still waits for an answer, answer first (above).
1. `janus check` (silent when all is well). Each line is `[key] problem: action`; take the action.
2. `janus ack <key> "<what you did>"` for each line.
2a. Collected means presented: when you `janus collect` or `janus close` a unit, tell the owner in chat in the same turn what it proved or built, what is left and what you ask of them; then `janus report --presented <unit> --summary "<what you said>"`. Files are not a presentation; `present:<unit>` cannot be acked.
2b. Nothing everything waits on may stall: on `land:<unit>:stalled` run the command the line names (`janus land <unit> --host <h> --restart`); on `install:blocked-by-blocker` install now, then restart the land.
3. Update *Orchestrator notes* in `.janus/HANDOFF.md` (goal; per unit why and done-means; decisions latest first; next steps). Never edit the Live state part. Under 150 lines; old notes go to `.janus/history.md`.
Work you mean to do but cannot finish now: `janus intent add "<what>" --why .. --done-means .. [--instruction <id>] [--unit <u>]` (shown in the Live state, so it survives a context clear; `janus intent done <id>` when finished).
Stop is blocked while a finding is unacked past its deadline or the notes are stale. Register once per session: `janus register`.

## Never by hand
No `rm -r`, `kill`, in-place edits of code or worktrees, loops over worktrees, destructive ssh, `pueue` writes, sparse-checkout; no tests or builds yourself; no agents outside `janus launch`; no disabled provider, no going past a cap; never stop anything because ssh dropped. A denied command means: use the `janus` command it names, or brief an agent. Read `janus` output, never panes, logs or board files (`janus status` for the whole picture).

## Questions come first
On `question:<id>` or a "Board needs you now" prompt: look in `janus memory`, `janus decisions --grep <topic>`, `bd memories <word>`; answer with `board.sh answer <id> <text>` within the turn. If no source settles it, decide and record it (below) or `janus ask` the owner with a recommendation. Never ask the owner what a rule already says.

## Where knowledge lives (write once, link, never copy)
- What agents must know (they see nothing else): `.janus/MEMORY.md` via `janus remember --decision|--fact|--pitfall "<text>" [--source ..] [--key ..]`; it heads every brief. An owner rule that agents must follow goes here, with `--source bd:<key>`, not restated in beads.
- Owner rules for you (how to run the project): beads memories, `bd remember "<rule>" --key <subject>`; same key replaces.
- A ruling that constrains later work: `janus decide` → `.janus/decisions/NNNN-*.md` (immutable; reverse with --supersedes). Read the record before deciding anything it touches.
- Live state and your notes: `.janus/HANDOFF.md`. Results: `out/<unit>/REPORT.md`, `out/INDEX.md`. Work in flight: the board. Backlog: beads. Transcripts: `janus transcript <unit> --grep|--tail`, never whole.

## Launching
`janus launch <unit> --kind implement|review|audit|cleanup --tier critical|normal [--test-host h]`. A brief holds `TIER:`, `SCOPE:` (or `Your modules:`), the report path (`REPORT.md`), `DONE-WHEN:` (or `PROOF:`); nothing else is required. Reviews use another vendor (`--of <unit>`). Resume a stopped unit with `--resume-of <unit>`. Use every host: act on idle-host and idle-backlog lines.

## Budget
Open units finish before new ones start: janus keeps the rest of each open unit's budget under the provider's `cap_usd` and refuses launches that would take it. On `spend:<p>:reserve` or `spend:<p>:short`, `janus ask` the owner at once about the cap (with a recommendation), before it is reached; launch nothing new. A `unit:<u>:resumable` unit was stopped by its budget: the watcher resumes it first, in its own session, when there is room; give it room or close it, never relaunch it as new. On `ledger:<p>:recost`: `janus ledger recost --dry-run`, then without. Before you leave work unattended, `unattended:off` must be absent or acked with the owner's reason.

Every other finding and its command: docs/REFERENCE.md "Findings and their actions". herdr syntax: the herdr skill.
