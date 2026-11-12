# janus reference

Details behind the [README](../README.md): install, configuration, every command, the hook rules, capacity, jobs,
landing and troubleshooting. The design is in [DESIGN.md](DESIGN.md); the issue register in [ISSUES.md](ISSUES.md).

## 1. Install and uninstall

```
./install --dry-run        # prints every change, changes nothing
./install                  # run from a directory with a janus.toml (or pass --project DIR)
```

Options: `--dry-run`, `--project DIR`, `--no-hosts` (skip the remote machines), `--hosts-only` (only run `janus install-host`
for each remote host in janus.toml), `--release-only` (only build, test and switch to a new release: the way to make an edit live),
`--rollback` (switch back to the previous release), `--skip-tests`. On Linux, install writes a systemd user timer
`janus-watch.timer` (every 5 min) instead of the launchd agent.

**Releases: nothing live ever points at the working tree.** `install` copies `janus.py`, `skill/`, `example.janus.toml` and
`tests/` into `~/.local/share/janus/releases/<date>-<content hash>/` (files made read-only), runs the checks on that copy
(parse, `--help`, hook calls, `tests/test_janus.py`), and only when they pass switches `~/.local/share/janus/current` to it with
one atomic rename. `~/.local/bin/janus` points at `current/janus.py` and the skill links at `current/skill`, so the toolkit and the
skill change together, at one instant, to a tested copy. A queued `janus api-run` task names the release it was queued from, so
a new release never changes a running task. The five newest releases are kept.

**Making a change live (developers):** edit in the repo (or a worktree), run `./check`, commit, then
`./install --release-only`. A half-edited `janus.py` is never live, and `./install --rollback` undoes a bad release.

What `install` changes:

| Where | Change |
|---|---|
| `~/.local/share/janus/releases/<v>/`, `~/.local/share/janus/current` | the tested release copy, and the link to it (switched by one rename) |
| `~/.claude/skills/janus`, `~/.codex/skills/janus` | symlinks to `~/.local/share/janus/current/skill` (older links into a janus working tree are replaced) |
| `~/.local/bin/janus` | symlink to `~/.local/share/janus/current/janus.py` (make sure `~/.local/bin` is on PATH). Project shims call `~/.local/bin/janus` |
| `~/.claude/settings.json`, `~/.codex/hooks.json` | adds `~/.local/bin/janus hook <event>` for SessionStart, UserPromptSubmit, PreToolUse (matcher `Bash\|Edit\|Write\|MultiEdit\|NotebookEdit\|apply_patch`) and Stop. Existing keys and hooks are kept; a backup `*.bak-janus-<time>` is written first |
| `~/Library/LaunchAgents/com.janus.watch.plist` | copied (not linked) and loaded with `launchctl`; runs every 300 s |
| `~/.config/janus/projects` | the project directory is added; the watcher runs `janus watch` in each listed project |
| Homebrew | `brew install beads pueue`, `brew services start pueue` |
| each ssh host in janus.toml | downloads pueue's static binaries into the host's `bin`, writes and enables a user systemd service `pueued`; no sudo |

An existing path that is not our link is skipped, never overwritten. A failing step prints "failed (continuing)".
Two details in the host step: `arch` must be `aarch64` or `x86_64`, and a host with `pueue_dir` (a shared server) keeps
pueue's files and its user unit under that directory instead of the home directory.

**Codex needs one manual step:** open Codex and run `/hooks` to trust the new hooks. Until then they do not run.

**Uninstall.** There is no uninstall script. By hand:
1. `launchctl bootout gui/$(id -u)/com.janus.watch`, then delete the plist and the project line in `~/.config/janus/projects`;
2. remove the entries whose command ends in `janus hook <event>` from `~/.claude/settings.json` and `~/.codex/hooks.json`
   (or restore the `.bak-janus-*` backup);
3. delete the three symlinks above and `~/.local/share/janus`;
4. optionally `brew uninstall beads pueue`, and on remote hosts `systemctl --user disable --now pueued`.

## 2. Setting up a project

Put a `janus.toml` in the project root (`janus` finds it from any subdirectory; paths are relative to it). Start from
`example.janus.toml`. The sections:

**`[project]`**: `name`, `beads` (the directory holding `.beads/`; run `bd init` there first), `state` (default `.janus`),
and timings in minutes: `cycle_min` (a check/ack cycle is overdue after this, default 15), `deadline_min` (an
unacknowledged problem blocks Stop after this, 30), `silent_min` (30), `reset_tokens` (context size that triggers a
reset, 120000: below the runtime's own auto-compaction).

**`[paths]`**: `board` (script with `post/read/summary/rule/close`), `gate_log` (lines like
`2026-11-11 09:00:00 name EXIT_x=0 ... RESULT: PASS|FAIL`), `briefs` (`<briefs>/<unit>.md`), `out`
(`<out>/<unit>/REPORT.md`), `worktrees` (`<worktrees>/<unit>` is the unit's working directory if it exists; it may be outside the project, e.g.
`../../worktrees`), `checklist` (the project's status checklist, editable by the orchestrator), `state_docs` (old state docs
for `janus init`). **`[placement]`**: optional `"<regex>" = "<host>"` rules matched against a unit's name and brief: the host
where its tests run, where launch places it.

**`[herdr]`** `workspace` and **`[orchestrator]`** `kind`/`args`: the herdr workspace label, and what the watcher starts
when the registered orchestrator is gone. It must match the registered session's kind (`check` reports a mismatch; the
watcher starts the registered kind, with `args` only when the kinds match). The watcher starts one only when all of these
hold: `herdr pane list` answered and the registered pane is not in it, the registered session's process (pid and start time,
recorded at register) is not running, and its transcript has not changed for 20 min. A herdr error or timeout starts nothing.
It then marks the registration pending, so the new session's first hook event claims it. Every prompt the watcher sends
starts with `[janus] `.

