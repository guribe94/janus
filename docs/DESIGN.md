# Design

Small coordination over the tools that already exist: a shared board, concise persistent memory and handoff, resource-aware execution on
each host, workers in any coding tool, verified context refresh, and a read-only monitor.

- **One root.** `PROJECT/.janus` owns config, memory, handoff, briefs, results, the board and janus-made worktrees. One resolver
  (`context.find_root`) serves the CLI, monitor, hooks, registry, watcher, install and setup. Source repositories stay where they are.
- **Observations, not bookkeeping.** `check` derives findings from hosts, units, board and queues now; there is nothing to acknowledge, no
  deadline, no stale-note tracking. An unreachable host is unknown, never a reason to move or relaunch work.
- **Memory is the one place for binding instructions** (`janus/memory.py`); routing restrictions live once in `janus.toml` and are enforced
  by `ledger.model_ok`. Handoff is curated, not generated.
- **Each host owns its work.** Admission, tests and merge preparation run where the tree is (`janus run`, `janus land` over ssh); the host's
  own admission is the final authority and the central estimate (`cap.host_cap`) agrees with it.
- **Publication is protected, not gated.** `land` squashes from the tip it merged and moves the branch only from that tip (expected ref),
  pushes unforced, and re-merges a moved branch a bounded number of times. No test run is implied.
- **Adapters only where tools differ:** launch (herdr/tmux/API run), resume, status, reset (`reset.RESET_CMD`, verified by a new session).
- **Standard tools:** bd (units, backlog), pueue (queues), herdr/tmux (panes), git, rsync, ssh, launchd/systemd, one stdlib package.

Modules: `context` (root, config, locks), `memory`, `hooks`, `reset`, `watch`, `check` + `findings/`, `board` + `msgboard` (the board contract),
`actions/{launch,jobs,lifecycle,landing,backlog,swarm,tidy}`, `probe`/`cap`/`platform` (discovery, admission), `migrate`, `monitor`, `setup`.
