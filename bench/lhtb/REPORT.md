# LHTB pilot report: janus harness vs stock Claude Code

Status: **not run yet** (waiting for the Docker host). Method in `README.md`; this file is filled from `report.py` and the
trial logs when the run is done. Keep it short: a reader should know the answer in the first paragraph.

## Answer
_(one paragraph: did arm B beat arm A, by how much, at what cost; what we conclude and what we will do about it)_

## Numbers
_(the table from `python3 bench/lhtb/report.py`: per task, reward and cost per arm, paired delta; then the per-arm summary)_

## What we saw in the logs
_(3-6 bullets, each with the trial path: where janus helped (units, verification, recovery after a rejection), where it hurt
(time spent on orchestration instead of the task, refused hand operations, idle waiting), anything that broke)_

## Caveats
- 40-minute budget, Sonnet 5.5, 6 tasks, 2 trials: coarse; not comparable with the LHTB leaderboard.
- One container = one host: janus's multi-host coordination is not exercised.
- Arm B's cost includes its sub-agents (janus ledger); both arms share the same per-trial wall budget.

## Next step
_(run a larger set / Opus / full budget, or stop; and the harness changes the logs suggest)_
