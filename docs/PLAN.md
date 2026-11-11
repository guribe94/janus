# Orchestration skill that enforces itself (Claude Code and Codex)

## Context

On 2026-11-11 the orchestrator went idle from about 02:00 to 09:21 with nothing set to wake it.
- Five landing gates failed on a fixture it had removed itself.
- linux1 thrashed for 6.4 hours.
- 77 fleet events went unread.
- $119.50 of API spend produced 9 failed jobs out of 11.
- About 27 agents ran against a cap of about 6.

When it woke, it did operations itself and broke more things. The owner had said all of this 40+ times.

The root causes:
- Nothing enforced the role on the orchestrator.
- Nothing kept it awake.
- Instructions were never written down as rules.
- Monitoring waited for events instead of checking what exists.
- Agents were launched beyond capacity.

The owner's rulings:
- Cheap, quick and efficient: a skill that makes the orchestrator orchestrate properly.
- Generic for any project.
- Works with Claude Code and Codex.
- Best use of the available models.
- Safe scaling of agents on the local machine and on network machines over ssh, all orchestrated by one
  orchestrator. Scale to what the machines can safely run; caps (total and per machine) only when the owner sets them.
- Token-efficient.
- Keep every instruction.
- A message board for all agents.
- No unnecessary tests, checks or infrastructure.
- $100 of API on the new key, spent sparingly (most of it is meant for the project, not this); the subscription off.
- Herdr wake, and start a new orchestrator if the session is gone.
- The state repository goes to a bare repo on linux1 by default.
- Autonomy: stop orphans, stalls and thrashing jobs; re-queue failed gates; land green units; launch within safe
  capacity and any owner caps.

Verified facts this rests on:
- **Claude Code hooks:** Stop can block (`decision: block`). SessionStart fires after compaction (`source: compact`)
  and can add context. UserPromptSubmit can add context. PreToolUse can deny.
- **Codex 0.166:** `hooks`, `goals`, `plugins` and `multi_agent` are stable. Its hooks use the same event names and
  the same JSON output (PreToolUse deny, Stop block, SessionStart and UserPromptSubmit context). They are configured
  in `~/.codex/hooks.json` and must be trusted once with `/hooks`.
- **The machines:** Python 3.14+ on all three. `systemd-run --user` memory limits work with no sudo on linux1 and
  linux2. herdr runs both `claude` and `codex` agents.

## Standard tools first; `orch.py` is only thin glue

| Need | Standard tool | Why |
|---|---|---|
| Units, dependencies, what to launch next, owner instructions and rules kept for good | **beads (`bd`)**, MIT, Homebrew 1.3.4 | `bd ready --json` gives the work whose blockers are done; `bd update --claim`, `bd close`, `bd dep add`; every issue keeps an audit trail. `bd remember` stores a standing rule, and `bd prime` re-injects every rule into Claude Code and Codex through the hooks that `bd setup claude` / `bd setup codex` install. Syncs over the bare git repo on linux1 (`bd dolt push`). The orchestrator is its single writer on the Mac. |
| Job queues on each machine that survive outages | **pueue**, MIT | Per-machine daemon; first-in first-out groups with parallel limits; logs persist; `pueue status --json`. |
| Stop one job from thrashing a machine | **`systemd-run --user -p MemoryMax`** | The kernel caps the job, with no sudo; verified on both Linux hosts. |
| Remote agents that keep running through outages | **tmux** on the remote machine | Already installed on both hosts. |
| Panes a human can follow | **herdr**, with its skill | Already in use for Claude and Codex agents. |
| Scheduled watcher | **launchd** on the Mac; systemd user timers on Linux | Built in. |
| File transfer, state | **rsync**, **git** | Built in. |
| Secrets | **macOS keychain** | Built in. |
| Agent messages | the existing **`board.sh`** | Works on all three machines today. |

What remains custom is one small script, `orch.py`. It reads all of the above into a few lines of findings, applies
the capacity rules, and joins the tools in `launch`, `stop`, `collect` and `close`. That keeps tokens low: the model
reads only those lines.

## The skill: `orchestrating`, installed for both Claude Code and Codex (replaces `orchestrating-agent-fleets`)

**`SKILL.md`, one page:**
- The role: dispatch, monitor, verify, land, clean up, report. Agents do all the work.
- The cycle: `orch check`, act on every line, `orch ack`.
- The autonomy list.
- The forbidden list: no operations by hand.
- The token rules: read script output, not panes or logs; short briefs.

