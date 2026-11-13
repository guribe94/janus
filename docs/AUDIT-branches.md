# Branch audit: the twelve archived branches against main (2026-11-13)

When the secondary worktrees were removed, twelve branches still held commits that `git cherry` listed as not in main.
Their history had been re-based by the orch -> janus rename, so patch ids differ; this audit compares what each commit
does against what main does. The refs are kept as archives (nothing deleted).

Method, per branch: (1) main holds a commit titled `merge <branch>` (a single-parent integration commit, list below);
(2) the branch's added lines against the lines that commit added: at least 94% match, the rest are explained below;
(3) every unique commit's behaviour was found in the current code and, where there is one, its test. A line-match
percentage alone was not accepted: each difference was read.

| Branch (tip) | Integrated in main | Commits and where they live now | Verdict |
|---|---|---|---|
| feat/adr-docs (b4b8b5e) | 21a0d79 | DECISIONS.md, KNOWLEDGE.md, `skill/templates/decision.md` (status line `accepted \| superseded by NNNN`) | incorporated; orch -> janus names only |
| feat/heartbeat (b8e22f8) | b39f239 | `heartbeat/{beat,watchdog,install-watchdog}` are identical to the tip after the rename; `tests/test_heartbeat.py` folds the repeat-alert test into the stale-beat test | incorporated |
| feat/sandbox (f9807fa) | 5169518 | `sandbox/run`, `macos.sb.in`, `tests/test_sandbox.py` identical; `linux.sh` gained a write descriptor (fd 4) for the options file (fix48); SANDBOX.md now says "optional, not wired" (panel-eng F6) | incorporated, later fixes on top |
| feat/readme (9e0200c) | 60ef368 | README rewrite and REFERENCE capacity summary (`janus capacity`, REFERENCE section 3); README was rewritten again later (0594049, verified against `--help`) | superseded by the newer README |
| imp/adr (8c8a6b0) | 062bc3d | `janus/decisions.py` (decide, decisions, ask, answer; agent refusal via `refuse_agent`; dry run writes nothing), findings `decision-missing`, `owner-question`, SKILL knowledge map, SessionStart decision titles; `tests/test_decisions.py` covers each (dry run, agent refusal, injection) | incorporated |
| imp/awareness (77eb0f4) | c1db3ba | `cmd_introduce`, `tend_related` (`actions/swarm.py`), `findings/awareness.py` (`related:`, `note:`), `board.sh note`, RELATED and predecessor pointers in the brief header; `tests/test_awareness.py` | incorporated (371 of 371 added lines) |
| imp/quality (139a90b) | 6e36cec | `quality_block` and `lint_brief` with `SCOPE_RX` accepting `Your modules:` and `## Scope` (the last commit), `report.json`, test-signal finding, `quality review`, `janus soak`; docs moved from README 7d to REFERENCE 6e | incorporated |
| imp/autopilot (c25bb12) | 449bace | writer lock and atomic writes (`tests/test_state.py`), autopilot ending policy (`relaunch_max`), staleness and `reset_tokens` Stop-time reset, `MAXRSS_PY` peak for the flaky capacity test, direct-build `[enforce] grace_min` / `stop_direct`, idle herdr agent (`agent_ended`); `tests/test_autopilot.py` | incorporated |
| imp/sched (88b4bce) | 6919a10 | host discovery in `platform.py`, `janus hosts`, `--needs` placement, learned cores and `--broad` in `ADMIT`, `install-host`, `agent_env`, runs dir disk fix, install-host keeps pueue config | incorporated; 7 missing lines are a README rewrite and an import moved |
| imp/resume (fc0287e) | 40de75d | `cmd_resume` and takeover (`setup.py`: `takeover_wait_s`, pull before takeover, diverged refusal, pending-start refusal), `session_start_text` in `hooks.py`; `tests/test_resume.py` | incorporated; the SessionStart text was merged with fix-prompts' instruction-first form (stated in 40de75d) |
| fix-prompts (ef10b30) | ee5b2c3 | park-aware agent Stop hook, `stop_hook_active` limit, VERDICT line wins over PENDING, labelled capped SessionStart parts, claim by pane (`pending:<ts>:<pane>`), shorter SKILL.md; 355 of 355 added lines | incorporated |
| fix-docs (cabda52) | be19066 | start-the-session text, decision commands, take over and hand back, doctor project-listing wording, brew only what is missing, init host line, PSI gloss, how-tos (REFERENCE "A unit failed", "Adding a model"), REFERENCE 4a recipes | incorporated except one sentence: the shared-machine note of `check` was lost in the later README cut; restored in REFERENCE section 3 by this audit (the behaviour is in `check.py`) |

## Result

Missing and still applicable: one doc sentence (fixed). Nothing else; no code from these branches needs integrating, and
re-applying them would revert later work (the rename, the README rewrites, the fd 4 fix). The branch refs stay as archives:
`git log main..<branch>` still lists their commits.
