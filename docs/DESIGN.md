# Design

orch makes an AI orchestrator session orchestrate: it dispatches agents, watches them, verifies, lands, cleans up and reports,
and never does the work by hand. Generic for any project, Claude Code and Codex, one machine or many over ssh.

## Principles

- **Enforce, don't remind.** Hooks hold the orchestrator to its role (PreToolUse denies hand operations), keep it awake
  (Stop blocks while problems are unacknowledged or its notes are stale) and file every owner message as an instruction.
- **Check what exists, not events.** `orch check` reads the real state (beads, pueue, tmux, herdr, the board, gate logs,
  host probes, git, the spend ledger) and prints one line per problem, nothing when all is well. The model reads only those.
- **Standard tools, thin glue.** beads (units, rules), pueue (queues), tmux/herdr (agents), ssh/rsync/git, systemd-run
  (memory limits), launchd (watcher). The code is one standard-library package (`orchkit/`) behind one entry point
  (`orch.py`); `board.sh` is a plain-file board.
- **Capacity by reservations.** Each job and agent holds a reservation of cores and learned peak memory; a host admits work
  while the sums fit, pauses on pressure, never kills. Caps exist only when the owner sets them.
- **Agents never pay to wait.** Builds and tests are submitted detached; the agent writes `WAITING <job ids>` and ends its
  turn; orch resumes the same session when the jobs finish.
- **Nothing is lost.** Remote work comes back as a patch plus the output folder before a copy is removed; ledger rows are
  idempotent per run; every report keeps a VERDICT line.
- **Shared knowledge, curated.** The orchestrator keeps a short session memory; agents see it, the peers and the reports
  index at start, and propose new knowledge through the board.

## Components

| Part | What it is |
|---|---|
| `orch.py` | the entry point: seven lines (`from orchkit.cli import main`); the command, its subcommands and the dispatch table are `orchkit/cli.py` |
| `orchkit/` | the package: every subsystem, one module per seam (below); the installed release runs this, never the working tree |
| `board.sh` | a thin shell shim over `orchkit/msgboard.py` (same path, same commands): cards, directions, questions with ids, outboxes on other hosts merged by `sync` |
| `skill/SKILL.md` | the one-page role card both Claude Code and Codex load |
| hooks | one handler (`orch hook <event>`) for SessionStart, UserPromptSubmit, PreToolUse, Stop, for the registered session only, plus an agent Stop check |
| watcher | `orch watch` every few minutes (launchd): check, park/resume, orphans, board alerts, wake or restart the orchestrator |
| `install` | builds a tested read-only release, links it, merges hooks, installs the watcher |

## Package layout (`orchkit/`)

The single stdlib file is split by seam into `orchkit/`, with `orch.py` kept as the entry point; `install`'s release copy
(`PARTS`) includes the package, so the installed command runs the same code. Dependency direction is lower-to-higher and
acyclic: a module never imports a higher one, and a read-only command returns `(rc, lines)` for `cli.py` to print rather than
calling `say()` across a layer.

| Module | Holds |
|---|---|
| `orchkit/__init__.py` | `__version__` |
| `orchkit/context.py` | Err/say/now/ts, find_root/Cfg, clean_env/sh/hrun/X, the registered-session process identity, pq/qp/fl/findkey, Locked, atomic_write, ORCH_SELF/FIXTURE_MARK/NUM, and `Run(dry, ro, all_units, rates)` — one invocation, carried as `cfg.run` (re-exports ps_table/secs/herdr from `platform`) |
| `orchkit/platform.py` | the one OS/tool adapter (owner direction 2026-11-11): process listing (`ps`), the `ps` elapsed-time flavour, the terminal/pane tool (`herdr`), the secret store (macOS keychain), the service manager (launchd/systemd) |
| `orchkit/templates.py` | the nine shell/Python templates orch writes (PROBE, DIRECT_PROBE, CAP_LIB, ADMIT, STOP_GROUP, CLEAN_PUEUE, UNIT_PROCS, FINGERPRINT, CONDENSE) |
| `orchkit/beads.py` | `Beads`, and the unit-record codec (`UNIT_V`, `unit_record`, `migrate_unit`) |
| `orchkit/cap.py` | CAP_DEFAULTS/capv/cap_nums, cap_lib, class_mb/class_secs, record_history, host_cap/next_free/build_room |
| `orchkit/probe.py` | probe/probe_all, probe_extra, pueue, margins/room/cap_line |
| `orchkit/ledger.py` | parse/read/spend/committed/append, reserve_budget/ledger_done, run_cost, settle_run, read_secret/model_ok |
| `orchkit/units.py` | unit_state, verdict, report_path, herdr_agents, ws_id |
| `orchkit/board.py` | board_script/dir/board, board_shipped, board_rows/board_questions/learned_posts, board_findings, board_files/board_alerts |
| `orchkit/msgboard.py` | the board contract itself (run by `board.sh`): one card schema, one name validator, question ids, `summary`/`digest`/`sync` |
| `orchkit/gates.py`, `budgets.py`, `backlog.py`, `memory.py`, `transcripts.py` | the query modules the actions and findings share (gate results and estimates, spend and the waste pause, checklist/beads backlog, the memory file, transcripts) |
| `orchkit/findings/__init__.py` | the `Finding(key, text)` record and `FAMILIES`, the one registry of every legal finding family (doc, urgent, ack_ttl) |
| `orchkit/findings/{hosts,units,work,memory}.py` | one function per finding family; `orchkit/check.py` runs `collect_findings` as the sequence of family calls |
| `orchkit/actions/{jobs,launch,lifecycle,landing,backlog,swarm,tidy}.py` | one module per action group (run/gate/cancel, launch and prompts, stop/collect/close/park/resume, land, backlog/fill, memory/peers/report/index, tidy) |
| `orchkit/hooks.py`, `watch.py`, `setup.py`, `cli.py` | the hook parser and handlers; the watcher; init/doctor/queue/register; the argparse table and dispatch (the only file with subcommand wiring) |

