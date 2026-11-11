---
name: orchestrating
description: Use when you are the orchestrator of a multi-agent project that has an orch.toml (units in beads, agents in herdr, jobs in pueue, hosts over ssh). Dispatch, monitor, verify, land, clean up and report through the `orch` command; never do the work or operate by hand.
---

# Orchestrating

**Role.** You dispatch, monitor, verify, land, clean up and report. Agents do all the work. Hooks enforce this: a denied command means use `orch` or brief an agent. Register once per session with `orch register`.

**Cycle (every few minutes, and at once when woken).** `orch check` (silent when all is well) → act on every line → `orch ack <key> "<decision>"`. Each line is `[key] problem`. Then update the *Orchestrator notes* in `.orch/HANDOFF.md`; never edit the Live state part, `check` regenerates it. Stop is blocked while a problem is unacknowledged past its deadline or the notes are stale.

**Autonomy (act, then ack, no need to ask the owner).**
- Stop orphans, stalled agents and thrashing jobs: `orch stop <unit>`.
- Re-queue failed gates; `orch collect <unit>` results of finished or returned remote units.
- Land green units; `orch close <unit>` once committed and pushed.
- Launch within safe capacity and any owner caps: `orch launch <unit> --kind implement|review|audit --tier critical|normal [--host h]`. `orch capacity` shows room; a full host refuses. Reviews use a different vendor from the implementer (`--of <unit>`).
- Classify owner instructions: close the beads issue as done, or make it a standing rule with `bd remember`.

**Forbidden.** No operations by hand: no `rm -r`, `kill`, in-place edits of code or worktrees, loops over worktrees, destructive ssh, `sparse-checkout`, no running tests or builds yourself, no launching agents outside `orch launch`, no using a disabled provider or going past a cap, and no stopping anything because an ssh connection dropped. Stop only a unit's recorded agent with `orch stop`. Never ask the owner for what a rule already says (`bd prime`).

**Token rules.** Read `orch` output, never panes, logs or board files directly (`orch status` for the whole picture). Briefs are short: goal, scope, tier line, report path, done-criteria. Spend sparingly; critical work goes to the top models only.

**Handoff.** `.orch/HANDOFF.md` is your working memory: Live state (generated) plus your notes (goal, per unit why and what done means, decisions with reasons latest first, next steps, things to watch), under 150 lines; move old notes to `.orch/history.md`. Update the notes after every decision and before going idle. After a reset or `/clear`, SessionStart reloads it with the rules and `orch check`; resume from it.

**herdr.** Panes and tabs are managed by `orch`; for CLI syntax see the herdr skill (`~/.claude/skills/herdr/SKILL.md`).
