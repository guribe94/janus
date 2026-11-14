# janus

Small coordination over tools you already use. One project-owned folder, `PROJECT/.janus`, holds the config, your binding
instructions (`MEMORY.md`), a curated `HANDOFF.md`, briefs, results, a message board and janus-made worktrees. Agents run in Claude Code,
Codex or opencode (or as API runs) on one machine or several over ssh. Each host admits and runs its own work by reservations, and a
read-only monitor shows everything. Python standard library only.

- **Board**: addressed messages between the orchestrator and agents (`board.sh send`, `janus answer`); no broadcasts.
- **Memory and handoff**: `MEMORY.md` entries are never expired or cut and are loaded first; the handoff is short and curated.
- **Resource-aware execution**: per-host discovery and admission by reservations; unknown stays unknown.
- **Landing**: each host merges its own trees; the landing branch moves only from the tip that was merged; pushes are never forced.
- **Verified reset**: the tool's own new-session command, counted done only when a new session registers.
- **Monitor**: `janus monitor`, read-only.

There are no generated gates, mandatory reviews or deadlines: findings (`janus check`) are observations that clear when the condition does.

## Prerequisites
- Required on the coordinating machine: Python 3.11+, `git`, `rsync`, `ssh`, `bd` ([beads](https://github.com/steveyegge/beads)), `pueue` (its daemon `pueued` running).
- Optional: `herdr` (agent panes; the watcher can only start or wake the orchestrator through it) and `tmux` (agent sessions on remote hosts; a fallback pane tool for `janus resume`).
- Each remote host: key-based ssh (BatchMode), `git`, `rsync`, `pueue` (`janus install-host H` sets it up on Linux without sudo) and coreutils `timeout`.
  A host without `timeout` makes each bounded wait fail at once, so janus polls every few seconds instead.
- macOS or Linux (launchd agent or systemd user timer for the watcher). `janus doctor` checks all of this.

## Fresh setup
Order matters: the watcher acts on every project listed in `~/.config/janus/projects`, so configure and register before it runs.

1. **Check and preview** (in the janus checkout; nothing is switched or written outside the release folder):
   ```
   ./check                      # cheap smoke check, not the test suite
   ./install --dry-run          # prints every change a full install would make
   ```
2. **Install the release only** (optional but recommended for a first setup): `./install --release-only` builds, smoke-checks and switches
   `~/.local/share/janus/current`, and links `~/.local/bin/janus` and the skill folders. It adds no hooks and starts no watcher.
   Make sure `~/.local/bin` is on your `PATH`.
3. **Initialize the project** (in `PROJECT`; `bd init` there first if it has no `.beads/`):
   ```
   janus init                   # writes .janus/janus.toml from example.janus.toml (see janus init --name NAME)
   $EDITOR .janus/janus.toml    # hosts, models, routing, API key store, [orchestrator] kind
   janus doctor                 # add --no-hosts to skip ssh checks
   ```
4. **Start the orchestrator yourself** in `PROJECT` (a Claude Code, Codex or opencode session, in herdr or tmux if you use them) and have it
   run `janus register`. It prints `registered <session> (<kind>)` and warns if `[orchestrator] kind` differs from the tool you started.
5. **Activate hooks and the watcher**: run the checkout's `install` with `PROJECT` as the current directory (or pass `--project PROJECT`).
   A full install does the release, edits `~/.claude/settings.json` and `~/.codex/hooks.json` (backed up first; trust Codex's hooks once with `/hooks`),
   installs the watcher (a launchd agent or systemd user timer, every 2 minutes), lists `PROJECT` in `~/.config/janus/projects`, installs missing
   `bd` and `pueue` through Homebrew on macOS, and runs `janus install-host` for each remote host (`--no-hosts` skips that).
   Running a full `./install` before step 4 is allowed, but then the watcher may start an orchestrator itself, in the `[orchestrator]` tool
   with the `[routing]` limits you set, once it finds work and no live orchestrator.
6. `janus doctor` again, then `janus status`.

## Day to day
```
janus monitor                    # from PROJECT or any folder below it; --interval SECONDS (default 5)
janus status                     # the state now;  janus check [--json]  for findings
janus hosts [--refresh]          # what each host has;  janus capacity  for admission now
janus launch UNIT --kind implement --new-worktree REPO    # agents are launched by the orchestrator or `janus fill`
janus land UNIT [--push]         # merge and optionally push, never forced
```
Commands, routing, capacity, locks and findings: [docs/REFERENCE.md](docs/REFERENCE.md).

**Monitor** (read-only; needs an interactive terminal): tabs Active, Backlog, Finished, Board, Overview. Keys: `Tab`/`←`/`→` or `1`-`5` switch tabs,
`↑`/`↓` or `j`/`k` and `PgUp`/`PgDn`/`g`/`G` move, `Enter` opens details (`Esc` closes), `/` filters, `r` refreshes, `?` help, `q` quits.
A host that does not answer is shown as unreachable or stale with the age of its last good reading, never as empty.

**Progress**: the Overview's checkpoint (`~N%`) is the latest line of `HANDOFF.md` that mentions checkpoint, progress, done, complete or finished with a percentage.
It is the orchestrator's rounded estimate, not a measurement. janus computes no completion time. Job expected run times come from past runs of the same
command class and only order the queue and flag overruns (`janus check`); the "a build slot should free about HH:MM" in a capacity refusal is that same estimate, not a promise.

## Models and authentication (your choice)
janus enforces only what you write in `.janus/janus.toml`; it has no built-in vendor or login rule. `[routing] allow_tools` and `allow_auth` limit
which coding clients and which auth kinds (`api_key` or `subscription`) may launch, on launch and on resume alike, and a model outside them is refused.
Unset, nothing is restricted. Only the first entry of each routing list is used unless you set `fallback = true`. For example, an API-key-only setup:
```toml
[routing]
allow_tools = ["claude"]
allow_auth  = ["api_key"]
critical = ["claude"]
```
That is what `example.janus.toml` ships (the key comes from a keychain item or a mode-600 file, and goes only to the agent process); add providers, tools or
`subscription` to the lists to allow them. Put standing rules for agents in `MEMORY.md` with `janus remember --instruction "..."`.

## Checks and tests
There are no generated gates. `./check` is a cheap smoke check; name test files to run just those (`./check test_migrate.py`). For project work run
the checks that matter for the change with `janus run --host H --wt DIR -- CMD`; a whole-suite run needs `--broad REASON`. `janus land` runs no tests:
a clean merge does not prove the merged tree is right.

## Upgrade and migration
- Always preview: `./install --dry-run`. `./install --stage-only` builds and checks a release without switching; `./install --rollback` returns to the previous one.
  Releases are immutable, and one that a running process or queued job names is never pruned.
- A full install rewrites the watcher service and adds hook entries (existing keys kept), so rerun it only when you want that; `--release-only` changes just the release and links.
- **Old layout** (a `janus.toml` at the top of the old root, no `.janus/`): both `./install` and `--release-only` refuse to switch while a registered project is
  still on it. Stage first, migrate with the staged code, then switch:
  ```
  ./install --stage-only
  python3 janus.py migrate OLD_ROOT --to PROJECT           # dry run: lists what moves and what refuses
  python3 janus.py migrate OLD_ROOT --to PROJECT --apply   # only with no jobs, agents or orchestrator running
  ./install --release-only                                 # then ./install when ready for the watcher
  ```
  Migration refuses while anything is live or unknown. It **never writes the bd database**: it prepares `.janus/migration/PENDING.md`, `bd-relocate.sh` and
  `bd-path-updates.sh` for your approval; read them and run them yourself, with watchers, pueue jobs and bd stopped. Temporary links stay at the old paths until then.

## Limits
- Hosts admit work independently, but they share the project's Git remote and landing branch. `janus land` moves the branch only from the tip it merged,
  re-merges a moved branch a bounded number of times and never forces a push; a refused push exits non-zero (`NOT PUBLISHED`) and leaves the remote untouched.
  Concurrent landings can still conflict and need you.
- janus is not a sandbox or a security boundary. Agents run with whatever permissions you configure (the example passes `--dangerously-skip-permissions`),
  hosts are trusted over your ssh keys, and no scale or hardening claim is made. opencode has no automatic size trigger and no end-to-end live run was made.
- Capacity counts configured resources against measured pressure; see [docs/REFERENCE.md](docs/REFERENCE.md#capacity).

Configuration: [example.janus.toml](example.janus.toml). Design: [docs/DESIGN.md](docs/DESIGN.md). Watcher watchdog on another machine: [docs/HEARTBEAT.md](docs/HEARTBEAT.md).
