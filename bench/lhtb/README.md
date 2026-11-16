# LHTB pilot: does the janus way of working help one Claude Code session?

[LHTB](https://github.com/zli12321/LHTB) (Long-Horizon Terminal-Bench, arXiv:2607.08964) drops an agent into a container and
grades it with hidden verifiers over hours. It cannot see a fleet, so it does not measure janus across hosts; it can measure
whether the janus harness (skill, briefs, units, verification, hooks) makes one Claude Code session do better long work than
stock Claude Code. That is the question this pilot answers. The write-up is `REPORT.md`.

## Design
- **Arm A** (`arm_a_stock.yaml`): Harbor's stock `claude-code` agent.
- **Arm B** (`arm_b_janus.yaml`, `agent.py`): the same agent with janus, pueue and bd installed in the container, a one-host
  `.janus` project at `/app`, the janus skill and hooks, and a system prompt saying it is the orchestrator. Sub-agents are
  `janus launch` units (API runs through pueue in the same container, same model).
- Same model (Sonnet 5.5), same 6 tasks, **same 40-minute budget per trial** (`override_timeout_sec`): LHTB's
  continue-until-timeout makes the agent use the whole budget, so the budget is the cost driver; capping it keeps the
  comparison fair and the pilot cheap. Absolute scores are lower than the leaderboard's and not comparable with it.
- 6 tasks with dense partial credit and 4 GB images: four 2.0 reconstruction tasks (sqlite WAL recovery, nginx location
  precedence, duckdb hierarchy report, pcap TLS fingerprint) and two software tasks (commit0 multilib TDD, langchain
  migration). 2 trials per cell: 24 trials, ~8 h wall at 2 concurrent.
- `report.py`: mean reward, solved (reward >= 0.95), cost and wall time per arm; paired B - A per task. Arm B's cost adds
  the sub-agents' spend from the container's janus ledger, which Harbor does not see.

## Run (on the Docker host)
```
git clone https://github.com/zli12321/LHTB ~/code/LHTB      # tasks + the bundled Harbor (continue-until-timeout)
python3 -m venv ~/.local/share/lhtb-venv && ~/.local/share/lhtb-venv/bin/pip install -e ~/code/LHTB/harbor
export PATH=~/.local/share/lhtb-venv/bin:$PATH ANTHROPIC_API_KEY=... DOCKER_DEFAULT_PLATFORM=linux/amd64
cd ~/code/orchestrator
harbor run -c bench/lhtb/arm_a_stock.yaml
PYTHONPATH=. harbor run -c bench/lhtb/arm_b_janus.yaml
python3 bench/lhtb/report.py                               # markdown for REPORT.md
```
Smoke test first: `harbor run -c ~/code/LHTB/configs/examples/oracle_smoke.yaml` (no API key). Harbor shells out to
`docker` and `docker compose`; on a podman host use the docker CLI shim and `DOCKER_HOST` at podman's socket.

## What a result means
With 6 tasks x 2 trials the test is coarse. A paired mean delta of about +-0.1 reward with most tasks agreeing is a real
signal; smaller than that with tasks split is "no visible difference". Either answer is useful: it decides whether a
larger run (more tasks, Opus, the full budget) is worth its cost.
