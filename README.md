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
- a watcher wakes the orchestrator, and starts a new one only when the registered one is really gone;
- `orch check` reads the real state (hosts, jobs, panes, gates, git) and prints one line per problem;
- `orch launch` admits an agent only while the machine's reservations leave room for it, and refuses (saying when room is expected) when full;
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

## Share with a friend: quick start

Needs macOS or Linux, Python 3.11+, git, ssh, rsync, [beads](https://github.com/steveyegge/beads) (`bd`), pueue, and the agent
CLIs you route to (Claude Code `claude`, Codex `codex`, opencode); herdr and tmux for agents in panes.

```
git clone <this repo> ~/code/orchestrator && cd ~/code/orchestrator
./check                            # the tests; install runs them again on the copy it installs
cd ~/code/myproject                # your project (a git repo)
python3 ~/code/orchestrator/orch.py init     # writes a commented orch.toml, .orch/, briefs/, out/
$EDITOR orch.toml                  # this machine's [hosts.*] entry, models, routing, the API key's secret store
bd init                            # beads for units, instructions and backlog (once)
security add-generic-password -s orch-api-key -a "$USER" -w     # macOS: the API key into the keychain (prompted)
#   Linux: printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" > ~/.config/orch/anthropic.env; chmod 600 ~/.config/orch/anthropic.env
~/code/orchestrator/install --project .      # release, links, hooks, watcher, tools (--dry-run first to see every change)
orch doctor                        # tools, release, hooks, watcher, config, secret store, ledger, beads, hosts: fix every FAIL
```

Then open Claude Code (or Codex) in the project, run `orch register`, and work through the cycle in section 4. Nothing in
`orch.py` is specific to one project: every path, host, container, model and key name is in `orch.toml`
(`example.orch.toml` documents every key).

## 2. Install and uninstall

```
./install --dry-run        # prints every change, changes nothing
./install                  # run from a directory with an orch.toml (or pass --project DIR)
```

Options: `--dry-run`, `--project DIR`, `--no-hosts` (skip the remote machines), `--hosts-only` (only set up pueue on the
hosts in orch.toml), `--release-only` (only build, test and switch to a new release: the way to make an edit live),
`--rollback` (switch back to the previous release), `--skip-tests`. On Linux, install writes a systemd user timer
`orch-watch.timer` (every 5 min) instead of the launchd agent.

**Releases: nothing live ever points at the working tree.** `install` copies `orch.py`, `skill/`, `example.orch.toml` and
`tests/` into `~/.local/share/orch/releases/<date>-<content hash>/` (files made read-only), runs the checks on that copy
(parse, `--help`, hook calls, `tests/test_orch.py`), and only when they pass switches `~/.local/share/orch/current` to it with
one atomic rename. `~/.local/bin/orch` points at `current/orch.py` and the skill links at `current/skill`, so the toolkit and the
skill change together, at one instant, to a tested copy. A queued `orch api-run` task names the release it was queued from, so
a new release never changes a running task. The five newest releases are kept.

**Making a change live (developers):** edit in the repo (or a worktree), run `./check`, commit, then
`./install --release-only`. A half-edited `orch.py` is never live, and `./install --rollback` undoes a bad release.

What `install` changes:

| Where | Change |
|---|---|
| `~/.local/share/orch/releases/<v>/`, `~/.local/share/orch/current` | the tested release copy, and the link to it (switched by one rename) |
| `~/.claude/skills/orchestrating`, `~/.codex/skills/orchestrating` | symlinks to `~/.local/share/orch/current/skill` (older links into an orch working tree are replaced) |
| `~/.local/bin/orch` | symlink to `~/.local/share/orch/current/orch.py` (make sure `~/.local/bin` is on PATH). Project shims call `~/.local/bin/orch` |
| `~/.claude/settings.json`, `~/.codex/hooks.json` | adds `~/.local/bin/orch hook <event>` for SessionStart, UserPromptSubmit, PreToolUse (matcher `Bash\|Edit\|Write\|MultiEdit\|NotebookEdit\|apply_patch`) and Stop. Existing keys and hooks are kept; a backup `*.bak-orch-<time>` is written first |
| `~/Library/LaunchAgents/com.orch.watch.plist` | copied (not linked) and loaded with `launchctl`; runs every 300 s |
| `~/.config/orch/projects` | the project directory is added; the watcher runs `orch watch` in each listed project |
| Homebrew | `brew install beads pueue`, `brew services start pueue` |
| each ssh host in orch.toml | downloads pueue's static binaries into the host's `bin`, writes and enables a user systemd service `pueued`; no sudo |

An existing path that is not our link is skipped, never overwritten. A failing step prints "failed (continuing)".
Two details in the host step: `arch` must be `aarch64` or `x86_64`, and a host with `pueue_dir` (a shared server) keeps
pueue's files and its user unit under that directory instead of the home directory.

**Codex needs one manual step:** open Codex and run `/hooks` to trust the new hooks. Until then they do not run.

**Uninstall.** There is no uninstall script. By hand:
1. `launchctl bootout gui/$(id -u)/com.orch.watch`, then delete the plist and the project line in `~/.config/orch/projects`;
2. remove the entries whose command ends in `orch hook <event>` from `~/.claude/settings.json` and `~/.codex/hooks.json`
   (or restore the `.bak-orch-*` backup);
3. delete the three symlinks above and `~/.local/share/orch`;
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
(`<out>/<unit>/REPORT.md`), `worktrees` (`<worktrees>/<unit>` is the unit's working directory if it exists; it may be outside the project, e.g.
`../../worktrees`), `checklist` (the project's status checklist, editable by the orchestrator), `state_docs` (old state docs
for `orch init`). **`[placement]`**: optional `"<regex>" = "<host>"` rules matched against a unit's name and brief: the host
where its tests run, where launch places it.

**`[herdr]`** `workspace` and **`[orchestrator]`** `kind`/`args`: the herdr workspace label, and what the watcher starts
when the registered orchestrator is gone. It must match the registered session's kind (`check` reports a mismatch; the
watcher starts the registered kind, with `args` only when the kinds match). The watcher starts one only when all of these
hold: `herdr pane list` answered and the registered pane is not in it, the registered session's process (pid and start time,
recorded at register) is not running, and its transcript has not changed for 20 min. A herdr error or timeout starts nothing.
It then marks the registration pending, so the new session's first hook event claims it. Every prompt the watcher sends
starts with `[orch] `.