**`[hosts.<name>]`**: one per machine. `local = true` for this one; others have `ssh`, `arch`, `root` (where synced
worktrees, briefs and outputs live) and `bin`. Capacity figures: `ram_gb`, `cores`, `reserve_gb` (kept free),
`agent_gb` (per agent), `job_gb` (per pueue job), `min_disk_gb`. Optional `memory_max` (systemd-run limit for remote
API agents) and `pueue_config`. For `janus run` (section 6a): `services_gb` (measured memory the host's own services use;
`ram_gb - services_gb` // `job_gb` sets the initial `jobs`/`gates` parallel limits), `gate_slots` (parallel `gates` tasks),
`legacy_probe` (shell printing `legacy_wait=<n>` and `legacy_run=<n>` for an older job queue, shown by `janus check` while it
drains) with `legacy_slots`, `runner = "podman"` with a `[hosts.<h>.container]` table (image, mount, mount_at, args,
usb_args, usb_lock, host_dirs, clone_base, name_prefixes, cpus), `worktrees`, `job_log`, `run_env` (shell run before every
job), `job_setup` (shell run in the worktree before the command), `sync_exclude`, `cores_per_job` (CPU admission), and on the
local machine `admit_check`/`admit_check_env` and `legacy_lanes` (dirs with a pid file of an older lane the land lane waits
for). Optional `[lanes.<lane>] age_min` overrides a lane's age limit. `example.janus.toml` shows every key.

**`[caps]`** (optional, only when the owner wants them): `total = 6`, and `[caps.host] server = 2`. With no caps the limit
is what each machine can safely hold.

**`[providers.<name>]`**: `enabled`, and optionally `cap_usd`, `ledger`, `budget_per_unit`, the secret store of the API key
(`key_keychain`: a macOS keychain item; `key_file`: a `VAR=value` file that only you can read, mode 600; `key_env`: the
variable, default `ANTHROPIC_API_KEY`) and `cmd` (the agent command; default `claude --bare -p --model {api_id}
--max-budget-usd {budget} --output-format json --dangerously-skip-permissions {prompt}`). A provider is usable while its
ledger is valid and `cap_usd` minus committed spend is at least `budget_per_unit`.

**API runs and the ledger belong to janus.** `janus launch` of an API model reserves the budget (under the ledger's lock:
refused when the ledger is malformed or the cap would be passed; else the run's one `running` row) and queues
`janus api-run <unit> --run <id> ...` in pueue's agents group with an allow-listed environment (no key). `janus api-run` reads the
key from the secret store at that moment, gives it only to the agent process (never pueue, a file or argv), forwards
TERM/INT/HUP to it, and writes the run's one `done` row with the reported cost, or the whole budget when none was reported.
Ledger rows: `ts  unit  running|<cost>  <budget>|done rc=<x>  <run id>`. Every write takes `<ledger>.lock` (an fcntl lock)
and is idempotent: a second running or done row for a run id is never written, so retries and re-runs never duplicate. Spend
is computed per run id (its done cost, or its budget while it runs). `janus ledger check` reports every problem (exit 1 when
malformed); `janus ledger repair` keeps the first running and first done row of each run id, drops unparseable rows, writes a
backup `<ledger>.bak-<time>` first and replaces the file by rename. `janus stop` and `janus close` close a run that ended without
a done row. `janus ledger reconcile [--dry-run]` closes the rest: every run whose agent is gone (its unit closed, the run
superseded by a later run of its unit, its agent ended, or over a day old with no unit) and that has no done row gets one,
`done unknown`, with its budget as the cost (the upper bound, so the cap never undercounts); `janus report` lists them as
reconciled, and `janus check` raises `ledger:<provider>:unreconciled` until it is run. Remote API agents get the key over ssh into a fifo only their task reads; `janus collect` records their cost.
Project launchers (an older `launcher` key) are no longer used; doctor says so.

**`[models.<name>]`**: `provider`, `vendor`, `kind`. `kind = "api"` is an API agent run by janus (`api_id` is the model id
the API takes).
`claude`, `codex` and `opencode` are agents in a herdr pane (in tmux on remote hosts); `args` are extra command-line
arguments. `hosts` limits where a model may run.

**`[routing]`**: ordered lists per tier: `critical`, `normal`, and `review`. The first model whose provider is enabled
and within budget wins. A review skips models of the implementer's vendor.

**Adding a model, or turning a provider off.** Add a `[models.<name>]` entry (provider, vendor, kind; `api_id` for an `api`
model; optional `args`, `hosts`) and name it in `[routing]`; `janus doctor` then checks it (provider enabled, within budget,
its CLI on PATH). To stop using a provider, set `[providers.<name>].enabled = false`: routing skips it, and `janus check`
reports a disabled provider a unit still uses.

Then, in a session that will be the orchestrator:

```
$ janus register
registered 5c2e...        # also creates the status, board and watcher tabs in the herdr workspace
```

If the session id is not known yet, it prints `registered (pending: ...)` and the next hook event from the session claims
it (within 10 minutes). Hooks act only for the registered session; any other session is left alone.

## 3. Day to day: the cycle

The cycle is **check, act, ack, notes**. Do it every few minutes and whenever woken.

**`janus check`**: silent when all is well; otherwise one `[key] problem` line each. Run by the registered orchestrator
session (its session id, or its process among the caller's ancestors), it rewrites the Live state part of `.janus/HANDOFF.md`,
saves acks and open problems, sets the pueue limits and stamps the cycle (`last_cycle`). Run by anyone else (an agent, an
auditor, a shell), `check`, `status` and `capacity` only read: no file is written, no pueue limit changes, no cycle stamp, and
`check` ends with a "(read-only: ...)" line. The watcher is the other writer.
```
$ janus check
[unit:parser:finished] parser finished (done; VERDICT: DONE); collect/close or relaunch
[gate:nightly@09:12:44] gate nightly FAILED at 09:12:44 (x y); re-queue or classify
[board:u7:question] u7 is QUESTION on the board: which baseline? ...; answer with board.sh direct
```
Options: `--brief` (at most 8 lines), `--escalate` (only problems open longer than `deadline_min`).
What it reports: per host and lane (`gates`, `jobs`, `land`), queued work with free slots or a paused group ("STALLED"),
queued or admission-waiting work while the host has room for another job ("IDLE"), a host with free slots in a lane while
another host has a backlog there, lane jobs older than their class age limit (enqueue to now: gates 240, jobs 120, land 240
min), and an older job queue (a host's `legacy_probe`) with waiters while it runs fewer than `legacy_slots`;
unreachable hosts and what was on them; hosts over their margins (key `margin:<host>:<kinds>`, kinds load, memory, disk);
idle room while beads has ready work (units, `janus-log` and instructions are not work: `janus launch` marks every unit
`in_progress`); orphan pueue jobs (including lane jobs labelled `unit:<name>` whose unit is closed), tmux sessions (`janus-*`)
and herdr agents that belong to no open unit; blocked or finished units; every board card in state question or blocked, or
SILENT (over `silent_min`) or GONE, including cards janus did not launch (marked "not a unit"); jobs still waiting under the
old cap rule (`old-rule:<host>`); on the coordinating machine, CPU burners an agent started (over 80 % CPU for 10 min, under an agent or
orphaned) and test or build runs outside the pueue lanes (`localtest:<pid>`); an orchestrator kind in janus.toml that differs
from the registered session's;
disabled providers in use; caps exceeded; spend at a provider's cap; failed gates (last 12 hours) and the same test failing
in two or more gates; uncommitted or unpushed git work; unclassified owner instructions; a stale handoff; the watcher not
running (macOS).

**`janus capacity`**: room per host, measured live (read-only).
```
$ janus capacity
laptop room=2 avail=9.4G load=2.10 psi=- disk=80G agents=1
server room=0 avail=3.1G load=1.20 psi=0.00 disk=60G agents=2 FULL: memory 3.1G free, reserve 3G
```
`psi` is memory pressure stall information: the share of time work stalled on memory (the kernel's avg10 reading for the
host); `-` means the host did not report a figure.

**`janus launch <unit> --kind implement|review|audit|cleanup [--tier critical|normal] [--host h] [--test-host h] [--model m] [--brief f] [--cwd d] [--allow-main] [--budget usd] [--of unit] [--dry-run]`**
```
$ janus launch parser --kind implement --tier normal
launch parser: model=codex host=laptop cwd=/proj/worktrees/parser
launched parser on laptop via herdr
```
A `cleanup` unit frees disk and load, so neither `min_disk_gb` nor the load limit refuses it; memory and caps still apply.
- **Model:** from `[routing]`, or `--model`, which passes the same checks (provider enabled, budget left). Every API launch
  checks `committed + budget <= cap_usd` and reserves the run's "running" row under the ledger's lock before anything is
  queued. A resumed unit continues its original report: `--resume-of <unit>` (its `out/<unit>/REPORT.md`) or `--report
  <path>`, never a path read from the brief's prose; check, close, collect and the Stop hook read that report, and a verdict
  written before the resume started does not count. Every value janus puts in a shell command is quoted, and launch waits a few
  seconds to confirm the run started (or is queued for a slot): a run that dies at once fails the launch with its exit code
  and log, and nothing is reported as launched. `--item <id>` (or an `ITEM: <id>` line) ties the unit to a backlog item.
- **Host:** `--host`; else where the unit's builds and tests run (`--test-host`, a `TEST-HOST: <h>` line in the brief, the
  first matching `[placement]` regex in janus.toml, or the sticky host in `out/linux-host/<unit>`) when the model may run
  there; else the allowed host with the most room.
- **cwd:** `<worktrees>/<unit>` if it exists, else `--cwd`, else the project root. It refuses a cwd another open unit uses
  (a local review or audit may share it) and a main checkout that has linked worktrees (unless `--allow-main`).
- **Order:** the beads issue is created and marked `in_progress` first; a failed start closes it with "launch failed".
- **Environment:** every agent gets `JANUS_ROLE=agent`, `JANUS_UNIT`, `JANUS_REPORT` and `JANUS_ROOT`. `pueue add` always runs with
  an allow-listed environment (PATH, HOME, USER, LANG, TMPDIR, SSH_AUTH_SOCK ...) plus those, never the caller's: pueue stores a
  task's whole environment in its state file, so a caller's `ANTHROPIC_API_KEY` or session token would land on disk.
- **Remote brief paths:** every absolute path the brief names must reach the agent there. A path in the unit's tree becomes
  the same path in its copy; a file under the project root (reports, logs, briefs) is copied to `<root>/ctx/<unit>/` and the
  brief rewritten to the copy (a named project root becomes that folder, and its `B/out/x.md`-style files are copied too); a
  missing path under `out/<unit>` becomes the host's; any other path must exist on the host. Otherwise the launch is refused
  before anything is recorded, naming each path.
- **Remote:** the worktree goes to `<root>/worktrees/<unit>` (and a copy to `<root>/base/<unit>` for collect); the brief gets a
  header naming the host-local test command `<root>/run/<unit>.lane '<cmd>'`, which runs the command through that host's
  pueue `jobs` lane with janus's admission and `MemoryMax`, waits and exits with its code. The key is sent from a detached
  ssh with no time limit, so a full `agents` group queues the task instead of failing the launch. Remote tmux agents run under
  `systemd-run MemoryMax` too.
- **herdr:** each unit gets a tab `<unit> · <model> · <host>`; for a pueue unit it follows `pueue follow <task>` (over ssh for a
  remote one). For a herdr agent, launch answers startup dialogs (Claude folder trust, Codex update and trust prompts) and
  reports any it cannot pass.
- **Board:** the first card's check is `herdr <unit>`, `pueue <host>:<task>` or `tmux <host>:janus-<unit>`.
It refuses with `janus: refused, no host has room (...)` when nothing fits. The unit name must match `[a-z][a-z0-9_-]{0,31}`
and the brief `<briefs>/<unit>.md` must exist. For a review, `--of <unit>` makes it avoid the implementer's vendor.

**`janus stop <unit>`**: ends the unit's whole process tree: `pueue kill` or `tmux kill-session`, its lane jobs (label
`unit:<name>`), then TERM (and after 5 s KILL) to every process group of every process under its task or pane and of every
process whose environment holds `JANUS_UNIT=<unit>` (detached children: `nohup`, `setsid`, `& disown`). It confirms none is
left, else exits 1.
```
$ janus stop parser
stop parser (pueue on laptop)
stopped: no process of the unit is left
```

**`janus collect <unit>`**: for a remote unit, makes `out/<unit>/changes.patch` on the host (the agent's tree against the copy
it was sent), brings back `out/<unit>/` (patch, REPORT, logs) and records API cost in the ledger. It never writes over the
local checkout; apply the patch in the unit's worktree with `git apply --3way`. Local units: nothing to collect.

**`janus close <unit> [--force] [--reason text]`**: for a unit with a worktree of its own, refuses on uncommitted or unpushed
work (unless `--force`). A unit without one (cwd is the project root, or shared with another open unit) skips the git checks
and needs a final VERDICT line in its REPORT instead; `--force` is refused there. Then it closes the unit's tab, removes the
remote copy unless another open unit on that host uses it, removes only its own pueue task, and closes the board card and
the beads issue.

**`janus board-close <agent>`**: archives a board card that is not an open unit (cards from other launchers); `board.sh close`
by hand is denied.

**`janus init`**: in a folder without a janus.toml, writes a commented starter one (from `example.janus.toml`, this machine
named after its host name) and creates `.janus/`, `briefs/` and `out/`. In a project that has one, turns the old state docs in
`paths.state_docs` into a one-line pointer to `.janus/HANDOFF.md`, keeping their text in `.janus/history-<name>`.

**`janus doctor [--no-hosts]`**: checks a setup end to end and names the fix for each problem: tools (git, ssh, rsync, bd,
pueue and pueued, herdr, tmux, each model's CLI), the installed release and skill links, the hooks, the watcher, the config
(hosts, models, routing, paths), beads, each API provider's secret store (the key is read, never shown) and ledger, and every
host over ssh with its pueue. Exit 1 when anything fails.

**`janus ledger check|repair`**, **`janus backlog import|list`**, **`janus queue`**, **`janus cancel`**: sections 2, 6a and 6b.

**`janus commit-state [-m msg] [--dry-run]`**: runs `git add -A`, then scans exactly the staged files for secrets: Anthropic,
OpenRouter, OpenAI (`sk-proj-`), hex `sk-` keys, AWS access key ids, GitHub and Slack tokens with a realistic length, and
private-key blocks. A bare prefix mention passes. On a hit it unstages again, refuses and lists the paths, never the values.
Otherwise it commits and pushes.

**`janus ack <key> "<decision>"`**: acknowledges a problem for 4 hours (margin problems 30 min), until the problem clears
(a cleared problem's ack is dropped, so it comes back if it recurs), and records the decision as a beads comment (on the
unit's issue for `unit:<name>:...` keys, otherwise on a `janus: decisions` issue).
```
$ janus ack unit:parser:finished "collected and closed, gate re-queued"
acked unit:parser:finished
```

**`janus status`**: prints the Live state (hosts, units, ready work, spend, problems) without counting as a cycle.

**`janus decide` / `janus decisions`**: the decision records (docs/DECISIONS.md). `janus decide "<title>" --by owner|orchestrator
--decision "<one sentence>" [--why "<ruling>"] [--context ...] [--options "<option and trade-off>" ...] [--consequences ...]
[--supersedes <n>] [--instruction <beads id>]` writes the next numbered record, marks the record it supersedes, and regenerates
`.janus/decisions/INDEX.md`; `janus decisions [--grep <regex>]` lists the records, newest first. The index is injected at
session start, so the record chain is in front of the orchestrator before it decides anything those records touch.

**Notes.** After each decision, update the *Orchestrator notes* part of `.janus/HANDOFF.md` (see section 5).

**A unit failed — what now.** The command depends on how it ended:
- *Crashed or capped with no verdict* (`unit:<u>:finished`): relaunch from its report with `janus launch <u> --resume-of <u>`;
  with `[autopilot] enabled` the watcher does this for a local unit.
- *Finished with a verdict*: `janus collect <u>` for a remote unit, then `janus close <u>`.
- *Done but not landed* (`landing:<u>:unlanded`): `janus gate <name>`, then `janus land <u>`.
- *Parked, not failed* (`VERDICT: WAITING`): its jobs are still running; the watcher resumes it when they finish (`janus
  resume <u>` resumes it by hand).
- *A stuck lane job*: `janus cancel <host>:<task>`.

## 4. What the hooks enforce, and why

One handler, `janus hook <event>`, serves both Claude Code and Codex (same event names and JSON). It only acts for the
registered orchestrator session, and finds the project from that session id (the project in `~/.config/janus/projects` whose
`.janus/session` holds it), never from the session's current directory, so `cd` elsewhere changes nothing. Only `janus register`
changes the registration (it records the session's process and kind, claude or codex); a pending registration (`janus
register` with no session id, or the watcher's new orchestrator) is claimed by the first hook event from that project's
directory within 10 min. No environment variable takes a registration over. Agents started by `janus launch` run with
`JANUS_ROLE=agent` and get only the agent rule.

| Event | Orchestrator | Why |
|---|---|---|
| SessionStart | injects HANDOFF.md (the Live state of the last check) and `bd prime` (standing rules), and tells the session to run `janus check` first; it does not probe hosts (the hook has 60 s) | a fresh or cleared session resumes without being re-told |
| UserPromptSubmit | files the prompt as a beads issue labelled `instruction` (except prompts janus sends, which start with `[janus] `, the scheduled `janus cycle:` prompt, slash commands such as `/clear`, and background-task notifications containing `<task-notification>` or `[SYSTEM NOTIFICATION`) | nothing the owner says is lost; `check` lists it until closed or turned into a rule |
| PreToolUse | parses each shell command and judges every part of it: heredoc bodies are data unless fed to a shell or `ssh`; `$( )`, backticks and `<( )` are judged as commands; the rest is split on `;` `&&` `\|\|` `\|` `&` and newlines; each part is judged by its command word after wrappers (`sudo`, `env`, `timeout`, `nice`, `nohup`, `xargs`, `command`, `exec`, ...) and any path are stripped, and `bash/sh -c`, `eval` and `ssh <host> <cmd>` are judged recursively. Denied: `rm -r` (any spelling), `kill`/`pkill`/`killall`, `sed -i`/`perl -i`, `git reset --hard`, `git clean`, `git push --force`/`+ref`/`--delete`, `git checkout -- .`, `git restore`, branch/stash/worktree deletion, `sparse-checkout`, `find -delete`/`-exec rm`, `rsync --delete`, `truncate`/`dd`/`mkfs`/`reboot`, `tmux kill-*`, `podman/docker rm/kill/stop`, `launchctl bootout`, `systemctl stop/restart`, interpreter one-liners that delete or kill (`shutil.rmtree`, `os.kill`, ...), `pueue` other than `status/log/follow/wait/group`, `bd` other than its read and issue-tracking commands (`bd delete`, `purge`, `compact`, `sql` ... are denied), `herdr pane/tab/workspace close`, `herdr pane run/send-text/send-keys`, `board.sh close` (use `janus board-close`), `janus close --force` (allowed on a unit's own worktree: not the project root, no other open unit's cwd, not a main checkout), loops over worktrees whose body changes files or runs janus commands other than `janus ack` and the read commands (only the commands inside the loop count), and shell redirections that write into the project or the worktrees outside the allowed paths. Nothing passes because it starts with `janus`, `bd`, `pueue`, `herdr` or `board.sh`. File edits (Edit/Write/MultiEdit/NotebookEdit, and `apply_patch` by the paths in the patch) are allowed only under the state dir, the briefs dir, `janus.toml`, `paths.checklist` and `paths.state_docs` | the orchestrator orchestrates; changes go through `janus` or an agent |
| Stop | blocks going idle while a problem is unacknowledged past `deadline_min`, or the handoff notes are stale | keeps it working and the notes current. After 3 blocks in a row it lets go, so it cannot loop forever |

Agents: their Stop is blocked until `REPORT.md` (path in `JANUS_REPORT`) has line 1 no longer `VERDICT: PENDING`, a final
`VERDICT: DONE|BLOCKED|FAILED` line, and a mention of exit codes.

A denial reads: "Orchestrators do not operate by hand (...). Use janus launch/stop/collect/close, or brief an agent."

## 4a. Findings and their actions

Every line `janus check` prints is `[key] problem`, and each names the action that clears it. This section is the reference
for those keys: one short recipe per finding — what it means and what to do — so the orchestrator can act without guessing.

Every `janus check` line names the action that resolves it. Act, then `janus ack <key> "<what you did>"`.

- **Memory.** `.janus/MEMORY.md` (Decisions / Facts / Pitfalls) heads every agent's brief; you are its only writer. Remember at
  once every decision, fact or pitfall that affects more than one agent: `janus remember --decision|--fact|--pitfall "<text>"
  [--source <path|sha|job>] [--key <subject>]` (a fact needs a source; the same `--key` replaces the older entry).
  `janus forget <n>` what no longer guides anyone; act on `memory-long`, `memory-source:`, `memory-review:`.
- **Decision records.** A ruling that constrains later work gets a record at once: `janus decide "<title>" --by
  owner|orchestrator --context ... --options ... --decision ... --why ... [--supersedes <n>] [--instruction <id>]`
  (`.janus/decisions/NNNN-title.md`, never edited; a reversal supersedes it). `janus decisions --grep <topic>` searches a
  record. A question for the owner: `janus ask "<question>" --recommend ...`, then `janus answer <n> "<answer>" [--decide]`.
  Act on `owner-question:<n>` and `decision-missing:<id>`.
- **Questions.** A `question:<id>` is answered first: check `janus memory`, `janus decisions --grep <topic>` and
  `bd memories <word>`, then `board.sh answer <id> <text>`. If the answer holds for every agent, `janus remember` it and
  `board.sh direct all <text>`. A `learned:<agent>:<n>` is a proposal: `janus remember ... --learned <agent>:<n>` or
  `janus learned --reject <agent>:<n>`.
- **Swarm.** `janus peers` shows who changes what; on `overlap:<a>:<b>` tell both (`board.sh direct`) or land one first.
  `janus transcript <unit> --grep <re>` reads what another agent met (never whole). `board.sh digest --since 1h` is the short
  story for the owner.
- **Stop orphans, stalled agents and thrashing jobs:** `janus stop <unit>`. On a `shared = true` host the watcher itself stops
  and re-queues this project's newest job when memory pressure stays high (`thrash:<time>` says what it did; `janus thrash
  --dry-run` shows the hosts).
- **Re-queue failed gates;** `janus collect <unit>` results of finished or returned remote units.
- **Land green units;** `janus close <unit>` once committed and pushed (`--force` only on the unit's own worktree).
  `janus tidy --dry-run`, then `janus tidy`: removes clean landed worktrees of no open unit, landed branches (local and remote,
  `[tidy] branches`), old run files, our stopped containers, remote copies of closed units with nothing uncollected, old test
  roots, cache entries over a host's disk budget, panes of closed units and gone cards; never by hand. On `tidy:<n>` or
  `disk:<host>`, run it.
- **Launch within safe capacity and any owner caps:** `janus launch <unit> --kind implement|review|audit|cleanup --tier
  critical|normal [--host h] [--test-host h]`. `janus capacity` shows room; a full host refuses (a `cleanup` unit is exempt
  from the load and disk limits). Reviews use a different vendor from the implementer (`--of <unit>`). Each unit needs its own
  worktree. A remote launch copies the files the brief names under the project root to the host and rewrites the brief; it
  refuses a brief naming a path the agent there cannot reach (move the file under the project root, or name it relative to the
  tree).
- **Answer every board question** (`board.sh answer <id> <text>`) and blocker (`board.sh direct <agent> <text>`) `check`
  reports; archive cards that are not units with `janus board-close <agent>`.
- **Classify owner instructions:** close the beads issue as done, or make it a standing rule with `bd remember "<rule>" --key
  <subject>`. A rule that changes an older one **replaces** it: reuse that rule's key (`bd memories <word>` finds it), never
  add a second rule beside it; `check` raises `rule-subject:<subject>` when two rules share a subject (keep one key,
  `bd forget` the rest). A ruling that is a decision gets a record at once (`janus decide ... --instruction <id>` links and
  labels it); a decision-labelled instruction cannot be closed without one (`check` raises `decision-missing:<id>`).
- **Keep the backlog in beads** (`janus backlog import` after the checklist changes). On `idle-backlog`, write briefs for the
  listed items and launch them with `--item <id>`. On `landing:<unit>`, gate the finished unit (`janus gate-scope <unit>`
  writes a targeted gate from the project's scope hook, then `janus gate <name> --unit <unit>`) or land it once its gates
  passed. A gate with only known failures (`[landing] known_failures`) passes; on `known-failure:<id>` launch the unit that
  fixes it at the root (or remove the entry); on `known-failure:<id>:passes` remove the entry.
- **Resume** a stopped unit with `--resume-of <unit>` so it continues that unit's report. Cancel a lane job that outlived its
  intent with `janus cancel <host>:<task>`; see queues with `janus queue`, never `ssh <host> pueue`.
- **Quality:** every brief header points at the project's rules (`[quality] rules_file`) and states the fail-first test rule;
  agents also write `report.json` beside REPORT.md. On a project with `[quality] review_model`, run `janus quality review
  <unit>` before `janus land` (its findings block the landing until fixed, or `--override '<reason>'`); on `test-signal:<gate>`
  scope or trim the slow gate. `janus soak` (read-only) checks the read paths on a live project.
- **Ledger:** on `ledger:` findings run `janus ledger check`, then `janus ledger repair` (on `ledger:<p>:unreconciled`,
  `janus ledger reconcile`); launches are refused until the ledger is valid. On `script:<pid>`, stop that process and relaunch
  it.
- **Token rules.** Read `janus` output, never panes, logs or board files directly (`janus status` for the whole picture).
  Briefs are short: goal, scope, tier line, report path, done-criteria. Spend sparingly; critical work goes to the top models
  only.
- **Handoff.** `.janus/HANDOFF.md` is your working memory: Live state (generated) plus your notes (goal, per unit why and what
  done means, decisions with reasons latest first, next steps, things to watch), under 150 lines; move old notes to
  `.janus/history.md`. Update the notes after every decision and before going idle. After a reset or `/clear`, SessionStart
  reloads it with the rules and `janus check`; resume from it.
- **Use every machine.** The machines exist to speed work up: each agent runs on the host where its builds and tests run, and
  jobs go to whichever host has room. `janus launch` places agents by test host and measured room, and `janus check` reports
  hosts idle while work waits and lanes free on one host while another has a backlog: act on those lines.

## 5. Handoff, instructions, rules, memory and the board

**Handoff.** `.janus/HANDOFF.md` is the orchestrator's working memory, in two parts:
1. *Live state*, between `LIVE:BEGIN` and `LIVE:END` markers, regenerated by `janus check`/`status`. Never edit it.
2. *Orchestrator notes*, written by the orchestrator: goal and scope, per unit why and what done means, decisions with
   reasons (latest first), next steps, things to watch. Keep it under 150 lines (`check` flags more); move old notes to
   `.janus/history.md`, which is never injected.

The notes count as stale only after a real event newer than them: a launch, a close, a landing, an owner instruction or a
decision-class ack (`janus ack --decision-class`, or a `unit`, `landing`, `question`, `instructions`, `ledger`, `parked`,
`provider`, `cap`, `learned` or `overlap` key; a `janus remember --decision`). Routine acks, collects, parks and the clock do
not. The Stop hook then blocks.

**Context reset.** The context size is the last turn's token usage in the transcript. Over `reset_tokens`, a Stop that
passes (nothing late, notes fresh) is the safe boundary: the hook starts `janus watch --reset`, which sends `/clear` once the
turn has ended, then the wake prompt; SessionStart reloads the handoff, rules and check output. The watcher asks an idle
orchestrator to finish its notes first. Every skipped reset says why in `.janus/watch.log`; `check` raises `context` while
the context is over the threshold and no reset was asked.

**Owner instructions and standing rules.** Every owner prompt becomes a beads issue labelled `instruction`. Classify it:
close it (it was a one-off, now done) or make it a standing rule with `bd remember "..."`. Rules come back at every session
start through `bd prime`, so the owner never has to repeat them.

A rule that changes an older one replaces it: `bd remember "<rule>" --key <subject>`, reusing the old rule's key (`bd memories
<word>` finds it). `check` raises `rule-subject:<subject>` when two rules share a subject.

### Memory

`.janus/MEMORY.md` is the session's curated memory: three sections (Decisions, Facts, Pitfalls), one dated line per entry. The
orchestrator is its only writer:
- `janus remember --decision|--fact|--pitfall "<text>" [--source <path|sha|job>] [--key <subject>]`: a fact needs a source;
  an exact duplicate is refused; an entry with the same `--key` replaces the older one (moved to `.janus/memory-history.md`).
- `janus forget <n|subject|text>` moves an entry to the history; `janus memory` lists them; `janus memory --keep <n>` re-dates
  one after review. Agents cannot write it (`JANUS_ROLE=agent` is refused); they propose with a `learned` board post.
- `check`: `memory-long` over `[memory] cap_lines` (30), `memory-source:<n>` when a cited path is gone, `memory-review:<n>`
  for an entry older than `review_days` (14), `memory-subject:<key>` for two entries on one subject.
- Every agent's brief starts with it, under "Session memory (curated by the orchestrator; your brief wins on conflict)", and
  the orchestrator's SessionStart hook injects it with the handoff and `bd prime`.

### Swarm awareness

Headless agents (`claude -p --bare`) skip CLAUDE.md, memories and hooks: they see only their prompt and brief. So janus's brief
header carries, after the run rules and the memory, pointers (never content):
- the project's agent instructions (`[memory] agent_docs`, default `AGENTS.md`: the root one and the one in each folder touched);
- the reports index: `out/INDEX.md` (one line per unit: verdict, result, report path), written by `janus index` and at every
  `janus commit-state`, with a copy `index.md` in the board folder;
- the peers file: `peers.md` in the board folder, written every check/watch cycle: one line per running unit with host,
  model, branch, files changed so far, current step, jobs it waits on, report and transcript location (`janus peers` prints it);
- how to read another agent's transcript: `janus transcript <unit> [--grep <re>] [--tail N]` finds the unit's sessions from its
  run records, reads them on the host that holds them (Claude Code jsonl, Codex sessions; opencode keeps its own database and is
  not read) and prints condensed text: messages and one-line tool summaries. `janus collect` saves `out/<unit>/transcript.md`.
- the rule: read peers and the index when your task touches another unit's area; read a transcript only to solve a problem
  another unit met, by grep or tail, never whole.

`check` raises `overlap:<a>:<b>` when two running units change the same file. Because the index and peers live in the board
folder, the board's sync carries them to every host: the same files serve one machine or many.

### Board

`paths.board` unset means the `board.sh` shipped with janus (a project may keep its own script there). Its files live in
`paths.board_dir` (default `board/`); check writes `.master`, `.hosts` (from janus.toml) and `.notify` there.
- Agents post **state changes only**: `status` at start, `waiting <job ids>` when parked (never SILENT or GONE; janus posts it
  when it parks a unit), `blocked`, `question`, `learned <fact and source>`, `done <VERDICT line>`. Steps go in the report.
- A question gets an id (`q12`) and stays "needs answer" until the orchestrator answers it: `board.sh answer <id> <text>`, or
  `board.sh direct <agent> <text>` (answers that agent's open questions). `check` raises `question:<id>` until then. A new
  question or block runs `janus watch --alert`, which prompts the orchestrator at once (each one once).
- `board.sh direct all <text>` broadcasts to every open card (it answers no question); remember a standing one with
  `janus remember`.
- A `learned` post shows in `check` as `learned:<agent>:<n>` until `janus remember ... --learned <agent>:<n>` or
  `janus learned --reject <agent>:<n>`.
- `board.sh summary` groups cards: needs answer, question, blocked, working, waiting, done. `board.sh digest [--since 1h]`
  tells a human what landed, started, finished, was asked, answered, failed or learned.
- Cards of closed units are archived by check. Other hosts post to an outbox under their root; `board.sh sync` (the watcher
  runs it each pass) merges the outboxes and pushes the board, and the script itself, to `<root>/board/` and `<root>/bin/`.

### The owner takes over, and hands back

**The orchestrator is just the registered session.** `janus resume [--runtime claude|codex] [--here]` takes over safely: if a
registered orchestrator is alive it asks it to stop and waits a bounded time, so two never run at once (the state writer lock
is the guard); it commits and pushes the state folder first, and pulls it if this machine is behind its remote; running agents
and jobs are untouched — they belong to their hosts' queues — and parked units keep their jobs. `--here` registers the current
Claude or Codex session as the orchestrator; without it, `resume` starts one in the requested runtime in the project's herdr
workspace (or tmux). Either way SessionStart reloads the handoff, the standing rules, the decision index and a fresh check, so
the new session starts with nothing lost. Under Codex, confirm the hooks are trusted (`/hooks`) once.

To work in the session yourself, `janus resume --here` (or `janus register`) makes the current session the orchestrator; its
hooks still file every prompt as an instruction. Everything that must survive is on disk: rules (`bd prime`), session memory
(`.janus/MEMORY.md`), decisions (`.janus/decisions/`), and live state plus your notes (`.janus/HANDOFF.md`).

**Hand back** by leaving the notes current and running `janus register` in the session you will keep; SessionStart then
reloads the handoff, rules and check output. The watcher starts a new orchestrator only when the registered pane and process
are gone and the transcript has been quiet for 20 minutes (section 2), so a live session is never doubled.

## 6. Multiple machines, capacity and network outages

### Capacity in brief: use all compute, never overload it

- **Admission by reservations.** Every lane job on every host waits in `runs/cap.sh`'s rule: it holds `cores_per_job` cores and
  its job class's memory (the max of the class's last 5 recorded peaks + 15 %, from `runs/history.tsv` on that host; `job_gb`
  until learned) and starts while the sums fit `cores` and `ram_gb - services_gb`. Agents hold the agent class's figure.
- **Back-pressure.** Memory PSI over `psi_pause`, or load over `load_pause` x cores for `hold_s`, pauses admission; running jobs
  are never killed; `janus check` reports `pressure:<host>`.
- **One scheduler per host.** `check` raises `scheduler:<host>` for another job runner or a job that holds no reservation.
- **Agents count against build capacity.** A unit records its build host; `janus launch` refuses, saying when, unless a build
  reservation or a queue position (one queued job per running job) is free there.
- **Fill.** `janus fill --dry-run` lists what fits now and why it stops; `janus fill` launches it; `[fill] watcher = true` lets the
  watcher do it. `check` reports `idle-with-backlog`.
- **Autopilot (opt-in: `[autopilot] enabled = true`; off by default).** The watcher applies the ending policy's fixed rules
  itself (docs/DESIGN.md, "Keeping work moving"): collect finished remote units; relaunch a crashed unit, or a capped one after
  its wrap-up run, from its report as `<unit>-r<k>` (same brief, tree and report, through `janus launch`: capacity, budget and
  the waste pause apply; at most `relaunch_max`, default 2); answer a board question that names a MEMORY.md subject exactly;
  cancel a job past its age limit and re-queue it once on a host with room; re-queue the unstarted jobs of a host down over
  `host_down_min` (20) elsewhere (the old copies are cancelled when it is back); archive finished cards. Always on: resume
  parked units, the wrap-up resume, cancel orphan jobs. Each action is recorded once per cause in `.janus/autopilot.log` and
  `autopilot.json`, as an ack of its finding with the reason, and on the unit in beads. `janus ack <key naming the unit> hold`
  keeps the autopilot off that unit. Everything else stays with the orchestrator.
- **Budgets from history.** A unit's default budget is the median cost of its kind and tier + `[budget] margin_pct`; `budget80:<unit>`
  warns at 80 % with a stale report.
- **Landing.** `janus land <unit>` merges the landing branch (stops and dispatches `merge-<unit>` on a conflict), gates the merged
  tree on `[landing] final_gate_host`, then squash-lands.

### Hosts, queues and outages

Every host in `janus.toml` can run agents. `launch` picks the host with the most room. Room is by reservations (see
"Capacity" below): the memory for reservations (`ram_gb - services_gb`) minus what running jobs and agents reserve, over the
agent figure (learned, else `agent_gb`); none while admission is paused by back-pressure (memory PSI over `psi_pause`, or
load over `load_pause` x `cores` for `hold_s`), or while disk is under `min_disk_gb` (disk does not hold back a `cleanup`
unit). Every agent holds a reservation file (`runs/agent-<unit>.res`), API and interactive alike. Caps reduce it further. `check` reports "host over its margins" for low disk or high load even when no units are ready. On macOS the probe takes the larger of two readings, `kern.memorystatus_level` (memory_pressure's free percentage, read with sysctl) x `hw.memsize`, and `vm_stat` free + inactive + speculative + purgeable pages, so one reading of 0 under load does not make a macOS host look full; with no reading at all the memory limit is skipped for that probe and load from `sysctl -n vm.loadavg`. An unreachable host has room 0.

Work belongs to the machine's own queue, not to an ssh session:
- **pueue** runs on each machine (launchd/brew on macOS, a user systemd service on Linux). API agents run as
  tasks in the `agents` group and keep running if ssh drops. The watcher and the orchestrator's own `janus check` also set the `jobs` group's parallel limit
  to `cores // cores_per_job` (admission by reservations decides what starts), and the `agents` group's to the running
  agents plus the agents that fit by reservations, capped by `[caps]`.
- **tmux** holds interactive remote agents (session `janus-<unit>`); the herdr tab only attaches to it with
  `ssh -t host tmux attach`, so a dropped view does not stop the agent.
- **systemd-run** with `MemoryMax` (from `memory_max`) wraps remote API agent tasks, so the kernel stops one that outgrows
  its share.
- A unit is stopped only by `janus stop <unit>`, never because a connection dropped. Probes use ssh `BatchMode` with
  5 s connect timeouts.

**Shared hosts that thrash.** A host shared with other users can set `shared = true` (with `thrash_psi`, memory PSI avg10,
default 40, and `thrash_min`, default 5). When its memory pressure stays over `thrash_psi` for `thrash_min` minutes, the
watcher stops this project's newest admitted job there (only a job `janus run` recorded in `.janus/runs/jobs` for that very
task, running past admission: its files saved, `pueue kill`, its container or scope stopped by name), re-queues it on another
host where admission has room (else on the same host, where it waits for admission), tells its unit on the board, and
`janus check` reports `thrash:<time>`. Nothing that is not this project's is touched. `janus thrash [--dry-run]` shows each
shared host's pressure and what would be done.

During an outage `check` shows `host X unreachable since <time>` and the units there; their state is "unknown", not
failed. When the host returns, `check` reports finished units and `janus collect <unit>` pulls the results back.

Remote API agents get the key from the provider's secret store (`key_keychain` / `key_file`) over ssh stdin into a
private fifo, which the task removes after reading; it is never written to a file or command line. No `pueue add` gets the
caller's environment (see launch), so the key never reaches pueue's state file, even when an agent that holds it in its
environment queues a job. Remote API agents need `claude` on the host.

## 6a. Build and test jobs: `janus run`, `janus gate`, `janus collect <host>:<task>`

One way to run a build or test job on any machine: that machine's pueue, never an ssh connection.

**Hosts are discovered, not assumed.** `janus hosts [--refresh] [--json]` shows what each host is: OS, arch, cores, memory, disk,
tools (pueue, podman, docker, systemd, launchd, sandbox tool, agent CLIs), agent logins and devices (usb, video, gpu, kvm),
cached in `.janus/hosts-facts.json` and refreshed by `janus check` after `[capacity] discover_h`; a host's `cores` and `ram_gb`
default to what was found. Per job class it also shows runs, expected run time, learned cores, CPU and queue wait (p50/p95).
**`janus install-host <host> [--dry-run]`** sets up a machine named in janus.toml: folders under its `root`, pueued as a user
service (systemd, linked from `pueue_dir` on a shared server; brew services on macOS), a copy of janus in `<root>/janus` for
read-only commands, and the heartbeat watchdog when `[heartbeat] watchdog` names it. The coordinator may be macOS or Linux
(launchd agent or systemd user timer; keychain, secret-tool or pass; herdr or tmux): those choices live in `janus/platform.py`.

**`janus run --needs <requirements>`** instead of `--host`: `linux,x86_64`, `os=linux`, `arch=aarch64`, `feature=podman+cargo`,
`device=usb`, `mem=4G` (bare words are features or devices; `[hosts.X] features`/`devices` add what discovery cannot see). The
job goes to a host that meets them and has room by its own admission (the coordinator too); when none has room, a placement
task waits in the coordinator's `place` group and dispatches it to the first host with room (`janus cancel` of it follows the
job). `--dry-run` prints each host's verdict. A job given `--host` stays there (a thrash stop re-queues it on the same host).
**`--broad <reason>`**: a jobs-lane run that matches `[capacity] broad_patterns` or whose class takes `broad_min` minutes is
refused without it; with it, the run gets nice 19, pueue priority -5 and goes after shorter jobs.

**`janus run --host <h> --lane gates|jobs|land [--unit <u>] --wt <worktree> [--subdir <d>] [--usb] [--priority N] [--detach] -- <cmd>`**
- refuses a worktree with a merge in progress, unmerged files or conflict markers; in the gates and land lanes (and `janus
  gate`) also one more than `[landing] max_behind` (default 10) commits behind the landing branch, never an agent's own jobs
  lane work (`--allow-unmerged` overrides).
- `--detach` (alias `--no-wait`) queues it, prints `<host>:<task>` and returns: agents then write `WAITING <ids>` in their
  report and end the turn; the watcher resumes them with the results (section 6d).
- syncs the worktree to a remote host: `rsync -rlpgoD --checksum --delete` with no `-t` (a changed file gets the transfer
  time as its mtime, so cargo never reuses an artifact built from older content), without the host's `sync_exclude`; a
  podman host first reflink-clones `container.clone_base`. Every `paths.sync_require` path must arrive.
- writes the job as `<root>/runs/<jid>.sh` and queues it in the host's pueue group: `gates` (priority 10) or `jobs`; `land`
  is the local machine's landing lane (parallel 1). Groups are created on first use (`jobs` and `gates` with one slot per job cap).
- admission on every host and lane is by reservations: the job waits in its pueue task, one job at a time under
  `runs/admit.lock` (a pid-owned lock dir where there is no `flock`), until its reservation fits: running jobs' cores +
  this job's cores (its class's learned cores: CPU seconds / run time of its last runs, 1 to `cores_per_job`) <= `cores`, and their memory + the agents' + this job's class figure (the max of its class's last
  `history_n` recorded peaks + `margin_pct`, `job_gb` until learned) <= `ram_gb - services_gb`; not while back-pressure
  pauses admission (memory PSI over `psi_pause`, load over `load_pause` x `cores` for `hold_s`); disk >= `min_disk_gb`. The
  rule lives in `<root>/runs/cap.sh`, rewritten on every `janus run` and launch (by rename) and re-read by waiting jobs every
  15 s; each job records its peak memory, run time, CPU seconds and queue wait in `runs/history.tsv`. Waiting jobs go
  shortest expected run time (their class's median) first, minus `age_weight` x seconds waited; a job is admitted only if it
  fits beside every job ahead of it. The admitted job is capped at its cores (`MAKEFLAGS=-jN`, `CARGO_BUILD_JOBS`,
  `RUST_TEST_THREADS`, `NEXTEST_TEST_THREADS`, `OMP_NUM_THREADS` unless set; Linux scope `CPUQuota`; podman `--cpus`). `MemoryMax` per job is only the runaway guard. A jobs-lane job yields while a gates-lane job waits. Local land
  lane: the disk floor, the host's `admit_check`, and no `legacy_lanes` lane still running.
- runs the job in `systemd-run --user --scope -p MemoryMax=<job_gb>` after the host's `run_env` and `job_setup`; with
  `runner = "podman"` in the host's container settings (`--memory=<job_gb>`, nice 15 and idle IO; `--usb` adds `usb_args`
  under `usb_lock`), with a start and end line in `job_log`.
- waits with `pueue wait` (retrying through ssh drops), prints the log and exits with the job's exit code. One command
  argument is shell text (like the old `"$*"`); several are quoted with `shlex.join`.
- If the caller dies the job keeps running. Its log and exit code stay in `<root>/runs/<jid>.{log,rc}` on the host;
  **`janus collect <host>:<task>`** copies them to `.janus/runs/`, prints the log and exits with the job's code (3 while it
  is still queued or running).

**`janus gate <name> [--host h] [--unit u] [--priority N] [--wait]`** queues `out/<name>/tests.sh` as a `gates` task on the
local machine. With `paths.gate_runner` set, that project script runs it with `<name>` (and owns the RESULT line); otherwise
janus runs `tests.sh` itself and appends `<date> <time> <name> EXIT_tests=<rc> RESULT: PASS|FAIL` to `paths.gate_log`.
`--host` is passed in `paths.gate_host_env` (default `JANUS_TEST_HOST`); `JANUS_LANE=gates` puts the gate's remote steps in the
hosts' `gates` lanes. `--unit` (default `$JANUS_UNIT`) records the gate on the unit for the landing check. `--wait` prints the
RESULT line, exit 0 on PASS. **`janus cancel <host>:<task>`** kills and removes a janus job that outlived its intent.
**`janus queue [host] [--log N]`** prints each host's pueue status (or a task's log), read-only.
**`janus busy <wt>`** exits 0 (and says why) while the tree is in use: a janus job queued or running on it (any host; a host
that does not answer counts as busy), an open unit's own job when the unit's cwd is that tree, or a local process matching
`[busy] patterns` (default: mutation testing, which edits sources in place) that runs in or names it; 1 when it is free. A
periodic WIP snapshot runs it first and skips the commit while it exits 0.

Project scripts may wrap these (test shims, queue workers); list them in `paths.lane_scripts` so a test they run is not
reported as running outside the lanes.

## 6b. Backlog and landing

**`janus backlog import`** reads the project's status checklist (`[backlog] file`, default `paths.checklist`): markdown table
rows (the header names the id, item and status columns) and `- [ ]` lines. Every unfinished item becomes a beads issue
labelled `backlog` and `status:<status>` (plus `backlog-ready` when its status is in `[backlog] ready`), keyed by external
ref `backlog:<id>`; a re-import updates labels, closes items that became done and reopens ones that came back, so it is
idempotent. `janus backlog list` shows what it reads. `janus check` then reports:
- `idle-backlog:<host>`: the host has room while ready backlog items have no open unit ("write briefs for: ..."); a unit
  covers an item by `janus launch --item <id>`, an `ITEM: <id>` line, or the id named in the first lines of its brief;
- `backlog:stale` / `backlog:import`: the checklist changed since the last import, or was never imported;
- `landing:<unit>:nogate`: a unit with VERDICT DONE for over `[landing] finished_min` (30) minutes has no gate queued or
  run (gates belong to a unit by `janus gate --unit` or `[landing] gate_names` patterns);
- `landing:<unit>:unlanded`: its gates passed (`gates_required`, default 1) but its worktree HEAD is not on the landing branch
  (an ancestor of it, or every file the unit changed has the tip's content).

**`janus tidy [--dry-run]`** does the bookkeeping that used to need hand operations: it removes worktrees under
`paths.worktrees` that are clean (no change, no untracked file, no stash of their branch), contained in the landing branch
and used by no open unit (`git worktree remove`, never `--force`; the branch is kept), closes the herdr panes of closed
units, and archives board cards whose process is gone and that belong to no open unit. Each worktree it keeps is listed with
the reason (uncommitted changes, stash, changed files not on the landing branch). `--dry-run` only reads (an agent may run it).
It also covers the rest of the mess orchestration makes, each by a safe rule:
- branches matching `[tidy] branches` (e.g. `["wk/*"]`; none by default) that no worktree has checked out and that are merged
  or squash-landed (contained in the landing branch) are deleted, and their `[tidy] remote` (origin) branch too when its tip is
  the same commit;
- job run files (`runs/<jid>.*`) older than `[tidy] keep_days` (14) on every host; this project's stopped containers (name
  prefix `janus-run-` only); remote synced copies of closed units, only when nothing in them is uncollected;
- test roots (`/tmp/janus-test-fixture-*`, and older `janus-test-*` / `janus-rem-*`) over a day old;
- `[hosts.X] disk_budget_gb`: the host's janus root plus its `cache_dirs` (build targets, compiler caches); over budget, the
  oldest top-level entries of the cache dirs go until it fits.
The watcher refreshes the list every `[tidy] every_min` (60); `janus check` raises `disk:<host>` over a budget and `tidy:<n>`
with what is reclaimable.

**Gate policy (targeted gates, known failures, time limits).**
- `janus gate-scope <unit> [--name land-<unit>] [--dry-run]` runs the project's affected-scope hook (`[landing] scope_cmd`,
  called with `{unit} {tree}`, env `JANUS_UNIT`/`JANUS_TREE`), which prints the tree's gate steps, one per line:
  `[@host ]<step>: <command>`. Steps without a host (portable ones) run on this machine in the tree; `@host` steps run there
  through `janus run`. janus writes `out/<name>/tests.sh` (each step prints `EXIT_<step>=<rc>`); queue it with `janus gate`.
