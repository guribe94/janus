# janus

Small coordination over the tools you already use. One project-owned folder, `PROJECT/.janus`, holds the config, the user's binding
instructions (`MEMORY.md`), a curated `HANDOFF.md`, briefs, results, a shared message board and janus-made worktrees. Agents run in
Claude Code, Codex or opencode on one machine or several over ssh; each host admits and runs its own work by reservations; a read-only
monitor shows it all. Python 3.11+, standard library only; needs `git`, `bd` (beads), `pueue`, and `herdr` or `tmux`.

## What it does
- **Shared board**: addressed messages between the orchestrator and agents of any tool (`board.sh send`, `janus answer`); no broadcasts.
- **Memory and handoff**: instructions and decisions keyed in `MEMORY.md` (never expired or cut, loaded first, sent to running agents when they change); a short curated handoff.
- **Resource-aware execution**: per host discovery (OS, arch, CPU, RAM, disk, GPU, tools, coding clients) and admission by reservations; ready authorized work fills every capable host; unknown stays unknown.
- **Safe landing**: each host merges its own trees; the landing branch moves only from the tip that was merged; pushes are never forced.
- **Verified reset**: the tool's own new-session command, counted done only when a new session registers.
- **Monitor**: `janus monitor` (read-only curses): overview, work, board, machines.

There are no generated gates, mandatory reviews, acknowledgements or deadlines: findings are observations that clear when the condition does.

## Use
```
./check                       # cheap smoke check (name test files to run them: ./check test_migrate.py)
./install --stage-only        # build and check an immutable release without switching to it
./install                     # release, skill links, hooks, watcher
janus init                    # in the project: writes .janus/janus.toml
janus doctor
janus migrate OLD_ROOT --to PROJECT        # an existing project: dry run; add --apply
```
Configuration: [example.janus.toml](example.janus.toml). Commands, routing, capacity, reset: [docs/REFERENCE.md](docs/REFERENCE.md). Design: [docs/DESIGN.md](docs/DESIGN.md).
Also: [docs/HEARTBEAT.md](docs/HEARTBEAT.md) (a watchdog on another machine), [docs/SANDBOX.md](docs/SANDBOX.md) (optional write limits, not wired into launches).
