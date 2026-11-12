# janus

janus lets one AI session run a software project as its orchestrator: it hands work to many AI coding agents on one or
more machines, watches them, checks their results, lands what passes and cleans up. The orchestrator never does the work
itself. janus is a command-line tool (`janus`, standard-library Python), a one-page skill the orchestrator loads, a set
of hooks that hold it to that role, and a watcher that wakes it. It is glue over standard tools: git, ssh, rsync, beads (a
local issue tracker, `bd`), pueue (a per-machine job queue), tmux and herdr (terminal panes).

Terms used below: a *unit* is one piece of work given to one agent; its *brief* is the written task; a *gate* is the
test run a change must pass before it *lands* (is merged into the main branch).

It exists because an AI orchestrator left to itself fails in the same ways again and again, and reminders in its prompt
do not stop it. janus replaces each reminder with a check or a hook.

## The problem

The failures below happened on one project, run by an AI orchestrator for days with dozens of agents on several
machines, before and while janus was built. Each is listed with the mechanism that now prevents it. The full register,
with root causes, is [docs/ISSUES.md](docs/ISSUES.md).

**Nobody kept it awake.** The orchestrator went idle for seven hours overnight with nothing to wake it. A separate
watchdog stopped after each event and was not restarted, so the machines ran unwatched. Later, a watcher started a
second orchestrator while the first was still alive.
- *Now:* a watcher runs every five minutes (launchd on macOS, a systemd timer on Linux). It wakes the orchestrator when
  something needs it. It starts a new one only when the old one's pane and process are gone and its transcript has been
  quiet for 20 minutes.
- A hook blocks the orchestrator from ending its turn while a problem is unacknowledged or its notes are stale.
- A new question or block from an agent wakes it at once.

**It did the work by hand, and broke things.** A bulk change to every worktree dropped a test fixture; five landing
gates failed on it overnight. A pattern kill meant for one agent stopped four. A delete ran before a copy command that
turned out to lack the needed flag (the files were committed, so nothing was lost that time). A hand-resolved merge
conflict left a function unclosed and broke the build. A gate was queued after a failed merge and tested a tree full of
conflict markers.
- *Now:* a hook judges every shell command the orchestrator runs. It denies deletes, kills, in-place edits, destructive
  git, and loops that change worktrees, including inside pipelines, `bash -c` and `ssh`. The orchestrator uses `janus`
  commands or briefs an agent instead.
- `janus run` and `janus gate` refuse a tree that is mid-merge, has conflict markers, or is far behind the main branch.
- `janus stop` ends exactly one unit's process tree, never a pattern.

**It overloaded the machines.** About 27 agents were launched against a safe limit of about 6. A build host ran at load
26 on 8 cores with 0.6 GB free, thrashing for hours, because each job was admitted on one momentary reading and then
grew. Two job schedulers ran on one host without knowing of each other. An out-of-memory kill restarted a three-hour job
from zero. Cancelled jobs left their containers running on a shared server that other people use.
- *Now:* every job and agent holds a reservation of cores and memory. The memory figure is learned from the peaks of
  past jobs of the same kind. A host admits work while the reservations fit, pauses on memory pressure or high load, and
  never kills to make room. `janus launch` refuses when no host has room, and says when room is expected.
- `janus check` reports a second scheduler on a host. Cancelling a job follows it to the remote host and its container.
- On a host marked shared, sustained memory pressure makes the watcher stop this project's newest job and queue it again
  elsewhere. Nothing that is not this project's is touched.

**Agents spent their budgets waiting.** Agents polled full build queues until their budget ran out. One spent $16.81
writing code and tests, then waited for a test slot until its cap, without one test run. Of $149.48 spent in one
stretch, $42.28 bought usable work, $50.28 ended capped mid-task with partial work, $16.73 ended capped with nothing,
and $40.20 went on building janus itself. Gates ran the whole test suite: one ran 185 minutes and failed only on
failures already known on the main branch, while the steps that concerned the change took about 3 minutes.
- *Now:* agents never pay to wait. An agent submits its build or test, writes `WAITING <job ids>` in its report and ends
  its turn. When the jobs finish, janus resumes the same session with the results.