- Known failures: `[landing] known_failures` names a tab-separated file: the test id columns, the cause, the unit fixing it at
  the root (the id is the columns before the last two joined by `/`; `#` lines are comments). A gate's `tests.sh` names its
  failing tests in `$JANUS_GATE_OUT/failed.txt` and the tests that ran and passed in `passed.txt`. The gate's RESULT is PASS when
  every failing test is in the file with a fixing unit (`janus gate-judge <name> <rc>` prints the judged line; a project
  runner's FAIL is re-judged the same way). A non-zero gate that names no failing test fails. `janus check` raises
  `known-failure:<id>` for an entry whose unit is missing or not open (failures on main are fixed at their root cause) and
  `known-failure:<id>:passes` for an entry a gate ran and passed in the last 24 h (remove it).
- `[landing] gate_timeout_min` (or `janus gate --timeout-min`): the gate's process group is stopped at the limit and a
  `EXIT_timeout=124 RESULT: FAIL` line is written. Every gate runs under `janus gate-exec`, which records its length.
- `[landing] quick_gate_min`: a gate expected (median of its own last runs, else of its family `land-*-<suffix>`) under this
  many minutes, or queued with `--quick`, goes to the one-slot `gates-quick` group, so short gates never wait behind long ones.

**Test fixtures never trip live findings.** `tests/test_janus.py` puts every fixture (temp projects, scripts, daemons) under one
temporary root named `janus-test-fixture-*` and sets `JANUS_IN_TESTS` for itself. A live `janus check` (or the watcher) skips any
process whose command line holds that mark (local test/build runs, CPU burners, changed scripts, scheduler and remote
direct-build probes), so running `./check` beside a live project raises nothing there; the suite still sees its own fixtures.

## 6c. Self-safety

Every script janus writes or replaces (job scripts, lane runners, remote agent scripts, the pueue shim, cap.sh, rewritten
reports) is written to a temp file and renamed into place, never edited in place: a running shell keeps reading its old file.
`janus check` reports `script:<pid>` when a running shell script's file was changed in place after it started (the shell reads
its script as it goes, so it would continue at its old byte offset of the new text, which is how a launcher once looped and
wrote 49,000 ledger rows). The live toolkit is a read-only release copy (section 1), so an agent editing the janus repository
never changes the toolkit that runs it. Every hook denial is logged in `.janus/denied.log`.