`install` symlinks it for both tools and merges the hook settings (see "Where it lives").

**`orch.py`: one file, Python standard library.** The project's settings are in `orch.toml`:
- hosts: the local machine and ssh hosts, with limits;
- per-agent and per-job resource figures;
- optional caps, total and per machine (none unless the owner sets them);
- providers on or off, with budgets;
- model routing;
- launch commands per runtime;
- gate result logs;
- board and state paths.

Commands:
- **`check`**: one line per problem, nothing when all is well. It reads the real state: herdr, the board summary,
  gate logs, host probes over ssh, git, and the spend ledger. It reports:
  - orphans, meaning any agent or job not owned by a live unit;
  - a stalled or silent agent, and a finished agent not collected or closed;
  - a host over its margins, and a host with room while work waits;
  - an unread gate failure, and the same test failing in two or more gates;
  - over an owner-set cap (total or per machine), or a disabled provider in use;
  - anything not committed and pushed;
  - unclassified owner instructions;
  - the watcher not running.
- **`capacity`**: free room per host, measured live (available memory, memory pressure, load against cores, disk),
  against the config's per-agent and per-job figures and margins.
  - **No agent cap by default.** The limit is what each machine can safely run. Optional caps, set only by the owner:
    a total across all machines, and one per machine.
  - **Multiple machines:** every host in `orch.toml` (this machine and any reachable over ssh) runs agents and jobs.
    `launch` spreads work across them by measured room, so no machine sits idle while work waits and none goes past
    its safety margins.
- **`launch <unit> --kind implement|review|audit --tier critical|normal [--host h]`**:
  - **Model:** the first model in that tier's ordered list whose provider is enabled and has budget. A review must use
    a different vendor than the implementer (Claude vs OpenAI vs DeepSeek).
  - **Host:** the host with room, locally or over ssh. Refused when none has any.
  - **Start:** that runtime's launch command (Claude Code or Codex in a herdr pane, `claude -p`, `codex exec`,
    opencode).
  - **Record:** the unit with host and pid. The brief gets the board rule and the agent rules.
- **`stop <unit>`** ends the agent by its recorded pid on its host and confirms it is gone. **`close <unit>`**: committed
  and pushed, pane closed, worktree and remote copy removed, board card closed.
- **`ack <problem> "<decision>"`** records the decision as a comment on the beads issue. **`register`** marks this
  session as the orchestrator. **`hook <event>`** is one handler for both tools.

**Hooks, the same handler for Claude Code and Codex.** Each acts only for the registered orchestrator session, plus a
small agent part.
- **SessionStart** (startup, resume, compact): inject the role card, the rules and `orch check` output.
- **UserPromptSubmit**: files the owner's message as a beads issue labelled `instruction`. It stays open, and `check`
  lists it, until it is closed as done or turned into a standing rule with `bd remember`. Rules are re-injected by
  `bd prime` at every session start, so nothing is said twice.
- **PreToolUse (shell)**: deny direct operations: `rm -r`, `kill`, in-place edits in code or worktrees, loops over
  worktrees, destructive ssh, `sparse-checkout`.
- **Stop**: block going idle while a problem is unacknowledged past its deadline.
- **Agents' Stop**: block until `REPORT.md` has a final verdict with exit codes.

**Wake, one mechanism for both tools:** launchd runs `orch.py watch` every 5 minutes. When a cycle is overdue or a
problem is open past its deadline, it prompts the orchestrator's herdr pane. If the pane is gone, it starts a new
orchestrator in herdr, but only when a provider is enabled.

**Handoff document: the orchestrator's working memory, kept current and used to reset its context.**

`.orch/HANDOFF.md` is one file. It replaces today's STATE.md CURRENT STATE and HANDOFF.md, which drifted out of date
(STATE.md last rewritten 2026-11-10 22:00, HANDOFF.md 2026-11-07). It has two parts.

1. **Live state.** `orch check` regenerates it on every run from the real state: hosts and their room; each unit's
   agent, host, model, state, age and report; the queue; spend; and open problems. Nobody edits it, so it can never
   be stale.
2. **Orchestrator notes,** written by the orchestrator and kept short (`check` flags it over about 150 lines):
   - the goal and scope;
   - for each unit: why it exists and what "done" means;
   - decisions and their reasons, latest first;
   - next steps;
   - things to watch.

   Owner rules are beads memories (`bd prime`), and the units are beads issues. The notes keep only what neither
   holds. Older notes move to an append-only `.orch/history.md`, which is never injected.