**`[hosts.<name>]`**: one per machine. `local = true` for this one; others have `ssh`, `arch`, `root` (where synced
worktrees, briefs and outputs live) and `bin`. Capacity figures: `ram_gb`, `cores`, `reserve_gb` (kept free),
`agent_gb` (per agent), `job_gb` (per pueue job), `min_disk_gb`. Optional `memory_max` (systemd-run limit for remote
API agents) and `pueue_config`. For `orch run` (section 7a): `services_gb` (measured memory the host's own services use;
`ram_gb - services_gb` // `job_gb` sets the initial `jobs`/`gates` parallel limits), `gate_slots` (parallel `gates` tasks),
`legacy_probe` (shell printing `legacy_wait=<n>` and `legacy_run=<n>` for an older job queue, shown by `orch check` while it
drains) with `legacy_slots`, `runner = "podman"` with a `[hosts.<h>.container]` table (image, mount, mount_at, args,
usb_args, usb_lock, host_dirs, clone_base, name_prefixes, cpus), `worktrees`, `job_log`, `run_env` (shell run before every
job), `job_setup` (shell run in the worktree before the command), `sync_exclude`, `cores_per_job` (CPU admission), and on the
local machine `admit_check`/`admit_check_env` and `legacy_lanes` (dirs with a pid file of an older lane the land lane waits
for). Optional `[lanes.<lane>] age_min` overrides a lane's age limit. `example.orch.toml` shows every key.

**`[caps]`** (optional, only when the owner wants them): `total = 6`, and `[caps.host] server = 2`. With no caps the limit
is what each machine can safely hold.

**`[providers.<name>]`**: `enabled`, and optionally `cap_usd`, `ledger`, `budget_per_unit`, the secret store of the API key
(`key_keychain`: a macOS keychain item; `key_file`: a `VAR=value` file that only you can read, mode 600; `key_env`: the
variable, default `ANTHROPIC_API_KEY`) and `cmd` (the agent command; default `claude --bare -p --model {api_id}
--max-budget-usd {budget} --output-format json --dangerously-skip-permissions {prompt}`). A provider is usable while its
ledger is valid and `cap_usd` minus committed spend is at least `budget_per_unit`.

**API runs and the ledger belong to orch.** `orch launch` of an API model reserves the budget (under the ledger's lock:
refused when the ledger is malformed or the cap would be passed; else the run's one `running` row) and queues
`orch api-run <unit> --run <id> ...` in pueue's agents group with an allow-listed environment (no key). `orch api-run` reads the
key from the secret store at that moment, gives it only to the agent process (never pueue, a file or argv), forwards
TERM/INT/HUP to it, and writes the run's one `done` row with the reported cost, or the whole budget when none was reported.
Ledger rows: `ts  unit  running|<cost>  <budget>|done rc=<x>  <run id>`. Every write takes `<ledger>.lock` (an fcntl lock)
and is idempotent: a second running or done row for a run id is never written, so retries and re-runs never duplicate. Spend
is computed per run id (its done cost, or its budget while it runs). `orch ledger check` reports every problem (exit 1 when
malformed); `orch ledger repair` keeps the first running and first done row of each run id, drops unparseable rows, writes a
backup `<ledger>.bak-<time>` first and replaces the file by rename. `orch stop` and `orch close` close a run that ended without
a done row. Remote API agents get the key over ssh into a fifo only their task reads; `orch collect` records their cost.
Project launchers (an older `launcher` key) are no longer used; doctor says so.

**`[models.<name>]`**: `provider`, `vendor`, `kind`. `kind = "api"` is an API agent run by orch (`api_id` is the model id
the API takes).
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

**`orch check`**: silent when all is well; otherwise one `[key] problem` line each. Run by the registered orchestrator
session (its session id, or its process among the caller's ancestors), it rewrites the Live state part of `.orch/HANDOFF.md`,
saves acks and open problems, sets the pueue limits and stamps the cycle (`last_cycle`). Run by anyone else (an agent, an
auditor, a shell), `check`, `status` and `capacity` only read: no file is written, no pueue limit changes, no cycle stamp, and
`check` ends with a "(read-only: ...)" line. The watcher is the other writer.
```
$ orch check
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
idle room while beads has ready work (units, `orch-log` and instructions are not work: `orch launch` marks every unit
`in_progress`); orphan pueue jobs (including lane jobs labelled `unit:<name>` whose unit is closed), tmux sessions (`orch-*`)
and herdr agents that belong to no open unit; blocked or finished units; every board card in state question or blocked, or
SILENT (over `silent_min`) or GONE, including cards orch did not launch (marked "not a unit"); jobs still waiting under the
old cap rule (`old-rule:<host>`); on the Mac, CPU burners an agent started (over 80 % CPU for 10 min, under an agent or
orphaned) and test or build runs outside the pueue lanes (`localtest:<pid>`); an orchestrator kind in orch.toml that differs
from the registered session's;
disabled providers in use; caps exceeded; spend at a provider's cap; failed gates (last 12 hours) and the same test failing
in two or more gates; uncommitted or unpushed git work; unclassified owner instructions; a stale handoff; the watcher not
running (macOS).

**`orch capacity`**: room per host, measured live (read-only).
```
$ orch capacity
laptop room=2 avail=9.4G load=2.10 psi=- disk=80G agents=1
server room=0 avail=3.1G load=1.20 psi=0.00 disk=60G agents=2 FULL: memory 3.1G free, reserve 3G
```

**`orch launch <unit> --kind implement|review|audit|cleanup [--tier critical|normal] [--host h] [--test-host h] [--model m] [--brief f] [--cwd d] [--allow-main] [--budget usd] [--of unit] [--dry-run]`**
```
$ orch launch parser --kind implement --tier normal
launch parser: model=codex host=laptop cwd=/proj/worktrees/parser
launched parser on laptop via herdr
```
A `cleanup` unit frees disk and load, so neither `min_disk_gb` nor the load limit refuses it; memory and caps still apply.
- **Model:** from `[routing]`, or `--model`, which passes the same checks (provider enabled, budget left). Every API launch
  checks `committed + budget <= cap_usd` and reserves the run's "running" row under the ledger's lock before anything is
  queued. A resumed unit continues its original report: `--resume-of <unit>` (its `out/<unit>/REPORT.md`) or `--report
  <path>`, never a path read from the brief's prose; check, close, collect and the Stop hook read that report, and a verdict
  written before the resume started does not count. Every value orch puts in a shell command is quoted, and launch waits a few
  seconds to confirm the run started (or is queued for a slot): a run that dies at once fails the launch with its exit code
  and log, and nothing is reported as launched. `--item <id>` (or an `ITEM: <id>` line) ties the unit to a backlog item.
- **Host:** `--host`; else where the unit's builds and tests run (`--test-host`, a `TEST-HOST: <h>` line in the brief, the
  first matching `[placement]` regex in orch.toml, or the sticky host in `out/linux-host/<unit>`) when the model may run
  there; else the allowed host with the most room.
- **cwd:** `<worktrees>/<unit>` if it exists, else `--cwd`, else the project root. It refuses a cwd another open unit uses
  (a local review or audit may share it) and a main checkout that has linked worktrees (unless `--allow-main`).
- **Order:** the beads issue is created and marked `in_progress` first; a failed start closes it with "launch failed".
- **Environment:** every agent gets `ORCH_ROLE=agent`, `ORCH_UNIT`, `ORCH_REPORT` and `ORCH_ROOT`. `pueue add` always runs with
  an allow-listed environment (PATH, HOME, USER, LANG, TMPDIR, SSH_AUTH_SOCK ...) plus those, never the caller's: pueue stores a
  task's whole environment in its state file, so a caller's `ANTHROPIC_API_KEY` or session token would land on disk.
- **Remote:** the worktree goes to `<root>/worktrees/<unit>` (and a copy to `<root>/base/<unit>` for collect); the brief gets a
  header naming the host-local test command `<root>/run/<unit>.lane '<cmd>'`, which runs the command through that host's
  pueue `jobs` lane with orch's admission and `MemoryMax`, waits and exits with its code. The key is sent from a detached
  ssh with no time limit, so a full `agents` group queues the task instead of failing the launch. Remote tmux agents run under
  `systemd-run MemoryMax` too.
- **herdr:** each unit gets a tab `<unit> · <model> · <host>`; for a pueue unit it follows `pueue follow <task>` (over ssh for a
  remote one). For a herdr agent, launch answers startup dialogs (Claude folder trust, Codex update and trust prompts) and
  reports any it cannot pass.
- **Board:** the first card's check is `herdr <unit>`, `pueue <host>:<task>` or `tmux <host>:orch-<unit>`.
It refuses with `orch: refused, no host has room (...)` when nothing fits. The unit name must match `[a-z][a-z0-9_-]{0,31}`
and the brief `<briefs>/<unit>.md` must exist. For a review, `--of <unit>` makes it avoid the implementer's vendor.

**`orch stop <unit>`**: ends the unit's whole process tree: `pueue kill` or `tmux kill-session`, its lane jobs (label
`unit:<name>`), then TERM (and after 5 s KILL) to every process group of every process under its task or pane and of every
process whose environment holds `ORCH_UNIT=<unit>` (detached children: `nohup`, `setsid`, `& disown`). It confirms none is
left, else exits 1.
```
$ orch stop parser
stop parser (pueue on laptop)
stopped: no process of the unit is left
```

**`orch collect <unit>`**: for a remote unit, makes `out/<unit>/changes.patch` on the host (the agent's tree against the copy
it was sent), brings back `out/<unit>/` (patch, REPORT, logs) and records API cost in the ledger. It never writes over the
Mac checkout; apply the patch in the unit's worktree with `git apply --3way`. Local units: nothing to collect.

**`orch close <unit> [--force] [--reason text]`**: for a unit with a worktree of its own, refuses on uncommitted or unpushed
work (unless `--force`). A unit without one (cwd is the project root, or shared with another open unit) skips the git checks
and needs a final VERDICT line in its REPORT instead; `--force` is refused there. Then it closes the unit's tab, removes the
remote copy unless another open unit on that host uses it, removes only its own pueue task, and closes the board card and
the beads issue.

**`orch board-close <agent>`**: archives a board card that is not an open unit (cards from other launchers); `board.sh close`
by hand is denied.

**`orch init`**: in a folder without an orch.toml, writes a commented starter one (from `example.orch.toml`, this machine
named after its host name) and creates `.orch/`, `briefs/` and `out/`. In a project that has one, turns the old state docs in
`paths.state_docs` into a one-line pointer to `.orch/HANDOFF.md`, keeping their text in `.orch/history-<name>`.

**`orch doctor [--no-hosts]`**: checks a setup end to end and names the fix for each problem: tools (git, ssh, rsync, bd,
pueue and pueued, herdr, tmux, each model's CLI), the installed release and skill links, the hooks, the watcher, the config
(hosts, models, routing, paths), beads, each API provider's secret store (the key is read, never shown) and ledger, and every
host over ssh with its pueue. Exit 1 when anything fails.

**`orch ledger check|repair`**, **`orch backlog import|list`**, **`orch queue`**, **`orch cancel`**: sections 3, 7a and 7b.

**`orch commit-state [-m msg] [--dry-run]`**: runs `git add -A`, then scans exactly the staged files for secrets: Anthropic,
OpenRouter, OpenAI (`sk-proj-`), hex `sk-` keys, AWS access key ids, GitHub and Slack tokens with a realistic length, and
private-key blocks. A bare prefix mention passes. On a hit it unstages again, refuses and lists the paths, never the values.
Otherwise it commits and pushes.

**`orch ack <key> "<decision>"`**: acknowledges a problem for 4 hours (margin problems 30 min), until the problem clears
(a cleared problem's ack is dropped, so it comes back if it recurs), and records the decision as a beads comment (on the
unit's issue for `unit:<name>:...` keys, otherwise on an `orch: decisions` issue).
```
$ orch ack unit:parser:finished "collected and closed, gate re-queued"
acked unit:parser:finished
```

**`orch status`**: prints the Live state (hosts, units, ready work, spend, problems) without counting as a cycle.

**Notes.** After each decision, update the *Orchestrator notes* part of `.orch/HANDOFF.md` (see section 6).

## 5. What the hooks enforce, and why

One handler, `orch hook <event>`, serves both Claude Code and Codex (same event names and JSON). It only acts for the
registered orchestrator session, and finds the project from that session id (the project in `~/.config/orch/projects` whose
`.orch/session` holds it), never from the session's current directory, so `cd` elsewhere changes nothing. Only `orch register`
changes the registration (it records the session's process and kind, claude or codex); a pending registration (`orch
register` with no session id, or the watcher's new orchestrator) is claimed by the first hook event from that project's
directory within 10 min. No environment variable takes a registration over. Agents started by `orch launch` run with
`ORCH_ROLE=agent` and get only the agent rule.

| Event | Orchestrator | Why |
|---|---|---|
| SessionStart | injects HANDOFF.md (the Live state of the last check) and `bd prime` (standing rules), and tells the session to run `orch check` first; it does not probe hosts (the hook has 60 s) | a fresh or cleared session resumes without being re-told |
| UserPromptSubmit | files the prompt as a beads issue labelled `instruction` (except prompts orch sends, which start with `[orch] `, the scheduled `orch cycle:` prompt, slash commands such as `/clear`, and background-task notifications containing `<task-notification>` or `[SYSTEM NOTIFICATION`) | nothing the owner says is lost; `check` lists it until closed or turned into a rule |
| PreToolUse | parses each shell command and judges every part of it: heredoc bodies are data unless fed to a shell or `ssh`; `$( )`, backticks and `<( )` are judged as commands; the rest is split on `;` `&&` `\|\|` `\|` `&` and newlines; each part is judged by its command word after wrappers (`sudo`, `env`, `timeout`, `nice`, `nohup`, `xargs`, `command`, `exec`, ...) and any path are stripped, and `bash/sh -c`, `eval` and `ssh <host> <cmd>` are judged recursively. Denied: `rm -r` (any spelling), `kill`/`pkill`/`killall`, `sed -i`/`perl -i`, `git reset --hard`, `git clean`, `git push --force`/`+ref`/`--delete`, `git checkout -- .`, `git restore`, branch/stash/worktree deletion, `sparse-checkout`, `find -delete`/`-exec rm`, `rsync --delete`, `truncate`/`dd`/`mkfs`/`reboot`, `tmux kill-*`, `podman/docker rm/kill/stop`, `launchctl bootout`, `systemctl stop/restart`, interpreter one-liners that delete or kill (`shutil.rmtree`, `os.kill`, ...), `pueue` other than `status/log/follow/wait/group`, `bd` other than its read and issue-tracking commands (`bd delete`, `purge`, `compact`, `sql` ... are denied), `herdr pane/tab/workspace close`, `herdr pane run/send-text/send-keys`, `board.sh close` (use `orch board-close`), `orch close --force`, loops over worktrees that change files, and shell redirections that write into the project or the worktrees outside the allowed paths. Nothing passes because it starts with `orch`, `bd`, `pueue`, `herdr` or `board.sh`. File edits (Edit/Write/MultiEdit/NotebookEdit, and `apply_patch` by the paths in the patch) are allowed only under the state dir, the briefs dir, `orch.toml`, `paths.checklist` and `paths.state_docs` | the orchestrator orchestrates; changes go through `orch` or an agent |
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

**The message board.** `paths.board` is your script. orch calls `summary` with `BOARD_SILENT_MIN` (each row: name, machine,
state; `SILENT` or `GONE` flags; `check` reports question and blocked states and both flags for every card), `rule <unit> <machine>` (text added to every agent prompt), `post` (on launch) and `close` (on close).
Agents post their progress to it; the orchestrator reads `orch` output rather than the board or panes directly.

## 7. Multiple machines, capacity and network outages

Every host in `orch.toml` can run agents. `launch` picks the host with the most room. Room is by reservations (see
"Capacity" below): the memory for reservations (`ram_gb - services_gb`) minus what running jobs and agents reserve, over the
agent figure (learned, else `agent_gb`); none while admission is paused by back-pressure (memory PSI over `psi_pause`, or
load over `load_pause` x `cores` for `hold_s`), or while disk is under `min_disk_gb` (disk does not hold back a `cleanup`
unit). Every agent holds a reservation file (`runs/agent-<unit>.res`), API and interactive alike. Caps reduce it further. `check` reports "host over its margins" for low disk or high load even when no units are ready. On macOS the probe takes the larger of two readings, `kern.memorystatus_level` (memory_pressure's free percentage, read with sysctl) x `hw.memsize`, and `vm_stat` free + inactive + speculative + purgeable pages, so one reading of 0 under load no longer makes the Mac look full; with no reading at all the memory limit is skipped for that probe and load from `sysctl -n vm.loadavg`. An unreachable host has room 0.

Work belongs to the machine's own queue, not to an ssh session:
- **pueue** runs on each machine (launchd/brew on the Mac, a user systemd service on Linux hosts). API agents run as
  tasks in the `agents` group and keep running if ssh drops. The watcher and the orchestrator's own `orch check` also set the `jobs` group's parallel limit
  to `cores // cores_per_job` (admission by reservations decides what starts), and the `agents` group's to the running
  agents plus the agents that fit by reservations, capped by `[caps]`.
- **tmux** holds interactive remote agents (session `orch-<unit>`); the herdr tab only attaches to it with
  `ssh -t host tmux attach`, so a dropped view does not stop the agent.
- **systemd-run** with `MemoryMax` (from `memory_max`) wraps remote API agent tasks, so the kernel stops one that outgrows
  its share.
- A unit is stopped only by `orch stop <unit>`, never because a connection dropped. Probes use ssh `BatchMode` with
  5 s connect timeouts.

During an outage `check` shows `host X unreachable since <time>` and the units there; their state is "unknown", not
failed. When the host returns, `check` reports finished units and `orch collect <unit>` pulls the results back.

Remote API agents get the key from the provider's secret store (`key_keychain` / `key_file`) over ssh stdin into a
private fifo, which the task removes after reading; it is never written to a file or command line. No `pueue add` gets the
caller's environment (see launch), so the key never reaches pueue's state file, even when an agent that holds it in its
environment queues a job. Remote API agents need `claude` on the host.

## 7a. Build and test jobs: `orch run`, `orch gate`, `orch collect <host>:<task>`

One way to run a build or test job on any machine: that machine's pueue, never an ssh connection.

**`orch run --host <h> --lane gates|jobs|land [--unit <u>] --wt <worktree> [--subdir <d>] [--usb] [--priority N] [--detach] -- <cmd>`**
- refuses a worktree with a merge in progress, unmerged files or conflict markers; in the gates and land lanes (and `orch
  gate`) also one more than `[landing] max_behind` (default 10) commits behind the landing branch, never an agent's own jobs
  lane work (`--allow-unmerged` overrides).
- `--detach` (alias `--no-wait`) queues it, prints `<host>:<task>` and returns: agents then write `WAITING <ids>` in their
  report and end the turn; the watcher resumes them with the results (section "Park and resume").
- syncs the worktree to a remote host: `rsync -rlpgoD --checksum --delete` with no `-t` (a changed file gets the transfer
  time as its mtime, so cargo never reuses an artifact built from older content), without the host's `sync_exclude`; a
  podman host first reflink-clones `container.clone_base`. Every `paths.sync_require` path must arrive.
- writes the job as `<root>/runs/<jid>.sh` and queues it in the host's pueue group: `gates` (priority 10) or `jobs`; `land`
  is the local machine's landing lane (parallel 1). Groups are created on first use (`jobs` and `gates` with one slot per job cap).
- admission on every host and lane is by reservations: the job waits in its pueue task, one job at a time under
  `runs/admit.lock` (a pid-owned lock dir where there is no `flock`), until its reservation fits: running jobs' cores +
  `cores_per_job` <= `cores`, and their memory + the agents' + this job's class figure (the max of its class's last
  `history_n` recorded peaks + `margin_pct`, `job_gb` until learned) <= `ram_gb - services_gb`; not while back-pressure
  pauses admission (memory PSI over `psi_pause`, load over `load_pause` x `cores` for `hold_s`); disk >= `min_disk_gb`. The
  rule lives in `<root>/runs/cap.sh`, rewritten on every `orch run` and launch (by rename) and re-read by waiting jobs every
  15 s; each job records its peak memory in `runs/history.tsv`. `MemoryMax` per job is only the runaway guard. A jobs-lane job yields while a gates-lane job waits. Local land
  lane: the disk floor, the host's `admit_check`, and no `legacy_lanes` lane still running.
- runs the job in `systemd-run --user --scope -p MemoryMax=<job_gb>` after the host's `run_env` and `job_setup`; with
  `runner = "podman"` in the host's container settings (`--memory=<job_gb>`, nice 15 and idle IO; `--usb` adds `usb_args`
  under `usb_lock`), with a start and end line in `job_log`.
- waits with `pueue wait` (retrying through ssh drops), prints the log and exits with the job's exit code. One command
  argument is shell text (like the old `"$*"`); several are quoted with `shlex.join`.
- If the caller dies the job keeps running. Its log and exit code stay in `<root>/runs/<jid>.{log,rc}` on the host;
  **`orch collect <host>:<task>`** copies them to `.orch/runs/`, prints the log and exits with the job's code (3 while it
  is still queued or running).

**`orch gate <name> [--host h] [--unit u] [--priority N] [--wait]`** queues `out/<name>/tests.sh` as a `gates` task on the
local machine. With `paths.gate_runner` set, that project script runs it with `<name>` (and owns the RESULT line); otherwise
orch runs `tests.sh` itself and appends `<date> <time> <name> EXIT_tests=<rc> RESULT: PASS|FAIL` to `paths.gate_log`.
`--host` is passed in `paths.gate_host_env` (default `ORCH_TEST_HOST`); `ORCH_LANE=gates` puts the gate's remote steps in the
hosts' `gates` lanes. `--unit` (default `$ORCH_UNIT`) records the gate on the unit for the landing check. `--wait` prints the
RESULT line, exit 0 on PASS. **`orch cancel <host>:<task>`** kills and removes an orch job that outlived its intent.
**`orch queue [host] [--log N]`** prints each host's pueue status (or a task's log), read-only.

Project scripts may wrap these (test shims, queue workers); list them in `paths.lane_scripts` so a test they run is not
reported as running outside the lanes.

## 7b. Backlog and landing

**`orch backlog import`** reads the project's status checklist (`[backlog] file`, default `paths.checklist`): markdown table
rows (the header names the id, item and status columns) and `- [ ]` lines. Every unfinished item becomes a beads issue
labelled `backlog` and `status:<status>` (plus `backlog-ready` when its status is in `[backlog] ready`), keyed by external
ref `backlog:<id>`; a re-import updates labels, closes items that became done and reopens ones that came back, so it is
idempotent. `orch backlog list` shows what it reads. `orch check` then reports:
- `idle-backlog:<host>`: the host has room while ready backlog items have no open unit ("write briefs for: ..."); a unit
  covers an item by `orch launch --item <id>`, an `ITEM: <id>` line, or the id named in the first lines of its brief;
- `backlog:stale` / `backlog:import`: the checklist changed since the last import, or was never imported;
- `landing:<unit>:nogate`: a unit with VERDICT DONE for over `[landing] finished_min` (30) minutes has no gate queued or
  run (gates belong to a unit by `orch gate --unit` or `[landing] gate_names` patterns);
- `landing:<unit>:unlanded`: its gates passed (`gates_required`, default 1) but its worktree HEAD is not on the landing branch.

## 7c. Self-safety

Every script orch writes or replaces (job scripts, lane runners, remote agent scripts, the pueue shim, cap.sh, rewritten
reports) is written to a temp file and renamed into place, never edited in place: a running shell keeps reading its old file.
`orch check` reports `script:<pid>` when a running shell script's file was changed in place after it started (the shell reads
its script as it goes, so it would continue at its old byte offset of the new text, which is how a launcher once looped and
wrote 49,000 ledger rows). The live toolkit is a read-only release copy (section 2), so an agent editing the orch repository
never changes the toolkit that runs it. Every hook denial is logged in `.orch/denied.log`.

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
| `git has no upstream: add a remote, then orch commit-state` | the project repo's branch has no upstream; `orch commit-state` pushes with `--set-upstream` |
| `refused: the <p> ledger ... is malformed` / `[ledger:<p>]` | `orch ledger check` lists the problems; `orch ledger repair` deduplicates by run id (backup first) |
| `no API key: ...` | the provider's `key_keychain` / `key_file` gave no key; `orch doctor` shows which |
| `[script:<pid>] running script ... was changed after it started` | stop that process and relaunch it; write scripts by rename |
| `refused: <wt> has conflict markers` / `is N commits behind main` | finish the merge or merge the landing branch first; `--allow-unmerged` overrides |
| `orch: waiting for admission (<class>; <what blocks>): reserved N job(s): C of K cores, M MB + agents A MB + this job J MB of T MB ...` (in a job log) | the host has not got that much room yet; it starts by itself when it has |
| `[old-rule:<host>] N job(s) still wait under the old cap rule` | jobs queued before the measured rule; re-queue them with `orch run`, then stop the old tasks |
| `(read-only: not the registered orchestrator session ...)` after `orch check` | expected for anyone but the orchestrator: nothing was saved, tuned or stamped |
| `refused: <dir> is the cwd of open unit(s) ...` / `is a main checkout` | give the unit its own worktree |
| `pane ... not found, but the registered session is still running` (watch.log) | the watcher will not start a second orchestrator while the registered one's process lives |
| `orch collect`: `<host>:<id> is Running, not finished` (exit 3) | collect again later; the job is still on the host |
| `orchestrator gone; no provider enabled` (in `.orch/watch.log`) | the watcher will not start a new orchestrator unless a provider is enabled |