## 6d. Park and resume: agents never pay to wait

An agent submits its builds and tests detached (`janus run --detach`), which prints each job id (`<host>:<task>`) at once,
writes `WAITING <job ids>` in its report and ends its turn. The unit is then parked on those jobs. When every job has
finished, the watcher resumes the same session with each job's exit code and log path: an API unit gets a new run of the
same session (`claude -p --resume <session id>`, or the provider's `resume_cmd`) with its own ledger rows and the rest of the
unit's budget; an interactive agent gets a prompt. A run that ends at its budget cap without a final verdict is resumed once
with `[budget] wrapup_usd` (default 0.5) to write its report.

- `janus park <unit>` parks a live agent: its turn is stopped, its jobs keep running. Refused when it has no job of its own.
- `janus resume <unit> [--message <text>] [--force]` resumes by hand (`--force` before its jobs finish).
- `check` raises `parked:<unit>:resume` when a resume failed.

## 6e. Quality: the rules, the brief lint, the review, and the soak

`[quality]` holds the project's quality settings, and nothing there runs unless it is set.
- **Every brief header** points at the project's rules file (`[quality] rules_file`, default `QUALITY.md`, read where present)
  and states two rules: every new test names the failure it catches, shown failing first; run only the tests your change can
  affect. Agents are also asked to write `report.json` beside `REPORT.md` (verdict, `files_changed`, jobs with their exit codes,
  `tests_added` with the failure each catches, `left`); janus prefers it over the prose and falls back to `REPORT.md` when it is
  absent or not JSON.
- **Brief lint** (`[quality] brief_lint = true`): `janus launch` refuses a brief that fails and names every reason: no tier line,
  no scope, no report path, no done-criteria (`DONE-WHEN:`/`PROOF:`), or an absolute path under the project root that is missing.
  Off unless set, so a project turns it on once its briefs carry the markers; `janus quality lint <brief|unit>` runs it alone.
- **Pre-landing review** (`[quality] review_model`, an api model): `janus quality review <unit>` has that cheap model read the
  unit's diff for slop (filler, needless code, comments that restate the code, speculative abstractions) and test value (tests
  with no real failure, duplicates, assertions that cannot fail), and writes `out/<unit>/review.json`. `janus land` refuses the
  unit until the review passes or `janus quality review <unit> --override '<reason>'` accepts its findings.