**Keeping it current is enforced, not remembered:**
- The orchestrator's Stop hook blocks going idle while the notes are older than the last unit change or the last
  owner instruction, or older than the cycle period while work is running. `check` reports "handoff stale" in the
  same cases.
- `orch launch`, `stop` and `close` record each change in that unit's beads issue, which keeps its audit trail. The
  orchestrator only writes the reasons and next steps in the notes.

**Referring to it regularly:**
- SessionStart (startup, resume, compact, clear) injects HANDOFF.md, `bd prime` (rules and workflow) and the `check`
  output. Nothing is injected on ordinary turns, to save tokens.
- The watcher's wake prompt is always "read .orch/HANDOFF.md, run orch check, act, update the notes".

**Context reset from the handoff, for Claude Code and Codex alike:**
- The watcher reads the orchestrator's context size from its transcript (configurable threshold).
- When it passes the threshold, the orchestrator is idle and the notes are fresh, the watcher asks it to finish the
  notes, waits for the Stop hook to pass, then sends `/clear` to its herdr pane.
- The SessionStart(clear) hook then reloads the handoff, the rules and the check output. The orchestrator carries on
  with a small context and nothing lost.
- The board is not part of this context. It remains the agents' channel.

**herdr keeps the work organized and visible.** Everything goes through herdr, following the herdr skill, so a human
can see the whole fleet at a glance.