### Seams (each removed a bug class)

- **`Finding` registry** (`findings/__init__.py`): families are the spec; an unregistered key raises; the old URGENT /
  NO_ACK / ACK_TTL prefix lists and `cfg._viol` are gone; `orch check --json` serialises the set.
- **`Run` object** (`context.py`): one invocation (dry, read-only, units and rates read once) carried as `cfg.run`; the `DRY`
  global and `cfg.ro`/`cfg._hs`/`cfg._rates`/`cfg._all_units`/`cfg._park` side-state are gone.
- **Actions return data**: read-only commands return `(rc, lines)` and `cli.py` prints; commands that change the world keep
  reporting progress as they go (a long rsync or merge must not go silent).
- **Board contract in Python**: `orchkit/msgboard.py` is the one implementation; `board.sh` is a shim that execs it next to
  itself, keeping the path every launcher names.
- **Unit record v2**: `beads.py` holds the codec; every reader migrates on read (`v`, `runs`).


## Reuse audit (standard tool vs custom code, per subsystem)

Prefer a tool already required by the skill (pueue, beads, herdr, git, systemd, launchd, flock, or the Python stdlib) over a
new dependency; keep custom code only where no standard tool fits, with the reason. No new dependency is added by the split.

| Subsystem | Standard tool that fits | Decision |
|---|---|---|
| Locking | Python stdlib `fcntl.lockf` (`Locked`, the ledger) and `flock(1)` in the admission script, with a `mkdir` lock fallback for hosts without flock | used |
| Atomic writes | Python stdlib `os.replace` (temp + rename) in `atomic_write`; `O_APPEND` for the ledger | used, except `Cfg.jput`/`Cfg.touch`, which still write in place (seam 2: the atomic-state test is an expected failure until then) |
| Config | Python stdlib `tomllib` for `orch.toml` | used |
| Process supervision | `systemd-run --user --scope` (`MemoryMax`) for jobs already; the ~90-line `enforce` burner killer + `STOP_GROUP` are custom | keep custom only where a cgroup cannot reach; Step 7 deletes the killer in favour of cgroup `CPUQuota`/`MemoryMax` |
| Job queues | `pueue` (already required; `pueued` per host) | used; `tune_pueue` duplicates admission, so keep one (Step 7) |
| Scheduling and placement | custom reservations (cores + learned peak memory) and PSI/load back-pressure over pueue | keep custom: no standard tool models this hobby fleet's mixed hosts; Slurm/Condor are far too heavy |
| Metrics | Python stdlib plus the hosts' own readings (`sysctl`/`vm_stat`, `/proc`) | keep custom: no standard tool reads this fleet's Mac/Linux mix |
| Transcripts and usage | Python stdlib (jsonl read, `cksum`) and a fixed price table | keep custom; no dependency added |
| Sandboxing | `podman` (already supported for jobs: `runner = "podman"`) and `systemd-run` scopes | reuse podman for agents (the Tier-4 owner decision), not a new tool |
| Notifications | `board.sh` (plain files) + herdr prompts + launchd/the systemd timer | used: no daemon, no new dependency |
| The board | plain files (cards, log, questions) with the semantics in `orchkit/msgboard.py` and `board.sh` a thin shim that execs it | used (stdlib Python; `board.sh` stays a callable path, section 9) |
| Test runner | Python stdlib `unittest` (`./check` discovers every `tests/test_*.py`; `install` runs them against the release copy) | used |

New dependencies added: **none**.

