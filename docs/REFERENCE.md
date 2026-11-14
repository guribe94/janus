# Reference

On-demand command reference. The short contract is `skill/SKILL.md`; how it fits together is `docs/DESIGN.md`. Everything project-owned
lives under `PROJECT/.janus`: `janus.toml`, `MEMORY.md`, `HANDOFF.md`, `briefs/`, `out/`, `board/`, `worktrees/` and janus's bookkeeping.
`janus` finds the project from any directory below it (the nearest `.janus/janus.toml`); a worker outside it passes `--project PATH` or
has `$JANUS_ROOT`.

## Commands

| Command | What it does |
|---|---|
| `janus init` | write a starter `.janus/janus.toml` and the folders |
| `janus doctor` | check tools, release, hooks, watcher, config, keys, hosts |
| `janus migrate SOURCE [--to TARGET] [--apply]` | the one-time move from an old root to `TARGET/.janus` (dry run unless `--apply`); keeps identity, messages, instructions, registry; refuses while jobs, agents or outboxes are live or unknown; moves worktrees only for units that record them (closed ones too); never writes the bd database: it prepares `.janus/migration/bd-relocate.sh`, `bd-path-updates.sh` and `PENDING.md` for your approval |
| `janus status` / `janus check [--json]` | the state now, derived from hosts, units, board, queues; findings clear when their condition clears |
| `janus hosts [--refresh]` | each host: OS, arch, cores, RAM, disk, GPU, tools, coding clients (installed / login present / usable by config), caches |
| `janus capacity` | each host's admission now: room, reservations, pause reason |
| `janus monitor` | read-only curses dashboard; works from the root and any subdirectory; tabs Active, Backlog, Finished, Board, Overview (current % checkpoint from `HANDOFF.md`); tool/model per unit; per host admission reason, coding clients and the age of that reading; `?` help, `/` filter |
| `janus launch UNIT --kind K` | start an agent on a host with room; model chosen by `[routing]`, only if allowed (see Routing) |
| `janus fill` | launch ready backlog units that fit; `[fill] watcher = true` lets the watcher do it |
| `janus run --host H -- CMD` | queue a job through the host's own admission (`--detach` returns `host:task`; `--needs` places by capability). A remote agent's `<root>/run/<unit>.lane [--detach] [--broad R] CMD` is the same code (`janus/lane.py`: guard, queueing, a job record beside the files) |
| `janus queue`, `janus cancel`, `janus busy`, `janus thrash` | read queues; cancel one job; is a tree in use; shared-host thrash acts |
| `janus park`, `janus resume [UNIT]` | park a unit on its jobs / resume it; with no unit, continue the orchestration in a new session |
| `janus collect`, `janus stop`, `janus close` | bring back a unit's output, stop it, close it (checks nothing is lost) |
| `janus land UNIT [--host H] [--push]` | merge the landing branch into the unit's branch, squash, move the branch from the tip merged (expected ref), optionally push (never forced); on the unit's own host. A requested push that stays refused exits non-zero (`NOT PUBLISHED`): the integrated result stays on the unit's branch and the remote is untouched |
| `janus remember`, `janus forget`, `janus memory` | MEMORY.md: `--instruction`, `--decision`, `--fact`, `--pitfall`, `--key` replaces |
| `janus answer q<N> TEXT`, `janus board-close AGENT` | answer a board question; archive a card |
| `janus transcript UNIT`, `janus index`, `janus report` | grep/tail a transcript; the reports index; derived status view |
| `janus backlog`, `janus ledger`, `janus tidy`, `janus commit-state` | checklist import; API spend ledger; clean up; commit and push state |
| `janus register`, `janus install-host H`, `janus watch` | register the orchestrator session; set a host up; the watcher pass (`--reset`: verified new session) |

`board.sh` (also `janus` for answers): `post`, `send <agent> <text>` (one addressed message), `direct <agent> <text>`, `read`, `summary`,
`questions`, `answer`, `digest`, `sync`. There is no broadcast.

