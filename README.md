# janus

janus lets one AI session act as the orchestrator of a software project. The orchestrator hands work to AI coding agents
on one or more machines, watches them, checks their results, lands what passes and cleans up. It does not do the work
itself. janus is a command-line tool (`janus`, standard-library Python), a one-page skill the orchestrator loads, hooks
that hold it to that role, and a watcher that wakes it. It is glue over git, ssh, rsync, [beads](https://github.com/steveyegge/beads)
(`bd`, a local issue tracker), pueue (a per-machine job queue), and herdr or tmux (terminal panes).

Terms: a *unit* is one piece of work given to one agent; its *brief* is the written task; a *gate* is the test run a
change must pass before it *lands* (is merged into the main branch).

## Why

A long-running AI orchestrator fails in the same ways again and again, and reminders in its prompt do not stop it. janus
replaces each reminder with a check or a hook:

| It tends to... | janus |
|---|---|
| go idle with nobody to wake it | a watcher runs every five minutes and wakes it; a hook refuses to let it stop with open problems |
| do work by hand and break things (bulk edits, pattern kills, hand-resolved merges) | a hook denies destructive shell commands; `janus` commands do the operations with safety checks |
| launch more agents than the machines hold | every agent and job holds a core and memory reservation; work waits or is refused, never killed to make room |
| let agents burn budget waiting for builds | an agent submits a job, writes `WAITING <job ids>` in its report and ends its turn; janus resumes it with the results |
| run whole test suites as gates | gates are targeted at what the change affects; known failures do not fail a gate |
| lose work or spend records | stop and close refuse while a remote copy holds uncollected work; the spend ledger is written under a lock |
| forget what was decided or asked | owner messages become instructions, decisions are recorded, questions keep an id until answered, notes are reloaded after a context reset |

The failures behind each row, with root causes, are in [docs/ISSUES.md](docs/ISSUES.md).

## When to use it

Use janus when you want an AI session to coordinate many agents over days, on one or several machines, and you want
guardrails instead of trust. Skip it for a single agent on a single task: janus adds setup and rules that only pay off
at scale.

## Install

You need macOS or Linux, Python 3.11+, git, ssh, rsync, `bd`, pueue, herdr or tmux, and the agent CLIs you will use
(Claude Code, Codex, opencode).

```
git clone <this repo> ~/code/janus && cd ~/code/janus
./check                              # the tests
./install --dry-run --project ~/code/myproject   # shows every change; then run without --dry-run
```

`install` builds a tested, read-only release, links the `janus` command and the skill, adds the hooks to Claude Code and
Codex, and installs the watcher. Codex needs one manual step: run `/hooks` there to trust them. Other options
(`--rollback`, `--hosts-only`, `--no-hosts`) are in `./install --help` and [REFERENCE §1](docs/REFERENCE.md).

## First run (about 5 minutes)

In your project, a git repository:

```
cd ~/code/myproject
python3 ~/code/janus/janus.py init   # writes a commented janus.toml; creates .janus/, briefs/, out/
$EDITOR janus.toml                   # hosts, models, where the API key is stored
bd init                              # beads, once, if there is no .beads/
janus doctor                         # checks the whole setup and names the fix for each problem
janus install-host <host>            # per extra machine in janus.toml: folders, pueue, a janus copy
```

Start your agent CLI in the project directory (for example `claude`) and run `janus register` in that session. This
session is the orchestrator; hooks act only for it. Then:

```
janus launch parser --kind implement --tier normal   # starts an agent on briefs/parser.md, on a host with room
janus check                          # one line per problem; silent when all is well
janus collect parser                 # bring back a remote unit's work
janus land parser                    # merge main, gate the merged tree, squash-land
```

`janus launch --dry-run` shows what would start without starting it. See [example.janus.toml](example.janus.toml) for
every setting.

## The core loop

The orchestrator repeats: **check, act on every line, acknowledge each with its decision, update its notes.**

- `janus check` reads the real state (hosts, queues, agents, board, gates, git, spend) and prints one line per problem,
  each naming the action that resolves it. `janus ack <key> "<decision>"` acknowledges one.
- Work goes out with `janus launch`, finishes with a report ending in a `VERDICT` line, and comes back with `janus collect`.
- `janus gate-scope <unit>` writes a targeted gate; `janus gate <name>` queues it; `janus land <unit>` runs the gate on
  the merged tree and lands it.
- Builds and tests run through pueue lanes (`janus run`), so agents never wait on them: `janus park <unit>` / `janus
  resume <unit>`.
- `janus close <unit>` ends a finished unit; `janus tidy --dry-run` lists what can be cleaned up.

Common commands:

| Command | What it does |
|---|---|
| `janus status`, `janus capacity` | the whole picture; room per host |
| `janus monitor [--interval 5]` | a read-only terminal dashboard: hosts, work (Active, Backlog, Finished) and the board; Tab switches, Enter shows details, `r` refreshes, `q` quits |
| `janus launch <unit> --kind implement --resume-of <unit>` | recover a crashed or capped unit from its report |
| `janus remember --fact "<text>" --source <path>` | add to session memory (also `--decision`, `--pitfall`) |
| `janus decide "<title>" --by owner --decision "…" --why "…"` | write a decision record |
| `janus decisions --grep <topic>` | find why something was decided |
| `janus answer q<N> "<text>"` | answer an agent's board question |
| `janus resume` | take over from another orchestrator session or machine |
| `janus report` | a status report for the owner |
| `bd remember "<rule>" --key <subject>` | add or replace a standing owner rule |

## Where state lives

Each kind of fact lives in one place:

| What | Where |
|---|---|
| Config: hosts, models, budgets, paths | `janus.toml` |
| Standing rules, units, owner instructions | beads (`.beads/`) |
| Session memory, about 30 dated lines at the head of every brief | `.janus/MEMORY.md` |
| Decision records | `.janus/decisions/` |
| Live state and the orchestrator's notes | `.janus/HANDOFF.md` (the live part is regenerated by `janus check`) |
| Each unit's task and result | `briefs/<unit>.md`, `out/<unit>/REPORT.md` |
| Agents' state, questions, answers | the board (`board.sh summary`), synced to every host |

Agents run headless and see only their brief, so the curated memory heads each one. Agents propose knowledge with a
`learned` board post; only the orchestrator writes it into memory. The map is in [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)
and [docs/DECISIONS.md](docs/DECISIONS.md).

## More detail

- [docs/REFERENCE.md](docs/REFERENCE.md): every command, option, hook rule and setting; troubleshooting; known gaps
- [docs/DESIGN.md](docs/DESIGN.md): how the pieces fit and why
- [docs/HEARTBEAT.md](docs/HEARTBEAT.md): the watcher's watchdog on another machine
- [docs/SANDBOX.md](docs/SANDBOX.md): the optional write-limit wrapper (not wired into launches)
- [docs/ISSUES.md](docs/ISSUES.md): the failure register

Status: the hooks, `check`, admission by reservations, park and resume, the ledger, targeted gates, the board and
session memory are built and tested. The watcher starting a new orchestrator, the controlled context reset and a remote
agent end to end are not yet seen working live. Open items: [REFERENCE known gaps](docs/REFERENCE.md#known-gaps).