Logs: `.orch/watch.log` (watcher), `~/.config/orch/watch.err` (launchd stderr). Hooks that fail print to stderr and let
the action through.

## Known gaps

`install` has been run (links, hooks, launchd); the Codex `/hooks` trust step is still manual.

Open items and their reasons are tracked in `docs/ISSUES.md`. Not yet verified live:
- A remote API agent end to end with the new lane runner and patch collect (dry-run only).
- `watch` starting a new orchestrator (the conditions are tested in code review only), and the context reset.
- A full herdr-backed agent via `orch launch`, including the startup-dialog answers.
- Codex's PreToolUse tool names (the handler accepts `Bash`, `shell`, `local_shell` and any tool input with `command`/`cmd`).

Described in `docs/PLAN.md` or asked for but not in the code: moving unstarted jobs to another host after `unreachable_min`
(the setting is read but unused), pushing state to a bare repo, `MemoryMax` for herdr agents (they run on the Mac, which has
no systemd), host roles (target/build/agents/dev) with parallel gates per host, and the remote agent's git (a remote copy has
no `.git`; its work comes back as a patch).

## Capacity: use all compute, never overload it (orch-fixes8)

- **Admission by reservations.** Every lane job on every host waits in `runs/cap.sh`'s rule: it holds `cores_per_job` cores and
  its job class's memory (the max of the class's last 5 recorded peaks + 15 %, from `runs/history.tsv` on that host; `job_gb`
  until learned) and starts while the sums fit `cores` and `ram_gb - services_gb`. Agents hold the agent class's figure.
- **Back-pressure.** Memory PSI over `psi_pause`, or load over `load_pause` x cores for `hold_s`, pauses admission; running jobs
  are never killed; `orch check` reports `pressure:<host>`.
- **One scheduler per host.** `check` raises `scheduler:<host>` for another job runner or a job that holds no reservation.
- **Agents count against build capacity.** A unit records its build host; `orch launch` refuses, saying when, unless a build
  reservation or a queue position (one queued job per running job) is free there.
- **Fill.** `orch fill --dry-run` lists what fits now and why it stops; `orch fill` launches it; `[fill] watcher = true` lets the
  watcher do it. `check` reports `idle-with-backlog`.
- **Budgets from history.** A unit's default budget is the median cost of its kind and tier + `[budget] margin_pct`; `budget80:<unit>`
  warns at 80 % with a stale report.
- **Landing.** `orch land <unit>` merges the landing branch (stops and dispatches `merge-<unit>` on a conflict), gates the merged
  tree on `[landing] final_gate_host`, then squash-lands.