## Memory and handoff
`MEMORY.md` holds the binding instructions and durable decisions, one keyed line each; a newer line with the same key replaces the older;
nothing expires or is truncated. It is loaded whole before the handoff at every start, reset and resume and heads every worker brief.
A new or replaced instruction or decision is sent as one addressed `[memory]` message to each running unit (no reply is asked).
`HANDOFF.md` is curated by the orchestrator: objective, decisions, blockers, next actions, and the latest short checkpoint. It is updated
after a meaningful change of plan, and before a reset (the Stop hook asks once over `reset_tokens`).

## Routing (one place: `janus.toml`)
`[providers.X] enabled`, `auth = "api_key"|"subscription"`; `[models.M] tool`, `provider`, `hosts`; `[routing] allow_tools = [...]`,
`allow_auth = [...]` and the ordered `critical`/`normal`/`review` lists. A model outside these is never launched. Only the first entry of a list is used; a later
entry (another provider or login) is used only when you write `fallback = true` in `[routing]`, and then it is printed. The same restrictions bind a
resume. A list you leave out restricts nothing. janus has no built-in model, vendor or login rule: what is allowed is what you write here. For example,
an API-key-only setup is `allow_auth = ["api_key"]`, `allow_tools = ["claude"]` with other providers disabled (the shipped `example.janus.toml`); add
`"subscription"` or other tools to allow them. The key is read from a keychain item or a mode-600 file at run time and given only to the agent process.

## Capacity
Admission is by reservations on the host that runs the job (cores, learned peak memory, disk floor, back-pressure); `janus capacity` and the
host's own admission use one calculation. Three things are kept apart:
- **Configured resources** (`cores`, `ram_gb`, `services_gb`, `cores_per_job`, `job_gb`, `agent_gb`, `min_disk_gb`) are the budget reservations count against.
  pueue's `jobs` group limit is derived from them alone (`cores // cores_per_job`); a reading never raises it.
- **Currently measured state** (free memory, memory pressure, load, free disk, what runs now) decides what may start now: it can pause admission or
  hold a job at the disk floor, and the `agents` group limit is set each watcher pass to the agents running plus the agents that fit now.
  A job's memory figure is learned from its past peaks, not configured once.
- **Owner caps** (`[caps] total`, `[caps.host]`) limit the number of agents and exist only when you set them; they lower the `agents` figure
  and raise `cap:*` findings when exceeded. They are not a resource budget.
 The first job on an idle host is exempt only from the agents' share: it must still fit the host alone.
Pending authorized work fills every capable host with room; nothing is pinned to a default host. A host that does not answer is *unknown*:
its jobs are not cancelled, moved or relaunched. SSH is retried only when the master connection refused the session (nothing ran).

## Waiting for jobs
`janus run` waits for its task in steps of at most 300 s (`WAIT_STEP`). Locally the wait is `exec pueue wait`, killed at 300 s. On a remote host it is
`timeout 300 pueue wait -q ID` run there over ssh (the local ssh is a 330 s backstop), because killing only the local ssh left the remote wait
running. A remote host therefore needs coreutils `timeout`; without it each wait fails at once and janus polls every few seconds. After a step
that ran out, janus reports the host's admission reason if the job's own admission is holding it, and waits again.

## Remote units and git
A unit on another host gets its copy by rsync (no `.git`) plus a git bundle of the landing branch and its HEAD, fetched into a new `.git` there:
it commits on its own branch (`janus/<unit>` when the source tree was on the landing branch), with the source's origin url, user name and email.
`janus land` then runs on that host (merge, squash, expected-ref move, plain push). A source that is no git tree stays a snapshot (changes come
back with `janus collect`). Publication needs that host to reach the remote; without one the result stays in the host's repository (fetch it
over ssh). Closing still checks the copy was collected.

## Tests
There are no generated gates. Tests are the project's own explicit commands, run with `janus run` through ordinary admission; a whole-suite
run needs `--broad REASON`. Landing runs none: after `janus land` reports it integrated moved work, a clean merge does not prove the merged
tree is right; run the tests that matter.

## Reset
`janus watch --reset` uses the tool's own new-session command (Claude Code `/clear`, Codex `/new`, OpenCode `/new`; read from each binary), marks
the session pending, and counts the reset done only when a new session registers (Claude, Codex: also a new transcript). OpenCode has no hook:
the new session runs `janus register` (it reads `OPENCODE_SESSION_ID`), and OpenCode keeps no transcript janus can read, so it has no automatic
size trigger and no end-to-end live run was made. Otherwise it says why and restores the old registration. Other tools are unsupported.
Launch and resume command lines (`launch.tool_argv`) are the ones each client's `--help` shows: claude `--model/--resume`, codex `resume`/`-m`,
opencode `run --model/--session`.

