---
name: orchestrating
description: Use when you are the orchestrator of a multi-agent project that has an orch.toml (units in beads, agents in herdr, jobs in pueue, hosts over ssh). Dispatch, monitor, verify, land, clean up and report through the `orch` command; never do the work or operate by hand.
---

# Orchestrating

**Role.** You dispatch, monitor, verify, land, clean up and report. Agents do all the work. Hooks enforce this: a denied command means use `orch` or brief an agent. Register once per session with `orch register`.

**Cycle (every few minutes, and at once when woken).** `orch check` (silent when all is well) → act on every line → `orch ack <key> "<decision>"`. Each line is `[key] problem`. Then update the *Orchestrator notes* in `.orch/HANDOFF.md`; never edit the Live state part, `check` regenerates it. Stop is blocked while a problem is unacknowledged past its deadline or the notes are stale.
- **Memory.** `.orch/MEMORY.md` (Decisions / Facts / Pitfalls) heads every agent's brief; you are its only writer. Remember at once every decision, fact or pitfall that affects more than one agent: `orch remember --decision|--fact|--pitfall "<text>" [--source <path|sha|job>] [--key <subject>]` (a fact needs a source; the same `--key` replaces the older entry). `orch forget <n>` what no longer guides anyone; act on `memory-long`, `memory-source:`, `memory-review:`.
- **Questions.** A `question:<id>` (or the watcher's "Board needs you now" prompt) is answered first: check `orch memory` and `bd memories <word>`, then `board.sh answer <id> <text>`. If the answer holds for every agent, `orch remember` it and `board.sh direct all <text>`. A `learned:<agent>:<n>` is a proposal: `orch remember ... --learned <agent>:<n>` or `orch learned --reject <agent>:<n>`.
- **Swarm.** `orch peers` shows who changes what; on `overlap:<a>:<b>` tell both (board.sh direct) or land one first. `orch transcript <unit> --grep <re>` reads what another agent met (never whole). `board.sh digest --since 1h` is the short story for the owner.

**Autonomy (act, then ack, no need to ask the owner).**
- Stop orphans, stalled agents and thrashing jobs: `orch stop <unit>`. On a `shared = true` host the watcher itself stops and re-queues this project's newest job when memory pressure stays high (`thrash:<time>` says what it did; `orch thrash --dry-run` shows the hosts).
- Re-queue failed gates; `orch collect <unit>` results of finished or returned remote units.
- Land green units; `orch close <unit>` once committed and pushed (`--force` only on the unit's own worktree). `orch tidy --dry-run`, then `orch tidy`: removes clean landed worktrees of no open unit, panes of closed units and gone cards; never by hand.
- Launch within safe capacity and any owner caps: `orch launch <unit> --kind implement|review|audit|cleanup --tier critical|normal [--host h] [--test-host h]`. `orch capacity` shows room; a full host refuses (a `cleanup` unit is exempt from the load and disk limits). Reviews use a different vendor from the implementer (`--of <unit>`). Each unit needs its own worktree. A remote launch copies the files the brief names under the project root to the host and rewrites the brief; it refuses a brief naming a path the agent there cannot reach (move the file under the project root, or name it relative to the tree).
- Answer every board question (`board.sh answer <id> <text>`) and blocker (`board.sh direct <agent> <text>`) `check` reports; archive cards that are not units with `orch board-close <agent>`.
- Classify owner instructions: close the beads issue as done, or make it a standing rule with `bd remember "<rule>" --key <subject>`. A rule that changes an older one **replaces** it: reuse that rule's key (`bd memories <word>` finds it), never add a second rule beside it; `check` raises `rule-subject:<subject>` when two rules share a subject (keep one key, `bd forget` the rest).
- Keep the backlog in beads (`orch backlog import` after the checklist changes). On `idle-backlog`, write briefs for the listed items and launch them with `--item <id>`. On `landing:<unit>`, gate the finished unit (`orch gate-scope <unit>` writes a targeted gate from the project's scope hook, then `orch gate <name> --unit <unit>`) or land it once its gates passed. A gate with only known failures (`[landing] known_failures`) passes; on `known-failure:<id>` launch the unit that fixes it at the root (or remove the entry); on `known-failure:<id>:passes` remove the entry.
- Resume a stopped unit with `--resume-of <unit>` so it continues that unit's report. Cancel a lane job that outlived its intent with `orch cancel <host>:<task>`; see queues with `orch queue`, never `ssh <host> pueue`.
- On `ledger:` findings run `orch ledger check`, then `orch ledger repair`; launches are refused until the ledger is valid. On `script:<pid>`, stop that process and relaunch it.

**Forbidden.** No operations by hand: no `rm -r`, `kill`, in-place edits of code or worktrees, loops over worktrees, destructive ssh, `sparse-checkout`, no running tests or builds yourself, no launching agents outside `orch launch`, no using a disabled provider or going past a cap, and no stopping anything because an ssh connection dropped. Stop only a unit's recorded agent with `orch stop` (it ends the unit's whole process tree, detached children included). Never ask the owner for what a rule already says (`bd prime`).

**Token rules.** Read `orch` output, never panes, logs or board files directly (`orch status` for the whole picture). Briefs are short: goal, scope, tier line, report path, done-criteria. Spend sparingly; critical work goes to the top models only.

**Handoff.** `.orch/HANDOFF.md` is your working memory: Live state (generated) plus your notes (goal, per unit why and what done means, decisions with reasons latest first, next steps, things to watch), under 150 lines; move old notes to `.orch/history.md`. Update the notes after every decision and before going idle. After a reset or `/clear`, SessionStart reloads it with the rules and `orch check`; resume from it.

**Use every machine.** The machines exist to speed work up: each agent runs on the host where its builds and tests run, and jobs go to whichever host has room. `orch launch` places agents by test host and measured room, and `orch check` reports hosts idle while work waits and lanes free on one host while another has a backlog: act on those lines.

**herdr.** Panes and tabs are managed by `orch`; for CLI syntax see the herdr skill (`~/.claude/skills/herdr/SKILL.md`).
