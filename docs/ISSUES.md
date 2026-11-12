# Issue register

Every problem met with the orchestrating skill, fixed at its root cause in the skill (generic, shareable). Kept current per
round. Status: **fixed (round)** = fixed in code, with the proof named; **verify** = believed fixed, still to be seen working
live; **open** = not fixed yet, with the reason. First filled 2026-11-11 (orch-fixes7) from the owner's register.

| # | Issue seen | Root cause | Status |
|---|---|---|---|
| 1 | Watcher started a duplicate orchestrator while the real one was alive | Watcher trusted a pane id, not the session's process | fixed (fixes6): pane absent + process gone + transcript quiet 20 min |
| 2 | Register wrote the wrong pane; an agent's test overwrote `.orch/session` and `.orch/pane` | Pane from `herdr pane current` (the focused pane); agents could register | fixed (fixes7): pane only from the caller's `HERDR_PANE_ID`; `orch register` refuses under `ORCH_ROLE=agent`; tests use temp projects |
| 3 | Mac probe read 0.0 GB and an empty load under load | launchd's PATH has no /usr/sbin, where `sysctl` lives; an empty reading printed as 0.0 | fixed (fixes7): /usr/sbin:/sbin always on the probe's and clean_env's PATH; an empty reading shows as unknown; verify under the watcher |
| 4 | `tune_pueue` tuned only the jobs group | Agents group not tuned | fixed (fixes6); verified 2026-11-11: Mac agents group at 14 |
| 5 | Admission by full caps left hosts idle; then 8 builds at load 17 on 8 cores | No real-use and no CPU rule | fixed: measured working set (migration); CPU rule (fixes7): (running jobs + 1) x `cores_per_job` <= cores. Mac local jobs lane has no admission script yet: open (the Mac jobs lane is capped by `services_gb` slots) |
| 6 | Running units counted as ready (false idle) | Launch left the issue open | fixed (fixes5/6): launch marks in_progress; ready work skips units |
| 7 | Mac had room for 4 agents while backlog existed, unflagged | Backlog not in beads; no check | fixed (fixes7): `orch backlog import`; finding `idle-backlog:<host>` |
| 8 | Finished units sat with no gate, unflagged | No check | fixed (fixes7): findings `landing:<unit>:nogate` and `:unlanded` ([landing] in orch.toml); `orch gate --unit` records gates |
| 9 | Notifications and cycle prompts filed as owner instructions | Prompt filter | fixed (fixes4): orch's prompts start with `[orch] `; task notifications skipped. verify live |
| 10 | Hook denied heredoc text, commit messages, rg patterns, read-only loops, read-only pueue queries | Loop rule searched the whole text; `pueue --color x` misparsed | fixed (fixes7): loop judged from command words and unquoted text; heredoc piped to a shell judged as commands; `--color` parsed; every denial logged in `.orch/denied.log`; tests/test_orch.py Hooks. Note: `$( )` and backticks inside double quotes do run in bash, so they stay judged |
| 11 | Hook bypass by a prefix (`orch status; rm -rf`) | Whole-command allowlist | fixed (fixes6) |
| 12 | Destructive git, pueue, bd, herdr commands not covered | Incomplete rules | fixed (fixes6) |
| 13 | Hooks acted only with cwd inside the project | Project found from cwd | fixed (fixes6) |
| 14 | API key stored in pueue `state.json` | `pueue add` inherits the caller's environment | fixed (fixes6: clean env; fixes7: `orch api-run` reads the key itself, at run time); key rotation: owner |
| 15 | Secret scan flagged prefix and PEM mentions | Patterns without length/structure | fixed (fixes4, migration) |
| 16 | `orch stop` left detached children | Stop did not end process groups | fixed (fixes6) |
| 17 | Capacity refused a cleanup unit on disk, then load | Limits applied to cleanup | fixed (fixes4, fixes6) |
| 18 | `orch close` would remove a worktree another unit used | No shared-cwd check | fixed (fixes6) |
| 19 | Close refused branches pushed without upstream | Pushes did not set upstream | fixed (fixes7): `orch commit-state` pushes with `--set-upstream` when the branch has none; close does not refuse a branch without upstream |
| 20 | `orch check` changed state | check not read-only | fixed (fixes6) |
| 21 | Board tab used `watch`; old cards never closed; SILENT/GONE ignored | Tool dependency; no close path | fixed (fixes6: shell loop, `orch board-close`, board findings); verify live |
| 22 | A remote agent ran cargo directly, bypassing admission | Brief header is advice only | open: the header routes builds through `<root>/run/<unit>.lane` (fixes5); enforcement on remote hosts (a burner/test finding there like the Mac's) is not built |
| 23 | An agent ran CPU burners on the Mac, again after a direction | Detection only; directions advisory | partly: burners and local tests are findings (fixes6); stopping is `orch stop`. Open: no automatic stop |
| 24 | Agents' own `pueue add` jobs looked like orphans | No unit label | fixed (fixes7): the agents' pueue shim adds `-l unit:$ORCH_UNIT` |
| 25 | Agents placed on the Mac by default | Placement ignored where builds run | fixed (fixes5): --test-host, TEST-HOST:, [placement], sticky host |
| 26 | A resumed unit continued its original report; check read the wrong path | Unit-to-report mapping | fixed (fixes7): `--resume-of`/`--report`/brief line; the unit records `report`; check, close, collect and the Stop hook use it; an old verdict does not count |
| 27 | Launch failed on Codex startup dialogs, leaving an orphan | Unit recorded after start; dialogs | fixed (fixes5 item 10): unit recorded first; dialogs passed; verify live |
| 28 | orch.toml named Codex as orchestrator while Claude was registered | Config not checked | fixed (fixes6): finding `orchestrator:kind` |
| 29 | Live symlink install: half-edited orch.py was live ($342k reading) | Install linked the working tree | fixed (fixes7): `install` builds `~/.local/share/orch/releases/<v>`, tests the copy, switches `current` atomically; links point at `current` |
| 30 | A launcher edited while running looped, 49,000 ledger rows | zsh launcher edited in place; ledger not idempotent | fixed (fixes7): orch runs API agents (`orch api-run`, Python); ledger rows idempotent per run id; scripts orch writes are replaced by rename; check flags a script edited in place under a running shell |
| 31 | Cap computed over all ledger rows | No one-done-row-per-run rule | fixed (fixes7): spend per run id |
| 32 | An agent ran out of budget without its report | No budget check | fixed (fixes7): prompt asks for the report after every step; finding `unit:<u>:budget` when api.json shows an error subtype and no verdict |
| 33 | A gate was queued after a failed merge and synced a conflicted tree | No guard | fixed (fixes7): `orch run` refuses a tree with MERGE_HEAD, unmerged files, conflict markers, or more than `[landing] max_behind` commits behind |
| 34 | Status checklist not editable by the orchestrator | Allowlist | fixed (fixes5/6): `paths.checklist` |
| 35 | Old state docs went stale | No pointer handling | fixed (fixes6): `orch init` pointer |
| 36 | A gate outlived its intent and could not be cancelled through orch | No cancel | fixed (fixes7): `orch cancel <host>:<task>` |
| 37 | Admission on linux2 reserved the running job's full cap | Reservation-based admission | verify (migration: measured working set) |
| 38 | Gates on linux2 failed for environment reasons | Project-specific | not a skill issue (project unit linux2-gate-env) |
| 39 | Spend shown wildly wrong with no alarm | No ledger sanity check | fixed (fixes7): malformed ledger refuses launches and is a finding; `ledger:<p>:implausible` when spend > 1.5 x cap or a run cost > 2 x its budget |
| 40 | A unit could edit the toolkit while it ran | No self-protection | fixed (fixes7): the live toolkit is a read-only release copy, never the working tree; in-place script edits flagged |
| 41 | Gates ran on branches 26-27 commits behind main; merges then conflicted or went untested | No landing flow | partly (fixes7): gates and jobs refuse trees behind the landing branch, mid-merge or with conflict markers (row 33). Open: the `orch land <unit>` flow (merge current main, stop on conflict and dispatch a merge unit, gate the merged tree, squash-land) is not built: it writes to the main checkout, which needs its own design and review round |