- Budgets come from the cost of past units of the same kind. Agents update their report after every step, so a capped
  run leaves its state behind, and a run that hits its cap gets one short resume to write its report.
- Gates are targeted: a project hook names the steps a change affects. Known failures, each tied to the unit fixing it,
  do not fail a gate. Gates have a time limit, and short gates do not wait behind long ones.

**Work was lost.** A remote copy was removed before the fix in it was collected; the fix had found a root cause and had
to be redone. A launcher script was edited while it ran; the shell read the new text at the old offset, the launcher
looped and wrote 49,000 rows to the spend ledger, which then showed $342k spent and blocked every paid launch. Earlier,
four jobs stopped at the same moment each rewrote the ledger, and together they lost 260 rows, which disabled the spend
cap.
- *Now:* `janus stop` and `janus close` refuse while a remote copy holds uncollected work. The watcher saves a finished
  remote unit's output and a patch.
- The installed toolkit is a tested, read-only copy, so editing the source never changes what runs. Every script janus
  writes is replaced by rename, never edited in place, and `janus check` flags a running script whose file changed.
- The ledger is written under a lock, one row per run. A malformed ledger blocks launches; an implausible one is
  reported.

**Knowledge was lost between agents and between sessions.** Agents run headless and see only their brief. Six asked the
same question; one spent its budget re-deriving a root cause recorded the day before. On the board, 80 % of lines were
step-by-step noise and 21 questions were never answered. When the orchestrator's context filled, the model compacted it
and lost detail, instead of a controlled reset from written notes.
- *Now:* the memory layers below. In short: a curated memory heads every brief, questions get ids and stay open until
  answered, agents can see each other's work, and the orchestrator's state lives in files it reloads after a reset.

**Instructions were forgotten.** The owner gave the same instructions dozens of times, and they were still not followed.
A changed rule sat beside the old one, so there were two spending caps.
- *Now:* every owner message is filed as an instruction and stays open until the orchestrator closes it or turns it into
  a standing rule. Rules are reloaded at every session start. A rule has a subject key, and a new rule on the same
  subject replaces the old one.

## How it works

```
 owner
   |
 orchestrator session (Claude Code or Codex)  <-- hooks: session start, each prompt, each tool call, stop
   |                                           <-- watcher: runs every 5 minutes; wakes it when needed
 janus (check, launch, run, gate, land, ...)   <-- janus.toml: hosts, models, budgets, paths
   |-- beads: units, owner instructions, standing rules
   |-- pueue on each machine: agents and build/test jobs, admitted by reservations
   |-- herdr panes / tmux: interactive agents
   '-- board: agents' state, questions and answers, synced to every machine
```

**The orchestrator drives.** One AI session (Claude Code or Codex) is the orchestrator. It decides what to do next:
which unit of work to brief, which model and machine to use, what to land, what to ask the owner. Each unit gets a
brief, its own git worktree and a report. The orchestrator records its important decisions in decision records and its
working state in a handoff document, so a later orchestrator picks up where it stopped and does not reopen settled
questions (see Memory).

**A deterministic script does the checking and the operations.** Bookkeeping is where the model failed, so a script does
it. `janus check` reads the real state (hosts, queues, agents, the board, gate results, git, spend) and prints one line
per problem, and nothing when all is well. Each line names the action that resolves it. The orchestrator's cycle is:
check, act on every line, acknowledge each with its decision (`janus ack`), update its notes. Every operation (launch,
stop, collect, gate, land, clean up) is a `janus` command with its safety checks built in.

**Hooks enforce the role.** Claude Code and Codex call hooks at fixed points, and janus serves them for the registered
orchestrator session only:
- at session start, it injects the handoff, the rules and the session memory;
- on each owner prompt, it files the prompt as an instruction;
- on each tool call, it denies hand operations;
- on stop, it refuses to go idle with open problems or stale notes.

Agents get one hook: they cannot finish until their report has a final verdict.

**Each machine has a job queue.** Agents and build or test jobs run in pueue on the machine itself, not inside an ssh
session, so a dropped connection stops nothing. A job waits in its queue until its reservation fits. Agents run on the
machine where their builds run.