- **Layout:**
  - One herdr workspace per project, labelled with the project name.
  - Fixed tabs, kept open: `orchestrator` (this session), `status` (`orch status` refreshing), `board` (the board
    summary), `watcher` (the watcher's log).
  - One tab per running unit, labelled `<unit> · <model> · <host>`. A remote agent's tab views it on its own machine
    (tmux attach or `pueue follow` over ssh), so its output is visible like a local one's and survives a dropped
    view.
- **Lifecycle:**
  - `orch launch` creates the tab, starts the agent with `herdr agent start` (named after the unit), and sends the
    brief with `herdr agent prompt`.
  - `orch stop` and `orch close` close that tab by pane id, after the agent has exited.
  - Nothing else creates or closes panes.
- **Monitoring through herdr:**
  - `orch check` reads `herdr agent list` / `get`: working, idle, done, blocked or unknown.
  - A pane with no live unit is reported as an orphan, and so is a unit with no pane.
  - A blocked agent is shown with its question from `herdr agent read --source detection`.
  - The orchestrator reads script output, not raw panes (token rule).
- **Waking the orchestrator:** the watcher's prompt and the context reset (`/clear`) go to the orchestrator's pane
  through `herdr agent prompt` / `send-keys`.
- **The skill points to the herdr skill** for CLI syntax instead of repeating it.

**Queues that keep working through network outages.**

The design rule: work on a machine belongs to that machine's own queue, not to an ssh session from the Mac.

- **Each machine runs its own queue daemon.** pueue (MIT, one static binary per user, no sudo) runs as a systemd user
  service on linux1 and linux2 (user lingering is already on, so it runs with nobody logged in) and as a launchd
  agent on the Mac.
  - Jobs and their logs persist on that machine, across ssh drops and network outages.
  - Each machine has one group per lane. The parallel limit is set from that machine's measured room, and
    `orch check` adjusts it as room changes.
  - Each Linux job is wrapped in `systemd-run --user --scope -p MemoryMax=…`, so the kernel stops a job that
    outgrows its share.
- **Remote agents run on their machine, not over a held ssh session.** Headless agents (`claude -p`, `codex exec`,
  opencode) run as queue tasks. Interactive agents run in a tmux session on that machine.
  - The herdr tab on the Mac only views it: `ssh -t host tmux attach -t <unit>`, or `pueue follow <id>`.
  - A network drop disconnects the view; the agent keeps working. On return, the tab reattaches.
- **Ownership by unit, not by connection.** A job or agent is stopped only by `orch stop <unit>`, never because a
  connection dropped. Orphans are things with no open unit issue in beads. This replaces today's "caller gone" rule,
  which kills work on any disconnect.
- **Queue ahead, so machines stay busy.** `orch launch` copies the unit's inputs to the chosen machine (worktree sync)
  and queues its jobs there, with landing gates at the highest priority. A machine that loses contact keeps working
  through what is already queued.
  - Outputs (reports, logs, results, the worktree diff) stay on that machine until collected.
- **When contact returns:**
  - `orch check` reports the host back, which jobs finished while away, and the results waiting.
  - `orch collect <unit>` pulls them back and records them.
  - If a host stays unreachable past a configured time, jobs it has not started are queued on another machine. Jobs
    already running are never duplicated. When the host returns, its unstarted copies are removed.
- **`check` reports:** host unreachable since when, and what is queued or running there; results waiting to collect;
  a machine with room while another has a backlog, so work is moved to it.

**Board:** the existing `board.sh` is reused. Its paths come from `orch.toml`, every launch adds the board rule, and
`check` reads its summary.

## Where it lives and how it is installed

- **Everything is in `/Users/user/code/orchestrator`**, its own git repository, which does not exist yet:
  - `skill/SKILL.md`;
  - `orch.py`;
  - `install`;
  - `launchd/` with the watcher plist;
  - `example.orch.toml`.
- **`install` links it into place**, following the existing pattern (Codex skills are symlinks):
  - `~/.claude/skills/orchestrating` → `/Users/user/code/orchestrator/skill`;
  - `~/.codex/skills/orchestrating` → the same folder;
  - `~/.local/bin/orch` → `/Users/user/code/orchestrator/orch.py` (`~/.local/bin` is on PATH).
- **The hooks** in `~/.claude/settings.json` and `~/.codex/hooks.json` call `orch hook <event>`. Each file is
  merged, not replaced. Codex hooks are trusted once with `/hooks`, which is the owner's step.
- **The launchd plist** is copied into `~/Library/LaunchAgents` (launchd does not follow symlinked plists reliably)
  and loaded.
- **The old skill:** `orchestrating-agent-fleets` is removed after the new skill works. herdr's own skill stays.
- **Project state lives in that project:** `orch.toml`, `.beads/` (units, instructions, rules and decisions) and
  `.orch/` (HANDOFF.md and history). This project's go in `gate-logs/orch-20261031/`.
- **Installed with Homebrew on the Mac:** `beads` and `pueue`. On the Linux hosts, pueue's static release binary goes
  in `~/.local/bin`, with no sudo.

## For this project (small)

- pueue (with `systemd-run --user` MemoryMax on Linux) replaces the queueing in `heavy.sh`, `run.sh`, `linux-test.sh`
  and the hand-off queue workers. Today's MemoryHigh throttling (what thrashed) and "caller gone" kills both go.
  The old scripts are retired once the drill passes.
- `orch.toml` for this project:
  - hosts: Mac, linux1, linux2, each running agents and jobs within measured safe capacity (linux2 stays within its
    /srv/app-fleet limits);
  - no agent cap unless the owner sets one;
  - providers: subscription off, Claude API on the new key with $100, OpenAI through Codex (ChatGPT plan), DeepSeek
    and free models through opencode;
  - routing: critical goes to Fable or Opus, review to the other vendor, normal to DeepSeek or Codex.
- The orchestration state directory goes into git, pushed to a bare repo on linux1.

## Build (efficient)

- **One Sonnet 5.5 agent on the API, capped at $15**: the right model for a one-page skill and one small script. It
  runs on the new key, stored once in the keychain and never printed; `fable-api.sh` gains a `sonnet` model option to
  launch it. It works in `/Users/user/code/orchestrator` and writes:
  - `SKILL.md`, `orch.py` and `install`;
  - the launchd plist;
  - the pueue setup for each machine (systemd user units on Linux);
  - this project's `orch.toml`.

  It reuses the working logic of today's launchers and probes, but not their code.
- **No separate review and no test suite.** I read the diff, and the practical check below decides whether it works.
- **A short practical check:**
  - each hook fires once, under Claude Code and under Codex (a denied `rm -r`, a blocked Stop, a captured
    instruction);
  - `check` finds a planted orphan;
  - `capacity` reports the three machines;
  - a launch on a full host is refused;
  - the watcher prompts the pane;
  - Stop is blocked by a stale handoff;
  - one forced `/clear` reloads the handoff, and the orchestrator resumes correctly from it;
  - the herdr workspace shows the fixed tabs plus one labelled tab per unit, including one remote agent over ssh;
  - closing a unit removes its tab;
  - an outage drill: queue jobs on linux1, cut its network for a few minutes (or stop the Mac's ssh), and confirm the
    jobs kept running; on return, `check` reports the results and `collect` pulls them back.

## Then (when the owner says resume)

Register this session, check the ten stopped worktrees for mutants, re-run the gates the fixture failed, and relaunch
work through `orch launch`.
