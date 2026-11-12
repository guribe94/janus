# Plan history

Deliberate history: this file quotes the names a thing had when it was decided, so a reader can follow a rename. The tool
itself carries only its current name; only this file and `docs/ISSUES.md`'s fixed-history rows keep the old ones.

## The rename: orch -> janus (owner ruling, 2026-11-11)

No alias and no backward compatibility: the old name is gone from the code, tests, docs and prompts, and an existing project
is moved once by the command below. `rg -i '\borch\b'` over the repo finds only this file, `docs/ISSUES.md`'s history rows,
and the migration code that has to name the old files.

| Was | Is |
|---|---|
| the command `orch` | `janus` |
| `orch.py` | `janus.py` |
| the package `orchkit/` | `janus/` |
| the config `orch.toml` (and `example.orch.toml`) | `janus.toml` (and `example.janus.toml`) |
| the state folder `.orch/` | `.janus/` |
| the env `ORCH_*` (`ORCH_ROOT`, `ORCH_UNIT`, `ORCH_ROLE`, ...) | `JANUS_*` |
| the skill `orchestrating` (`~/.claude/skills/orchestrating`, `~/.codex/skills/orchestrating`) | `janus` |
| the watcher label `com.orch.watch` (`launchd/com.orch.watch.plist`) | `com.janus.watch` (`launchd/com.janus.watch.plist`) |
| the hook commands `orch hook <event>` | `janus hook <event>` |
| the release store `~/.local/share/orch`, the link `~/.local/bin/orch`, `~/.config/orch/projects` | `.../janus` |

`janus migrate <project>` does this once, idempotently: it renames the project's `orch.toml` and `.orch/`, rewrites the hook
commands a previous install wrote, and replaces the watcher. It is the only code place the old names remain, on purpose.

Before the rename the package had already been split out of the one file (`orchkit/`), the board contract had moved to
`orchkit/msgboard.py` with `board.sh` a shim, and unit records had a `v: 2` codec; those are recorded as fixed in
`docs/ISSUES.md`.