## Locks
Host and herdr I/O never runs under the project writer lock (`.janus/write.lock`). A watcher pass holds a separate `.janus/watch.lock` (a second
pass is skipped, not queued) and takes the writer lock only for short state steps: probes, remote output copies, board sync, heartbeat, tidy scans,
thrash cancels and re-queues, orphan cancels, herdr calls and a resume's message and delivery all run with it let go (`cfg.unlocked`). A resume
records the unit busy (and `once.json` for the same finished jobs) before it lets go, so neither another pass, `janus resume`, `stop` nor `close`
starts a second one; a thrash stop restarts the host's over-since clock before touching the host. If the lock cannot be retaken afterwards the pass
ends and `check` names the unit interrupted. A resume refuses a removed, unknown or disabled model (`route_ok`) on every path.

## Findings
| Family | Meaning |
|---|---|
| `age` | a job waited or ran past its lane's limit |
| `backlog` | checklist not imported / ready units held |
| `beads` | bd unavailable |
| `blocked` | jobs wait in an admission that says no |
| `board` | a card is blocked, has a question, is gone or silent; posts wait unsynced |
| `cap` | more agents than the owner's caps |
| `disk` | a host is over its disk budget |
| `git` | the project repository is dirty / unpushed |
| `host` | a host is unreachable (unknown: nothing is moved or relaunched for it) |
| `idle` | room while work waits |
| `idle-with-backlog` | ready briefed units fit now: janus fill |
| `ledger` | a provider ledger is malformed or unreconciled |
| `machine` | shared-host items with no project id: listed, never acted on |
| `margin` | a host is over disk or memory margins |
| `old-rule` | jobs under the old cap rule |
| `orchestrator` | registered orchestrator differs from config / never registered |
| `orphan` | a job, session or pane with no open unit |
| `parked` | a parked unit's job cannot finish or its resume failed |
| `pressure` | admission paused by back-pressure |
| `provider` | a unit runs on a disabled provider |
| `scheduler` | job-running work outside janus's lanes |
| `spend` | committed spend near or at the cap |
| `stalled` | queued work with free slots |
| `tab` | a tab of a closed unit is still open |
| `thrash` | the watcher stopped and re-queued a job on a thrashing host |
| `tidy` | what janus tidy can reclaim |
| `unit` | a unit is blocked or finished |
| `unregistered` | a janus bug: an unregistered finding family |
| `waiting` | a live agent waits for admission |
| `watcher` | the watcher is not running or a step keeps failing |

## Hooks
`SessionStart` loads MEMORY.md and HANDOFF.md; `PreToolUse` refuses the orchestrator's hand operations; `Stop` never blocks except one reminder to
update the handoff over the context threshold. A worker's Stop hook blocks once if its report has no VERDICT and no WAITING line.

## Install
`./check [test_file.py ...]` is a smoke check (and only the named tests). `./install` options: `--dry-run` (print every change), `--project DIR`,
`--stage-only` (build and check an immutable release, switch nothing), `--release-only` (release and links only: no hooks, no watcher),
`--rollback` (back to the previous release), `--no-hosts`, `--hosts-only`, `--tests PATTERN` (also run only the matching test files against the copy).
Both `--release-only` and a full `./install` switch `current` and refuse while a registered project is still on the old layout (migrate it first, with
`python3 janus.py migrate` from the checkout or the staged release; see the README). A full `./install` also merges hooks into `~/.claude/settings.json`
and `~/.codex/hooks.json` (backup first), activates the watcher (launchd agent or systemd user timer, every 2 minutes, over the projects listed in
`~/.config/janus/projects`; the project found from the current directory or `--project` is added) and runs `janus install-host` per remote host.
Because the watcher can start an orchestrator, set up and `janus register` first, then run the full install (README, Fresh setup).
Releases a running process or queued job names are never pruned. Skill links: `~/.claude/skills/janus` (Claude Code and opencode) and
`~/.agents/skills/janus` (Codex).