**The message board.** Every agent posts to a board: when it starts, when it waits on jobs, when it is blocked, when it
has a question, when it learned something others need, and when it is done. Steps go in its report, not on the board.
The board exists for three reasons:
- *Reliable, simple messaging.* It is plain files and a shell script. Any agent on any machine can post with one
  command, whatever model or tool it runs. Remote machines write to an outbox that the watcher merges, so a dropped link
  loses nothing.
- *One place to watch.* The owner and the orchestrator see every agent's state in one place. `board.sh summary` groups
  cards by what needs attention; `board.sh digest` tells a person what happened.
- *The orchestrator's ledger of what is happening.* `janus check` reads the board. A question gets an id and stays open
  until answered, and it wakes the orchestrator. Cards of closed units are archived.

### Memory

Each kind of fact lives in exactly one place, chosen by how long it lasts and who needs it. A fact kept in two places
drifts, so everything else links to it.

| Layer | What it holds | Written by | Read by |
|---|---|---|---|
| Owner rules (beads, `bd remember --key`) | standing instructions | the orchestrator, when the owner gives one | the orchestrator at every session start |
| Session memory (`.janus/MEMORY.md`) | about 30 dated lines: decisions, facts, pitfalls that guide agents now | the orchestrator only (`janus remember`, `janus forget`) | every agent, at the head of every brief; the orchestrator |
| Decision records (`.janus/decisions/`) | one file per important decision: what, why, what it costs | the orchestrator | later orchestrators, before deciding anything a record touches |
| Handoff (`.janus/HANDOFF.md`) | live state (generated by `check`) and the orchestrator's notes: goal, units, decisions, next steps | `janus check`; the orchestrator | the orchestrator, at session start and after a reset |
| Board | work in flight: states, questions, answers | agents; the orchestrator | the orchestrator, the owner, other agents |
| Reports and their index (`out/<unit>/REPORT.md`, `out/INDEX.md`) | each unit's result, with a `VERDICT` line | agents; the index by janus | the orchestrator; agents via the index |
| Conversation histories | each agent's transcript, saved at collect | the agent runtime | anyone, by search or tail, never whole |

Old handoff notes move to a history file that is never loaded. Agents propose new knowledge with a `learned` post; the
orchestrator accepts it into memory or rejects it, so memory stays small and correct. The peers file shows who is
changing which files, and `janus check` reports two units changing the same file.

**Why this works.** Each reader gets only what it needs. An agent's brief carries the curated memory and pointers to the
index, peers and transcripts, not their contents. The orchestrator reloads its rules, notes and memory from files at
every session start, so a reset loses nothing that was written down, and the watcher can reset its context on purpose
when it is idle with fresh notes. Nothing important lives only in a model's context. The full map is
[docs/KNOWLEDGE.md](docs/KNOWLEDGE.md); decision records are described in [docs/DECISIONS.md](docs/DECISIONS.md).

## Design choices

- **Standard tools, thin glue.** Queues, issue tracking, terminals and transport already exist and are tested. janus
  joins them and adds only the rules between them.
- **Enforced, not remembered.** Anything that must always happen is a hook or a check. A rule kept only in a prompt does
  not count as prevention.
- **Read the state, not events.** `janus check` looks at what exists now, so a missed event cannot hide a problem.
- **One writer per file.** Only the orchestrator writes memory and notes; only janus regenerates the live state; the
  ledger and admission take a lock. Two writers on one file is how the ledger lost its rows.
- **Park instead of wait.** Waiting costs tokens and holds budget. A parked agent costs nothing and resumes with its
  results.
- **Targeted gates.** Test what a change affects. Known failures are tracked to the unit fixing them, not re-discovered
  by every gate.
- **Extras are optional.** Owner caps, shared-host back-off, automatic fill and branch cleanup are off unless
  configured.
- **No machine assumptions.** The coordinating machine may run macOS or Linux; every host, path, model and key name is
  in `janus.toml`. Nothing in the code names a project.

## Getting started on one machine

