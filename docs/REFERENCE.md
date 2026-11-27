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
| `janus hosts --mark HOST gone\|returning\|clear` | the owner's word on a host that does not answer: `gone` (retired) relaunches its units' items elsewhere now; `returning` keeps them waiting (cleared by its first good probe); `clear` goes back to `[capacity] down_after_min` |
| `janus capacity` | each host's admission now: room, reservations, pause reason |
| `janus monitor` | read-only curses dashboard. On narrow screens Active and Ended keep the unit name and state visible. Enter shows details. Overview gives the standby reason. Works from the root and any subdirectory; tabs Active, Backlog, Ended, Board, Overview (current % checkpoint from `HANDOFF.md`; the checklist totals `janus report` gives); tool/model per unit; per host admission reason, coding clients and the age of that reading; a host down past `down_after_min` or marked is dimmed and last (`DOWN`/`GONE`/`RETURNING`), its units `stranded` (`->` the relaunch), the items only they covered `relaunch` in Backlog, with the covering host; a launch that never finished `starting STALLED`; `?` help, `/` filter, `w` window (24h, 7d, all: how far back the Ended tab and the header's flow line look). The header's `done` fraction is the checklist's (it moves on acceptance only); the flow line beside it counts the window's launched, finished, landed and accepted. The Ended tab's STAGE (finished, closed, landed, accepted) and WHEN order it newest first after the units waiting to be collected. The Active tab's STATE says what is happening in words: agent working; waiting for job H:N with the job's state, run time and its class's usual time (warning colour past 3x); waiting for a free agent slot; agent stopped (turn ended, no job, no verdict); launching or launch stuck; stranded (host gone) with what janus does with it. Active and Ended show each unit's ITEM; the Backlog tab shows per item its UNITS (done, open: the newest open one), STATE in words (in progress; all units done: close it or brief the rest; ready; ready, no brief; held; owner's) and LAST (when one of its units last started or closed). |
| `janus launch UNIT --kind K` | start an agent on a host with room; model chosen by `[routing]`, only if allowed (see Routing). A remote launch needs a tree: `--new-worktree REPO`, `--cwd TREE` or an existing `<worktrees>/UNIT`; it is refused (nothing recorded or copied) rather than copy the project root |
| `janus fill` | launch ready backlog units that fit: only items beads lists ready (`bd ready`), of an agent, not held, not covered by an open unit; beads unreadable: nothing launched (never from the bare checklist); a brief a remote host cannot satisfy is refused while planning; `[fill] watcher = true` lets the watcher do it. Brief header lines read: `ITEM`, `KIND`, `TIER`, `PRIORITY`, `BUDGET` (USD, replaces the learned budget), `MODEL` (a `[models]` name: fill and launch without `--model` use it, checked as `--model` is; never replaced by a routed model), `TEST-HOST`, `HOST-NEEDS`, `REPO`; any other line is prose for the agent (ordering is a beads dependency: `bd dep add <blocked> <blocker>`) |
| `janus run --host H -- CMD` | queue a job through the host's own admission. During a queue outage the waiter keeps the task ID and retries. A successful listing can confirm removal. Options (`--detach` returns `host:task`; `--needs` places by capability). `--wt HOST:PATH` runs in a tree already on that host (a remote unit's copy: never synced; another open unit's copy is refused). `--usb` takes the host's usb lock on every runner (`[hosts.H] usb_lock`, else the container's, else `<runs>/usb.lock`). A remote agent's `<root>/run/<unit>.lane [--detach] [--usb] [--broad R] CMD` is the same code (`janus/lane.py`: guard, queueing, a job record beside the files) |
| `janus queue`, `janus cancel`, `janus busy`, `janus thrash` | read queues; cancel one job (a task its host no longer has: what janus kept for it, its busy and job records and wait files, is removed); is a tree in use; shared-host thrash acts |
| `janus park`, `janus resume [UNIT]` | park a unit on its jobs / resume it; with no unit, continue the orchestration in a new session |
| `janus collect`, `janus stop`, `janus close` | bring back a unit's output, stop it, close it (checks nothing is lost). `janus stop --orphan HOST:SESSION` stops one tmux session of this project that no open unit records (`janus tidy` stops them all). `janus close UNIT --force` closes without a verdict: on its own tree it skips the git checks; without a tree of its own it still needs its agent stopped and its copy collected, except on a host marked gone (that copy is lost: nothing is asked of the host, its run is settled, out/UNIT stays); never a main checkout with linked worktrees. `janus close UNIT --abort` undoes a close that died before its cleanup began (the unit is open again: resume, collect, land or close it). `janus collect UNIT --subdir PATH` (remote units only): a verified patch of one relative subtree of the copy, paths kept relative to the copy, in `out/UNIT/changes-scoped.patch`; partial, so the unit is not recorded collected and stop/close still need a full collect |
| `janus land UNIT [--host H] [--push] [--force-review]` | merge the landing branch into the unit's branch, squash, move the branch from the tip merged (expected ref), optionally push (never forced); on the unit's own host. A requested push that stays refused exits non-zero (`NOT PUBLISHED`): the integrated result stays on the unit's branch and the remote is untouched. A verified push records the publication (source HEAD, commit, remote, branch) on the unit and writes the sha into the linked item's checklist evidence cell (status unchanged); `janus land` again finishes an incomplete writeback without landing again. `janus close` of a squash-landed unit accepts its unpushed commits only when its tree is contained in that publication, verified now |
| `janus remember`, `janus forget`, `janus memory` | MEMORY.md: `--instruction` (a standing owner rule), `--decision` (a ruling on one question), `--fact`, `--pitfall`; `--key` replaces the entry of that key; `--until WHEN` gives it an end (past it the entry is loaded nowhere and `memory:expired` asks to forget it). `remember` lists the standing entries whose key shares a word with the new one. A unit's lease, a status note or a pending question is not memory: handoff or board |
| `janus answer q<N> TEXT`, `janus send AGENT TEXT`, `janus board-close AGENT` | answer a board question (the unit that asked is resumed with it when its agent has stopped); one addressed message; archive a card |
| `janus transcript UNIT`, `janus index`, `janus report` | grep/tail a transcript; the reports index; derived status view |
| `janus backlog`, `janus ledger`, `janus tidy`, `janus commit-state` | checklist import; API spend ledger (`janus ledger check` also names each run that cost over twice its budget, the rule behind `ledger:<provider>:implausible`; `janus ledger accept RUN --why TEXT` records an overspend the owner reviewed, which then leaves the finding); clean up; commit and push state. `janus backlog set ITEM status=DONE --unit UNIT [--commit SHA] [--dry-run]`: explicit acceptance of a whole item, only with the unit's publication verified on the remote now (recorded by `janus land --push`, or `--commit`: a commit the unit's agent pushed itself or verified already there, named in its report): the row's status becomes DONE, its evidence cell gets the sha, then its beads issue closes. `--commit SHA --owner-accepted WHY`: the owner accepted the item on that published commit, which the report need not name (still verified on the landing branch; WHY goes in the close reason). Acceptance and import are the orchestrator's: an agent is refused. `janus backlog import` that would close more than one open item lists them and needs `--yes` |
| `janus bead close ID... --reason WHY` | close beads that are neither units (`janus close`) nor backlog items (their acceptance): stale beads the hook keeps you from closing with bd by hand |
| `janus register`, `janus install-host H`, `janus watch` | register the orchestrator session; set a host up; the watcher pass (`--reset`: verified new session) |
| `janus standby [--reason R] [--stop-units [--force]]`, `janus standby off` | the owner said stop or done: the watcher only syncs the board and saves results (no wake, resume, fill, restart), no context reset runs, the status/board/watcher tabs close; `--stop-units` also stops every open unit and its jobs; `off` resumes |