- `janus check` raises `test-signal:<gate>` for a gate that takes over `[quality] signal_min_min` (20) minutes and has not failed
  in its last `signal_runs` (5) recorded runs: scope or trim it (deleting a step is the owner's call).
- **`janus soak [--minutes N] [--live]`** exercises janus's read-only paths on a live project and prints one pass/fail line per
  step: the watcher's wake decision, a check, peers, the reports index, a brief header (with the session memory), a transcript,
  each ledger, and the decision log. Read-only by default; `--live` also runs one watcher pass. `--minutes 0` is one pass.

## 7. Troubleshooting

| You see | Meaning |
|---|---|
| `janus: no janus.toml here or in a parent` | run it inside the project |
| `refused, no host has room (laptop: disk 12G; ...)` | every candidate host is full or unreachable; the reason is listed per host. Check `janus capacity` |
| `no model available for normal/implement (codex: provider codex disabled; ...)` | routing found no enabled, in-budget model (or one allowed on `--host`, or of another vendor for a review) |
| `<name> already launched on <host>` | the unit exists; `janus stop`/`close` it first (a review may reuse the name) |
| `brief ... not found` | create `<briefs>/<unit>.md` |
| `has uncommitted changes; commit and push, or --force` / `unpushed commits` | `janus close` protects unlanded work |
| `<unit> is still alive after stop` | the process outlived 10 s of waiting; run `janus check` and look again |
| Hook denial "Orchestrators do not operate by hand" | brief an agent or use `janus`; see section 4 |
| Stop blocked "Do not go idle" | run `janus check`, act, `janus ack`, update the notes |
| `beads unavailable: ...` | `bd` is missing or `[project] beads` has no `.beads/`; run `bd init` there |
| `watcher com.janus.watch is not running: run install` | the launchd agent is not loaded |
| `git has no upstream: add a remote, then janus commit-state` | the project repo's branch has no upstream; `janus commit-state` pushes with `--set-upstream` |
| `refused: the <p> ledger ... is malformed` / `[ledger:<p>]` | `janus ledger check` lists the problems; `janus ledger repair` deduplicates by run id (backup first) |
| `no API key: ...` | the provider's `key_keychain` / `key_file` gave no key; `janus doctor` shows which |
| `[script:<pid>] running script ... was changed after it started` | stop that process and relaunch it; write scripts by rename |
| `refused: <wt> has conflict markers` / `is N commits behind main` | finish the merge or merge the landing branch first; `--allow-unmerged` overrides |
| `janus: waiting for admission (<class>; <what blocks>): reserved N job(s): C of K cores, M MB + agents A MB + this job J MB of T MB ...` (in a job log) | the host has not got that much room yet; it starts by itself when it has |
| `[overrun:<host>:<job>] ... has run N min, Kx its expected M min` | the job runs far longer than its class's median there: read its log; `janus cancel` it if stuck |
| `refused: a whole-suite run (...) needs --broad <reason>` | narrow the command (one package, a filter), or pass `--broad <why it must be whole>`: it then runs at low priority |
| `[old-rule:<host>] N job(s) still wait under the old cap rule` | jobs queued before the measured rule; re-queue them with `janus run`, then stop the old tasks |
| `(read-only: not the registered orchestrator session ...)` after `janus check` | expected for anyone but the orchestrator: nothing was saved, tuned or stamped |
| `refused: <dir> is the cwd of open unit(s) ...` / `is a main checkout` | give the unit its own worktree |
| `pane ... not found, but the registered session is still running` (watch.log) | the watcher will not start a second orchestrator while the registered one's process lives |
| `janus collect`: `<host>:<id> is Running, not finished` (exit 3) | collect again later; the job is still on the host |
| `orchestrator gone; no provider enabled` (in `.janus/watch.log`) | the watcher will not start a new orchestrator unless a provider is enabled |

Logs: `.janus/watch.log` (watcher), `~/.config/janus/watch.err` (launchd stderr). Hooks that fail print to stderr and let
the action through.

## Known gaps

The Codex `/hooks` trust step is manual.

Open items and their reasons are tracked in `docs/ISSUES.md`. Not yet verified live:
- A remote API agent end to end with the new lane runner and patch collect (dry-run only).
- `watch` starting a new orchestrator (the conditions are tested in code review only), and the context reset.
- A full herdr-backed agent via `janus launch`, including the startup-dialog answers.
- A board question or block waking the orchestrator at once, and the session memory in agents' briefs (tested, marked
  "verify live" in `docs/ISSUES.md`).
- Codex's PreToolUse tool names (the handler accepts `Bash`, `shell`, `local_shell` and any tool input with `command`/`cmd`).

Asked for but not in the code: moving unstarted jobs to another host after `unreachable_min`
(the setting is read but unused), pushing state to a bare repo, `MemoryMax` for herdr agents on a macOS coordinator (no
systemd), host roles (target/build/agents/dev) with parallel gates per host, and the remote agent's git (a remote copy has
no `.git`; its work comes back as a patch).
