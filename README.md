# orch: an orchestrator that is held to its role

`orch` is a small toolkit for running a multi-agent project (several AI coding agents, on one or more machines) from one
"orchestrator" AI session. It is one Python file (`orch.py`, standard library only, Python 3.11+), a skill
(`skill/SKILL.md`), an installer and a launchd watcher. It is glue over standard tools: beads (`bd`), pueue, tmux, herdr,
ssh, rsync and git.

## 1. What it is, and the problem it solves

An orchestrator session is supposed to dispatch work, monitor it, verify, land, clean up and report. In practice one went
idle for hours with nothing to wake it, left orphan agents and jobs running, did operations by hand and broke more things,
launched far more agents than the machines could carry, let one machine thrash, and was told the same instructions many
times (the full story is in `docs/PLAN.md`). Reminders in a prompt did not fix it.

`orch` makes the role enforced by hooks and scripts instead of reminders:

- hooks deny hand operations (`rm -r`, `kill`, in-place edits) and refuse to let the orchestrator go idle with open problems;
- a watcher wakes the orchestrator, and restarts it if its pane is gone;
- `orch check` reads the real state (hosts, jobs, panes, gates, git) and prints one line per problem;
- `orch launch` measures free room and refuses when a machine is full;
- owner instructions are filed in beads and stay open until classified, so they are not lost.

```
 orchestrator session (Claude Code or Codex)
     |  hooks: SessionStart, UserPromptSubmit, PreToolUse, Stop  -> `orch hook <event>`
     v
   orch  <---- orch.toml (hosts, providers, routing, paths)
     |-- beads (bd): units, owner instructions, standing rules, decision log
     |-- pueue on each machine: queues and agent tasks, survive ssh drops
     |-- tmux on remote machines: interactive agents
     |-- herdr panes: one tab per unit, plus status / board / watcher tabs
     |-- board.sh: the message board agents post to (your script)
     '-- watcher (launchd, every 5 min): `orch watch` wakes the orchestrator
```

The board script (`paths.board`) and your briefs belong to your project; they are not part of this repository.

## 2. Install and uninstall

```
./install --dry-run        # prints every change, changes nothing
./install                  # run from a directory with an orch.toml (or pass --project DIR)
```

Options: `--dry-run`, `--project DIR`, `--no-hosts` (skip the remote machines), `--hosts-only` (only set up pueue on the
hosts in orch.toml).

What `install` changes:

| Where | Change |
|---|---|
| `~/.claude/skills/orchestrating`, `~/.codex/skills/orchestrating` | symlinks to this repo's `skill/` |
| `~/.local/bin/orch` | symlink to `orch.py` (make sure `~/.local/bin` is on PATH) |
| `~/.claude/settings.json`, `~/.codex/hooks.json` | adds `~/.local/bin/orch hook <event>` for SessionStart, UserPromptSubmit, PreToolUse (matcher `Bash\|Edit\|Write\|MultiEdit\|NotebookEdit\|apply_patch`) and Stop. Existing keys and hooks are kept; a backup `*.bak-orch-<time>` is written first |
| `~/Library/LaunchAgents/com.orch.watch.plist` | copied (not linked) and loaded with `launchctl`; runs every 300 s |
| `~/.config/orch/projects` | the project directory is added; the watcher runs `orch watch` in each listed project |
| Homebrew | `brew install beads pueue`, `brew services start pueue` |
| each ssh host in orch.toml | downloads pueue's static binaries into the host's `bin`, writes and enables a user systemd service `pueued`; no sudo |

An existing path that is not our link is skipped, never overwritten. A failing step prints "failed (continuing)".
Two details in the host step: `arch` must be `aarch64` or `x86_64`, and a host named `linux2`, or one whose `bin` starts
with `/srv/`, keeps its pueue files under `/srv/app-fleet/tools` (a layout specific to the original project).

**Codex needs one manual step:** open Codex and run `/hooks` to trust the new hooks. Until then they do not run.

**Uninstall.** There is no uninstall script. By hand:
1. `launchctl bootout gui/$(id -u)/com.orch.watch`, then delete the plist and the project line in `~/.config/orch/projects`;
2. remove the entries whose command ends in `orch hook <event>` from `~/.claude/settings.json` and `~/.codex/hooks.json`
   (or restore the `.bak-orch-*` backup);
3. delete the three symlinks above;
4. optionally `brew uninstall beads pueue`, and on remote hosts `systemctl --user disable --now pueued`.

## 3. Setting up a project

Put an `orch.toml` in the project root (`orch` finds it from any subdirectory; paths are relative to it). Start from
`example.orch.toml`. The sections:

**`[project]`**: `name`, `beads` (the directory holding `.beads/`; run `bd init` there first), `state` (default `.orch`),
and timings in minutes: `cycle_min` (a check/ack cycle is overdue after this, default 15), `deadline_min` (an
unacknowledged problem blocks Stop after this, 30), `silent_min` (30), `reset_tokens` (context size that triggers a
reset, 150000).

**`[paths]`**: `board` (script with `post/read/summary/rule/close`), `gate_log` (lines like
`2026-11-11 09:00:00 name EXIT_x=0 ... RESULT: PASS|FAIL`), `briefs` (`<briefs>/<unit>.md`), `out`
(`<out>/<unit>/REPORT.md`), `worktrees` (`<worktrees>/<unit>` is the unit's working directory if it exists).

**`[herdr]`** `workspace` and **`[orchestrator]`** `kind`/`args`: the herdr workspace label, and what the watcher starts
when the orchestrator pane is gone.

**`[hosts.<name>]`**: one per machine. `local = true` for this one; others have `ssh`, `arch`, `root` (where synced
worktrees, briefs and outputs live) and `bin`. Capacity figures: `ram_gb`, `cores`, `reserve_gb` (kept free),
`agent_gb` (per agent), `job_gb` (per pueue job), `min_disk_gb`. Optional `memory_max` (systemd-run limit) and
`pueue_config`.

**`[caps]`** (optional, only when the owner wants them): `total = 6`, and `[caps.host] server = 2`. With no caps the limit
is what each machine can safely hold.

**`[providers.<name>]`**: `enabled`, and optionally `cap_usd`, `ledger` (a TSV: ts, unit, cost or `running`, budget, run
id) and `budget_per_unit`. A provider is usable while `cap_usd` minus spend (including budgets of running rows) is at
least `budget_per_unit`.

**`[models.<name>]`**: `provider`, `vendor`, `kind`. `kind = "api"` is a headless launcher run as a pueue task
(`launcher` is called as `<unit> <brief> <cwd> <budget> <model>`; for a remote host `api_id` names the model).
`claude`, `codex` and `opencode` are agents in a herdr pane (in tmux on remote hosts); `args` are extra command-line
arguments. `hosts` limits where a model may run.

**`[routing]`**: ordered lists per tier: `critical`, `normal`, and `review`. The first model whose provider is enabled
and within budget wins. A review skips models of the implementer's vendor.

Then, in a session that will be the orchestrator:

```
$ orch register
registered 5c2e...        # also creates the status, board and watcher tabs in the herdr workspace
```

If the session id is not known yet, it prints `registered (pending: ...)` and the next hook event from the session claims
it (within 10 minutes). Hooks act only for the registered session; any other session is left alone.

## 4. Day to day: the cycle

The cycle is **check, act, ack, notes**. Do it every few minutes and whenever woken.

**`orch check`**: silent when all is well; otherwise one `[key] problem` line each. It also rewrites the Live state part of
`.orch/HANDOFF.md`.
```
$ orch check
[unit:parser:finished] parser finished (done; VERDICT: DONE); collect/close or relaunch
[gate:nightly@09:12:44] gate nightly FAILED at 09:12:44 (x y); re-queue or classify
[idle:server] host server has room for 2 but 3 unit(s) are ready: orch launch
```
Options: `--brief` (at most 8 lines), `--escalate` (only problems open longer than `deadline_min`).
What it reports: unreachable hosts and what was on them; hosts over their margins; idle room with ready units; orphan
pueue jobs, tmux sessions (`orch-*`) and herdr agents that belong to no open unit; blocked, finished or silent units;
disabled providers in use; caps exceeded; spend at a provider's cap; failed gates (last 12 hours) and the same test failing
in two or more gates; uncommitted or unpushed git work; unclassified owner instructions; a stale handoff; the watcher not
running (macOS).

**`orch capacity`**: room per host, measured live.
```
$ orch capacity
laptop room=2 avail=9.4G load=2.10 psi=- disk=80G agents=1
server room=0 avail=3.1G load=1.20 psi=0.00 disk=60G agents=2 FULL: memory 3.1G free, reserve 3G
```

**`orch launch <unit> --kind implement|review|audit [--tier critical|normal] [--host h] [--model m] [--brief f] [--cwd d] [--budget usd] [--of unit] [--dry-run]`**
```
$ orch launch parser --kind implement --tier normal
launch parser: model=codex host=laptop cwd=/proj/worktrees/parser
launched parser on laptop via herdr
```
It picks the model from `[routing]`, the host with the most room (or `--host`), syncs the worktree and brief to a remote
host with rsync, starts the agent, records the unit as a beads issue and posts to the board. It refuses with
`orch: refused, no host has room (...)` when nothing fits. The unit name must match `[a-z][a-z0-9_-]{0,31}` and the brief
`<briefs>/<unit>.md` must exist. For a review, `--of <unit>` makes it avoid the implementer's vendor.

**`orch stop <unit>`**: ends that unit's recorded agent (pueue kill, tmux kill-session, or its pid) and waits until it is
gone.
```
$ orch stop parser
stop parser (herdr on laptop)
stopped
```

**`orch collect <unit>`**: for a remote unit, rsyncs back the worktree and the outputs, and records API cost in the ledger.
Prints `collected` (or `local unit: nothing to collect`).

**`orch close <unit> [--force] [--reason text]`**: refuses if the worktree has uncommitted or unpushed work (unless
`--force`); then closes the pane, removes the remote copy, closes the board card and the beads issue.
```
$ orch close parser
close parser
closed
```

**`orch ack <key> "<decision>"`**: acknowledges a problem for 4 hours and records the decision as a beads comment (on the
unit's issue for `unit:<name>:...` keys, otherwise on an `orch: decisions` issue).
```
$ orch ack unit:parser:finished "collected and closed, gate re-queued"
acked unit:parser:finished
```

**`orch status`**: prints the Live state (hosts, units, queue, spend, problems) without counting as a cycle.

**Notes.** After each decision, update the *Orchestrator notes* part of `.orch/HANDOFF.md` (see section 6).

## 5. What the hooks enforce, and why

One handler, `orch hook <event>`, serves both Claude Code and Codex (same event names and JSON). It only acts for the
registered orchestrator session; agents started by `orch launch` run with `ORCH_ROLE=agent` and get only the agent rule.

| Event | Orchestrator | Why |
|---|---|---|
| SessionStart | injects HANDOFF.md, `bd prime` (standing rules), and the `orch check` output | a fresh or cleared session resumes without being re-told |
| UserPromptSubmit | files the prompt as a beads issue labelled `instruction` (except the watcher's wake prompt) | nothing the owner says is lost; `check` lists it until closed or turned into a rule |
| PreToolUse | denies shell commands matching: `rm -r`, `kill`/`pkill`/`killall`, `sed -i`/`perl -i`, `sparse-checkout`, `for`/`while` loops mentioning worktrees, `ssh` with a destructive command. Commands starting with `orch`, `bd`, `pueue`, `herdr` or `board.sh` pass. File edits are allowed only under the state dir, the briefs dir and `orch.toml`; `apply_patch` is always denied | the orchestrator orchestrates; changes go through `orch` or an agent |
| Stop | blocks going idle while a problem is unacknowledged past `deadline_min`, or the handoff notes are stale | keeps it working and the notes current. After 3 blocks in a row it lets go, so it cannot loop forever |

Agents: their Stop is blocked until `REPORT.md` (path in `ORCH_REPORT`) has line 1 no longer `VERDICT: PENDING`, a final
`VERDICT: DONE|BLOCKED|FAILED` line, and a mention of exit codes.

A denial reads: "Orchestrators do not operate by hand (...). Use orch launch/stop/collect/close, or brief an agent."

## 6. Handoff, instructions, rules and the board

**Handoff.** `.orch/HANDOFF.md` is the orchestrator's working memory, in two parts:
1. *Live state*, between `LIVE:BEGIN` and `LIVE:END` markers, regenerated by `orch check`/`status`. Never edit it.
2. *Orchestrator notes*, written by the orchestrator: goal and scope, per unit why and what done means, decisions with
   reasons (latest first), next steps, things to watch. Keep it under 150 lines (`check` flags more); move old notes to
   `.orch/history.md`, which is never injected.

The notes count as stale when they have not changed since the last unit change or owner instruction, or (while work runs)
for longer than `cycle_min`. The Stop hook then blocks.

**Context reset.** The watcher estimates context size from the transcript file (file size / 4, a rough guess). When it is
over `reset_tokens`, the orchestrator is idle, no problems are open and the notes are fresh, it first asks the
orchestrator to finish its notes; on a later pass it sends `/clear`. SessionStart then reloads the handoff, rules and
check output.

**Owner instructions and standing rules.** Every owner prompt becomes a beads issue labelled `instruction`. Classify it:
close it (it was a one-off, now done) or make it a standing rule with `bd remember "..."`. Rules come back at every session
start through `bd prime`, so the owner never has to repeat them.

**The message board.** `paths.board` is your script. orch calls `summary` (a line containing `SILENT` or `GONE` marks a
silent agent), `rule <unit> <machine>` (text added to every agent prompt), `post` (on launch) and `close` (on close).
Agents post their progress to it; the orchestrator reads `orch` output rather than the board or panes directly.

## 7. Multiple machines, capacity and network outages

Every host in `orch.toml` can run agents. `launch` picks the host with the most room. Room is measured live: free memory
minus `reserve_gb`, divided by `agent_gb`; a host is full when load exceeds 1.5 x `cores`, memory pressure (`psi`, Linux)
is 5 or more, or disk is under `min_disk_gb`. Caps reduce it further. An unreachable host has room 0.

Work belongs to the machine's own queue, not to an ssh session:
- **pueue** runs on each machine (launchd/brew on the Mac, a user systemd service on Linux hosts). API agents run as
  tasks in the `agents` group and keep running if ssh drops. `orch check` also adjusts the `jobs` group's parallel limit
  from measured room (at least 1, at most `cores`).
- **tmux** holds interactive remote agents (session `orch-<unit>`); the herdr tab only attaches to it with
  `ssh -t host tmux attach`, so a dropped view does not stop the agent.
- **systemd-run** with `MemoryMax` (from `memory_max`) wraps remote API agent tasks, so the kernel stops one that outgrows
  its share.
- A unit is stopped only by `orch stop <unit>`, never because a connection dropped. Probes use ssh `BatchMode` with
  5 s connect timeouts.

During an outage `check` shows `host X unreachable since <time>` and the units there; their state is "unknown", not
failed. When the host returns, `check` reports finished units and `orch collect <unit>` pulls the results back.

Remote API agents read the key from the macOS keychain item `orch-anthropic-api` and pass it over ssh stdin into a
private fifo; it is never written to a file or command line. Remote API agents need `claude` on the host.

## 8. Troubleshooting

| You see | Meaning |
|---|---|
| `orch: no orch.toml here or in a parent` | run it inside the project |
| `refused, no host has room (laptop: disk 12G; ...)` | every candidate host is full or unreachable; the reason is listed per host. Check `orch capacity` |
| `no model available for normal/implement (codex: provider codex disabled; ...)` | routing found no enabled, in-budget model (or one allowed on `--host`, or of another vendor for a review) |
| `<name> already launched on <host>` | the unit exists; `orch stop`/`close` it first (a review may reuse the name) |
| `brief ... not found` | create `<briefs>/<unit>.md` |
| `has uncommitted changes; commit and push, or --force` / `unpushed commits` | `orch close` protects unlanded work |
| `<unit> is still alive after stop` | the process outlived 10 s of waiting; run `orch check` and look again |
| Hook denial "Orchestrators do not operate by hand" | brief an agent or use `orch`; see section 5 |
| Stop blocked "Do not go idle" | run `orch check`, act, `orch ack`, update the notes |
| `beads unavailable: ...` | `bd` is missing or `[project] beads` has no `.beads/`; run `bd init` there |
| `watcher com.orch.watch is not running: run install` | the launchd agent is not loaded |
| `git has no upstream: set the bare repo on linux1 and push` | the project repo has no upstream branch (the text names linux1 from the original project) |
| `orchestrator gone; no provider enabled` (in `.orch/watch.log`) | the watcher will not start a new orchestrator unless a provider is enabled |

Logs: `.orch/watch.log` (watcher), `~/.config/orch/watch.err` (launchd stderr). Hooks that fail print to stderr and let
the action through.

## Known gaps

Not yet verified (from the toolkit's own test report, `out/api-orch-toolkit2/REPORT.md`):
- A remote API agent on linux1 (only linux2 ran it; same code path).
- `watch`: waking, restarting the orchestrator, and the context reset.
- A full herdr-backed agent via `orch launch`, and `orch stop` of a live remote task (`pueue kill` was only tried by hand
  on the Mac).
- A Mac-local API launch under pueue through the launcher script.
- The real `install` (links, hooks, launchd) was not run.
- `orch close` on a remote unit with a real git worktree.

Known open items:
- **Notifications filed as instructions:** UserPromptSubmit files every prompt except the watcher's wake text, so an
  automatic notification becomes an `instruction` issue that must be classified.
- **Cleanup refused by the disk floor:** `launch` refuses a host below `min_disk_gb`, so an agent meant to free disk space
  cannot be launched there.
- **The loop rule blocks read-only loops:** any `for`/`while` command that mentions worktrees is denied, even a harmless read.
- **`check` can miss a host under its floor when idle:** the "over its margins" problem is raised only for low memory (below
  60% of `reserve_gb`); low disk or high load alone are not reported, and an idle host with room is not reported without
  ready units.

Described in `docs/PLAN.md` but not in the code: moving unstarted jobs to another host after `unreachable_min`
(the setting is read but unused), pushing state to a bare repo, wrapping tmux or herdr agents in `MemoryMax` (only remote API
agents get it), and the Linux systemd timer for the watcher (the watcher is launchd, so macOS only).