You need macOS or Linux, Python 3.11+, git, ssh, rsync, [beads](https://github.com/steveyegge/beads) (`bd`), pueue, the
agent CLIs you will use (Claude Code, Codex, opencode), and herdr or tmux for agents in panes.

```
git clone <this repo> ~/code/janus && cd ~/code/janus
./check                                # the tests
cd ~/code/myproject                    # your project, a git repository
python3 ~/code/janus/janus.py init     # writes a commented janus.toml and .janus/, briefs/, out/
$EDITOR janus.toml                     # this machine, models, routing, where the API key is stored
bd init                                # beads, once
~/code/janus/install --dry-run         # shows every change; then run it without --dry-run
janus doctor                           # after that real install: checks the whole setup and names the fix for each problem
```

`install` builds a tested release, links the `janus` command and the skill, adds the hooks to Claude Code and Codex, and
installs the watcher. Codex needs one manual step: run `/hooks` in Codex to trust them. Then start your agent CLI in the
project directory as the orchestrator (for example, run `claude` there), and inside that session run `janus register`.
This one session is the orchestrator; hooks act only for it (other sessions are left alone — see docs/REFERENCE.md,
section 2). If the session id is not known yet, `register` says `pending` and the next hook event claims it.

To take over an orchestrator that is already running (another session, or another machine), run `janus resume
[--runtime claude|codex] [--here]`: it asks a live orchestrator to stop, syncs the state, and starts a new session in the
chosen runtime — or, with `--here`, registers the current session — with the handoff, rules and a fresh check. Hand back by
leaving the notes current and running `janus register` in the session you keep (see docs/REFERENCE.md, section 5).

## Adding machines

Add a `[hosts.<name>]` entry per machine with its ssh name, architecture, a root folder, and its cores and memory. Run
`install` again: it sets up pueue on each host as a user service, without sudo. Agents and jobs then go to whichever
host has room, and the board syncs to every host. A host shared with other users can be marked `shared = true` so janus
backs off when it is under pressure. Every key is documented in `example.janus.toml`.

## Daily commands

| Command | What it does |
|---|---|
| `janus check` | one line per problem; silent when all is well (run outside the registered session it also prints a closing `(read-only: …)` note; see REFERENCE §7) |
| `janus ack <key> "<decision>"` | acknowledge a problem and record the decision |
| `janus status`, `janus capacity` | the whole picture; room per host |
| `janus launch <unit> --kind implement --tier normal` | start an agent on the brief `briefs/<unit>.md`, on a host with room |
| `janus park <unit>`, `janus resume <unit>` | park an agent on its jobs; resume it |
| `janus collect <unit>`, `janus close <unit>` | bring back a remote unit's work; close a finished unit |
| `janus gate-scope <unit>`, `janus gate <name>` | write a targeted gate; queue it |
| `janus land <unit>` | merge main, gate the merged tree, squash-land |
| `janus remember --fact "<text>" --source <path>` | add to session memory |
| `janus decide "<title>" --by owner\|orchestrator --decision "…" --why "…"` | write a decision record (`.janus/decisions/`) |
| `janus decisions --grep <topic>` | find why a decision was made |
| `janus tidy --dry-run` | list worktrees, branches, files and caches that can go |
| `janus report` | a status report for the owner |
| `board.sh summary`, `board.sh answer <id> <text>` | see the board; answer a question |
| edit `[models.*]` / `[providers.*].enabled` in janus.toml, then `janus doctor` | add a model, or turn a provider off |
| `bd remember "<rule>" --key <subject>` | add or replace a standing rule (reuse the subject key to replace) |
| `janus launch <unit> --resume-of <unit>` | recover a crashed or capped unit from its report |

On a machine running other janus projects, `check` also reports machine-level state (queues, panes, hosts) that is not yours.

Every command, option, hook rule and setting is in [docs/REFERENCE.md](docs/REFERENCE.md), with a troubleshooting table.

## Status

Built, with tests: the hooks, `check`, launch and admission by reservations, park and resume, the ledger, targeted
gates, the board with question ids, session memory, peers and transcripts. Not yet seen working live: the watcher
starting a new orchestrator, the controlled context reset, a remote agent end to end, and a board question waking the
orchestrator. Decision records are written and searched with `janus decide` and `janus decisions`. The open items are
listed in [docs/REFERENCE.md](docs/REFERENCE.md#known-gaps) and [docs/ISSUES.md](docs/ISSUES.md).