**The platform adapter (owner direction 2026-11-11).** Every OS- or tool-specific choice sits behind one module,
`orchkit/platform.py`, so a second platform is a new function, not a new branch in the tool. `process listing`, `stat/date
flavours`, `terminal/panes (herdr)`, `secrets (keychain)` and `the service manager (launchd/systemd)` are there now; `sh` is
imported inside its functions so it stays below `context` (no import cycle), and `context` re-exports `ps_table`/`secs`/`herdr`
so call sites are unchanged. Still to fold in: `containers/scopes (podman, systemd-run)`, host discovery, the
`PROBE` script's macOS/Linux branches, and `install`'s service-manager half.

## State (in the project, under `[project] state`, default `.orch/`)

- `HANDOFF.md`: Live state (regenerated by check) + the orchestrator's notes. `history.md` for old notes.
- `MEMORY.md`: Decisions / Facts / Pitfalls, one dated line each, written only by `orch remember` / `orch forget`;
  `memory-history.md` keeps what was replaced or forgotten.
- acks, open problems, stops, the registered session, run records, the denied-command log.
- Units live in beads (one issue per unit, its record in the notes); owner rules are beads memories (`bd remember --key`).

## Swarm awareness

Headless agents (`claude -p`) see only their prompt and brief. So every brief starts with orch's header: where the agent
runs and how it submits jobs, the session memory, and pointers: the project's agent instructions (`[memory] agent_docs`),
the reports index (`out/INDEX.md`, copied to the board folder), the peers file (`peers.md` in the board folder: running units,
their hosts, branches, changed files, steps, jobs, reports, transcripts) and how to read another agent's transcript
(`orch transcript <unit> --grep|--tail`, never whole). The board folder is synced to every host, so the same files serve one
machine or many. `check` raises `overlap:<a>:<b>` when two running units change the same file.

## Board

Agents post state changes only (status, waiting, blocked, question, learned, done); steps go in their reports. Questions get
ids and stay open until answered; a new question or block wakes the orchestrator at once (`.notify` -> `orch watch --alert`).
`direct all` broadcasts. `learned` posts become memory entries once the orchestrator accepts them. `summary` groups cards by
what needs attention; `digest` tells a human what happened.

## Keeping work moving: the ending policy (spec for the autopilot round)

Owner rule (core): the orchestrator never leaves a unit, job, gate or host broken, held or idle without action; on any error it
fixes, restarts, re-queues or routes around it, and records what it did. Every `orch check` finding names the action that
resolves it. The next round makes the safe actions automatic: the watcher's **autopilot** applies this policy on each pass,
writes one line per action to `.orch/autopilot.log` and the unit's beads comments, posts the change to the board, and leaves
everything it does not cover to the orchestrator (as findings). Each rule acts once per cause (a record in
`.orch/autopilot.json` keyed by unit/job and cause), never on a unit the owner or orchestrator paused (`orch ack <key> hold`),
and within capacity, budget and caps exactly like `orch launch`.

| Situation (finding) | Automatic action | Limits |
|---|---|---|
| Capped: an API run ended at its budget cap without a verdict (`budget`, `wrap_due`) | wrap-up resume once (`[budget] wrapup_usd`), then relaunch with `--resume-of <unit>` and its report, budget from history | at most `[autopilot] relaunch_max` (2) per unit; the waste pause stops it |
| Crashed or gone: the agent ended without a verdict (`unit:<u>:finished` no verdict, `board:<a>:gone`) | relaunch from its report (`--resume-of`), same host when it has room, else another | as above; never when its remote copy is uncollected (collect first) |
| Question whose answer is in memory (`question:<id>`) | answer with the MEMORY.md entry it matches (subject key or exact rule), cite it | only exact subject matches; anything else waits for the orchestrator |
| Job stuck or orphaned (`orphan:job`, `age:`, `parked:<u>` on a job not queued or running) | cancel (files saved), re-queue the recorded job (`.orch/runs/jobs`) on a host with room | once per job; a job that failed twice is reported, not re-queued |
| Host down (`host:<n>` unreachable past `unreachable_min`) | cancel its unstarted jobs and re-queue them elsewhere; running ones wait | never stops running work on a host that may come back |
| Lane idle beside a backlog (`idle:<n>:<lane>:vs`) | move waiting jobs to the free host | same lane kind, local vs remote as today |
| Reclaimable (`tidy:<n>`, `disk:<host>`) | `orch tidy` (its safe rules only) on a schedule | branch and remote deletions only with `[tidy] branches` set |
| Thrash on a shared host (`thrash:`) | already automatic (fixes10 item 3) | |

Out of scope for the autopilot (stays with the orchestrator): landing decisions, owner instructions, rules, briefs, any
destructive action outside the rules above.