`board.sh` (the agents' interface; the orchestrator uses `janus answer` and `janus send`): `post`, `send <agent> <text>` (one addressed message), `direct <agent> <text>`, `read`, `summary`,
`questions`, `answer`, `digest`, `sync`. There is no broadcast.

## Memory and handoff
`MEMORY.md` holds the binding instructions and durable decisions, one keyed line each; a newer line with the same key replaces the older;
nothing expires or is truncated. It is loaded whole before the handoff at every orchestrator start, reset and resume. A worker's brief and each resume message carry its instructions and pitfalls in full and the path of the file for its decisions and facts (a remote host: the copy at `<root>/MEMORY.md`, written at launch and at each resume).
A new or replaced instruction or decision is sent as one addressed `[memory]` message to each running unit (no reply is asked).
`HANDOFF.md` is curated by the orchestrator: objective, decisions, blockers, next actions, and the latest short checkpoint. It is updated
after a meaningful change of plan, and before a reset (the Stop hook asks once over `reset_tokens`). It is rewritten in place, never appended to.

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

## Tests and rigor
There are no generated gates. Tests are the project's own explicit commands, run with `janus run` through ordinary admission. How much a
unit tests, and whether it is reviewed before landing, is its rigor level: `[rigor] level` in janus.toml (default `lean`), or for one unit
a brief's `RIGOR:` line or `janus launch --rigor`. The level is written into the agent's brief header and the orchestrator's session text.

| level | tests written | tests run | reviews before `janus land` | whole-suite run (agents) |
|---|---|---|---|---|
| `ship` | none | none; the Stop hook takes DONE without exit codes | none | refused |
| `quick` | none | a compile or typecheck of the changed code | none | refused |
| `lean` | only where no existing test would catch the break; extend, do not add fixtures; remove obsolete ones | the one targeted check that proves the change | none | refused |
| `careful` | one per changed behavior | the touched packages' tests | 1 for a critical unit | `--broad REASON` |
| `thorough` | plus edge cases | the full suite | 2 for critical (one by another vendor), 1 for normal | allowed, low priority |

A review counts when it is a unit launched `--kind review --of UNIT` whose report says `VERDICT: DONE`; until then check raises
`review:<unit>` and `janus land` refuses (`--force-review` lands anyway). The orchestrator's own `janus run` keeps `--broad REASON` at every
level. Landing runs no tests: after `janus land` reports it integrated moved work, a clean merge does not prove the merged tree is right.

## Reset
`janus watch --reset` uses the tool's own new-session command (Claude Code `/clear`, Codex `/new`, OpenCode `/new`; read from each binary), marks
the session pending, and counts the reset done only when a new session registers (Claude, Codex: also a new transcript). OpenCode has no hook:
the new session runs `janus register` (it reads `OPENCODE_SESSION_ID`), and OpenCode keeps no transcript janus can read, so it has no automatic
size trigger and no end-to-end live run was made. Otherwise it says why and restores the old registration. Other tools are unsupported.
Launch and resume command lines (`launch.tool_argv`) are the ones each client's `--help` shows: claude `--model/--resume`, codex `resume`/`-m`,
opencode `run --model/--session`.
An opencode agent in tmux runs **one process per turn** (`launch.per_turn`): janus gives it a session id of its own (`ses_janus_<unit>_<time>`,
recorded as the unit's `session`), writes each message to `<root>/run/<unit>.msg` and runs `opencode run --session <id> "$(cat <msg>)"` in the
unit's tmux pane, which is kept after the process ends (`remain-on-exit`). Its state is its process: working while it lives, idle when its pane is
dead (the probe's `dead <time>`), gone when the session is. Nothing is pasted and no screen is read. A resume (janus resume, the watcher) is a new
turn on the same session; it is refused while a turn still runs. Each turn's end is brought here (its end time is the saved marker).
An agent started on a host with `memory_max` runs in a systemd scope with that `MemoryMax` (`[models.M] memory_max` overrides the host's) and
`OOMPolicy=continue`: when the kernel kills a build the agent ran itself, that build fails and the agent lives. Builds belong in `janus run`, whose
job gets its own admitted scope.
The Stop hook defers the reset while the owner wrote in the last 15 min and never resets in standby; after a reset the new session is handed the old one's last message to repeat if it was for the owner. A hook from the registered orchestrator pane registers its session, whatever its id (an older session resumed there).

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
| `backlog` | checklist not imported / ready units held / ready items blocked by a beads dependency (`backlog:blocked`) |
| `beads` | bd unavailable |
| `blocked` | jobs wait in an admission that says no |
| `board` | a card is blocked, has a question, is gone or silent; posts wait unsynced |
| `cap` | more agents than the owner's caps |
| `disk` | a host is over its disk budget |
| `review` | a finished unit lacks the DONE reviews its rigor level requires (`careful`: 1 for critical; `thorough`: 2 for critical, one by another vendor, 1 for normal); `janus land` refuses it until then (`--force-review`) |
| `copy` | `copy:<host>`: the host's janus copy (`<root>/janus/VERSION`, written by `janus install-host`) is not this release's: `janus install-host <host>` |
| `duplicate` | a stranded unit's host answers again while its relaunch is open: keep one (collect and land or close the original, or `close <relaunch> --duplicate-of <original>`) |
| `git` | the project repository is dirty / unpushed |
| `host` | a host is unreachable. Past `[capacity] down_after_min` (default 30) or marked `gone` it is stranded: its open units no longer cover their backlog items or hold cap slots, and `janus fill` relaunches each item on a host that answers (a new unit `<name>-rN`, `--resume-of` the original: its report copied here with a RELAUNCH note recording the patch outcome. A collected-only `changes.patch` or conflicting patches stay available for reconciliation); a brief pinned to it waits (`backlog:pinned:<unit>`). A host marked `returning` waits for reconnection. `host:<h>:gone` lists a gone host's units to close |
| `handoff`, `memory` | `handoff:long` / `memory:long`: HANDOFF.md or MEMORY.md is over its size (`[project] handoff_max_kb`, default 16; `memory_max_kb`, default 48). Both are loaded whole at every session start; nothing is cut: rewrite the handoff in place (older checkpoints to `.janus/HANDOFF-archive.md`), `janus forget` superseded memory entries. `memory:expired`: entries past their `--until` end |
| `idle` | room while work waits |
| `idle-with-backlog` | ready briefed units fit now: janus fill; `:nobrief`: ready items without a usable brief, by id. Dispatch work: wakes an idle orchestrator again every 10 min while it holds |
| `ledger` | a provider ledger is malformed or unreconciled |
| `machine` | shared-host items with no project id: listed, never acted on |
| `margin` | a host is over disk or memory margins |
| `old-rule` | jobs under the old cap rule |
| `orchestrator` | registered orchestrator differs from config / never registered |
| `orphan` | a job, session or pane with no open unit |
| `parked` | a parked unit's job cannot finish or its resume failed |
| `backlog:done:<item>` | an open item with no open unit, at least one unit closed DONE and no brief waiting: close it (`janus backlog set <id> status=DONE --unit <unit>`) or brief what is left |
| `unit:<name>:not-started` | a working unit whose report is still the launch seed past `[project] silent_min`: its agent is likely at a startup prompt, a login or an error; look at its screen |
| `pressure` | admission paused by back-pressure |
| `provider` | a unit runs on a disabled provider |
| `routing` | a `[routing]` list starts with a model that cannot launch (provider disabled, tool or auth not allowed) and no fallback is allowed: fill and launch without a model launch nothing for it |
| `question` | an open board question: `janus answer q<N> TEXT` |
| `scheduler` | job-running work outside janus's lanes |
| `spend` | committed spend near or at the cap |
| `stalled` | queued work with free slots |
| `tab` | a tab of a closed unit is still open |
| `thrash` | the watcher stopped and re-queued a job on a thrashing host |
| `tidy` | what janus tidy can reclaim (`janus tidy` prints `kept copy HOST:NAME: why` for each remote copy or tree it leaves). Kind `job-tree`: a tree `janus run` synced to a host for a unit closed over `[tidy] keep_days` (a mirror of the unit's tree, plus build output). Kind `brief`: brief files unchanged for `[tidy] keep_days` that nothing reads (a unit closed that long ago; a closed item's; one naming no item whose name never ran) are moved to `.janus/briefs-archive`, never deleted; an open unit's, an open item's unlaunched brief, `drafts/` and held files stay |
| `unit` | a unit is blocked, finished, ended its turn without a verdict (`:ended-pending`), interrupted (`:interrupted`) or not started |
| `unregistered` | a janus bug: an unregistered finding family |
| `waiting` | a live agent waits for admission |
| `watcher` | the watcher is not running or a step keeps failing |

## Hooks
`SessionStart` loads MEMORY.md and HANDOFF.md; `PreToolUse` refuses the orchestrator's hand operations; `Stop` never blocks except one reminder to
update the handoff over the context threshold. A worker's Stop hook blocks once if its report has no VERDICT and no WAITING line. A client without hooks (opencode, one process per turn) gets the same text from the watcher instead: one resume when its turn ended with neither, once (the turn that nudge starts is not nudged again; `unit:<name>:ended-pending` then stands). Hook and nudge name the board script by its full path (`JANUS_BOARD`).

## Install
`./check [test_file.py ...]` is a smoke check (and only the named tests). `./install` options: `--dry-run` (print every change), `--project DIR`,
`--stage-only` (build and check an immutable release, switch nothing), `--release-only` (release and links only: no hooks, no watcher),
`--rollback` (back to the previous release), `--no-hosts`, `--hosts-only`, `--tests PATTERN` (also run only the matching test files against the copy).
Both `--release-only` and a full `./install` switch `current` and refuse while a registered project is still on the old layout (migrate it first, with
`python3 janus.py migrate` from the checkout or the staged release; see the README). A full `./install` also activates the watcher (launchd agent or systemd user timer, every 2 minutes, over the projects listed in
`~/.config/janus/projects`) and runs `janus install-host` per remote host of the project it is run in, else of every project that is on (the copy of janus, its release name, the skill; a janus skill link the host already has is pointed at that copy, none is made). Janus is on per project: `janus on` (in the project) writes its
hooks into `<project>/.claude/settings.local.json`, links `<project>/.claude/skills/janus`, adds the project to `~/.config/janus/projects`, and
while any project is on keeps the Codex hooks and skill global (`~/.codex/hooks.json`, `~/.agents/skills/janus`); `janus off` removes exactly
those. Agents janus launches get the hooks for their own session (`claude --settings`). An install keeps every registered project on and removes
the old global Claude Code hooks and skill link.
Because the watcher can start an orchestrator, set up and `janus register` first, then run the full install (README, Fresh setup).
Releases a running process or queued job names are never pruned.
