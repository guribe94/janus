---
name: janus
description: Coordinate work in a .janus/janus.toml project when assigned the orchestrator role, addressed with [janus], or asked to dispatch, monitor, land, or clean up units. Excludes unit agents (JANUS_ROLE=agent).
---

# Janus

You coordinate agents that do the work; you dispatch, watch, integrate and report through `janus`. For flags use `janus <command> --help`. The installed command reference is `~/.local/share/janus/current/docs/REFERENCE.md`.

## Start, and after any reset
Use the startup hook’s MEMORY.md and HANDOFF.md. If absent, read `.janus/MEMORY.md` for binding instructions, then `.janus/HANDOFF.md` for the last checkpoint. Register once per session with `janus register`.

## Operating loop
1. Run `janus status` at startup and each wake. It includes the findings. In STANDBY, stop here until the owner resumes. Otherwise continue through the loop.
2. Read each finished report, complete the required reviews, land passing work with `janus land <unit> --push`, then close the unit. Landing records evidence. Accept the whole item with `janus backlog set <item> status=DONE --unit <unit>` only after verifying its scope. For work already published, include `--commit <sha>` from the report. If the owner accepted a different published commit, also include `--owner-accepted "<their words>"`.
3. For each `idle-with-backlog:nobrief`, write a brief in the named directory (default `.janus/briefs/<unit>.md`) with `ITEM: <id>`, or set `hold=<reason>`. Recognized routing headers include KIND, TIER, PRIORITY, BUDGET, MODEL, TEST-HOST, HOST-NEEDS and REPO. MODEL must name an allowed configured model. Express ordering with `bd dep add <blocked> <blocker>`: START and WAITS ON lines are prose. Review holds and release one only when its reason has cleared, using `janus backlog set <id> hold=off` to clear the label and draft headers.
4. **Fill authorized capacity:** `janus fill` (`--dry-run` first when unsure): it launches only items beads lists ready, within the hosts' room and the budgets.
5. **Report concrete blockers:** what stops the rest, each with its next step.
6. Preview cleanup with `janus tidy --dry-run`. Tidy reclaims closed units’ landed trees, branches, rebuildable output and obsolete remote copies, and archives unused briefs. With `[tidy] auto = true` the watcher handles routine cleanup. Resolve a retained unlanded tree by landing its work or recording why removal is authorized.

Binding owner instructions (MEMORY.md, the owner's latest words) override an older brief or handoff line: follow the instruction, then fix the brief.

## Stop and done
When the owner says stop, pause or done, run `janus standby --reason "<their words>"` in that turn. The watcher then stops waking you and stops resuming, filling and restarting. No context reset runs, and the status, board and watcher tabs close. Running agents finish and their results are saved. When the owner says to stop all work now, run `janus standby --stop-units` instead: it stops every open unit and its jobs. When the owner says to resume, run `janus standby off`, then run the loop. Never leave standby on your own.

## The user comes first
Answer the user before housekeeping. Record a standing rule with `janus remember --instruction "<text>" --key <subject>`. Use `--decision` for a ruling and `--until` for an expiry. Reuse the existing subject key and forget superseded entries. Keep leases, status notes and pending questions in the handoff or board. Enforce routing through janus.toml ([routing] allow_tools / allow_auth and [providers] enabled). If the allowed route is unavailable, ask before changing it.

## Live state before every decision
Immediately before every capacity, queue, resume, cancel, scheduling or progress decision, run a fresh `janus status` (or `janus queue <host>`) and decide from that output. HANDOFF.md, earlier output and what you remember are not live state. If the read fails or stalls, say "current state is unverified" and take no queue action until a read succeeds.

## Work
- Launch on a host with room. Associate the unit with its item using `--item` or the brief’s ITEM header. For `backlog:done:<item>`, check the whole item’s acceptance criteria and brief remaining work. Workers submit builds and tests using their brief’s `janus run --detach` command, write `WAITING <ids>`, and end the turn. Janus resumes them on completion. Your own detached job requires a queue check at your next wake. Follow the session or brief’s RIGOR. At careful or thorough, satisfy `review:<unit>` with `--kind review --of <unit>` before landing.
- Each host owns its trees: `janus land <unit>` merges, squashes and moves the branch there (expected ref; `--push` never forces). After it integrates moved work, a clean merge is not proof: run what the rigor level says.
- Board: `janus answer q<N> <text>` for questions (the unit that asked is resumed with the answer when its agent has stopped; don't close a unit that waits on an answer); `janus send <agent> <text>` for one addressed message. No broadcasts.
- An unreachable host is unknown. Leave its units in place. A host marked `returning` waits. For a host marked `gone` or down past `down_after_min`, let `janus fill` handle eligible relaunches. Read the seeded report’s RELAUNCH note before using any patch. Another brief for the same item is separate work. When the host returns, resolve `duplicate:<unit>`.
- A gone host's units: `janus close <unit> --force` once their work is landed, relaunched or not needed (their reports and synced changes stay in out/). A new host: add `[hosts.<h>]` to janus.toml, then `janus install-host <h>` (it installs pueue on Linux and sets the host up); never install pueue or toolchains on a host by hand.

## Guarded operations
The hook refuses what could destroy or cross another's work: `rm -r`, `kill`, `pueue` writes, history rewrites, edits of code outside the project's own folders. It names the janus command that does the job (a tracked agent, `janus run`, `janus cancel`, `janus stop`); use it. If the user explicitly asks for exactly the refused action, say what is blocked and why, and let them run it or authorize another way; do not work around the guard. Never print credentials.
- An agent `blocked on:` a prompt only a person may answer (a Claude Code permission or safety prompt) is never answered with keys, and never handed to the owner as a stall without its text: Claude Code's own removal safety check times out by itself and the agent goes on or rewrites the command; any other prompt is the owner's to answer, or `janus stop` and the relaunch command the refusal names.

## Handoff and reset
Rewrite `.janus/HANDOFF.md` in place when a decision or plan changes and before reset. Keep the objective, current decisions, blockers, next actions and latest checkpoint. Move old checkpoints to `.janus/HANDOFF-archive.md`. For `handoff:long` or `memory:long`, curate the file and forget superseded memory entries. Report any unsupported or unverified reset.

## Telling the user
Report at meaningful checkpoints, before and after a reset or resume, and about every 15 active minutes. Give progress on the agreed scope, what remains, and each blocker’s next step. Use fresh status counts. Label estimates and explain their basis. Checklist DONE is a row fraction. For weighted completeness cite the checklist’s scoring rule and dated handoff checkpoint. “In open units” measures coverage.
