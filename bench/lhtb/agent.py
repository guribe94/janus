"""JanusClaudeCode: Harbor's Claude Code agent with janus installed in the task container as a one-host project.

Arm B of the LHTB pilot (README.md). Everything Harbor's stock agent does stays the same (claude -p, bypassPermissions,
stream-json log); on top, install() puts janus, pueue and bd in the container, seeds /app/.janus (one local host, one
API model: the same model the stock arm uses), starts pueued, installs the janus skill and hooks, and writes the API key
to the key file janus reads (mode 600, never printed). The task instruction reaches the orchestrator unchanged; the run
config's append_system_prompt tells it that it is the orchestrator and that the task is the whole backlog.

Secrets: the key file is written from $ANTHROPIC_API_KEY inside the container with umask 077; nothing is echoed."""
import os
import shlex
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment

REPO = Path(__file__).resolve().parents[2]   # the orchestrator checkout: janus.py, janus/, board.sh, skill/
PUEUE_URL = "https://github.com/Nukesor/pueue/releases/latest/download"   # janus/setup.py uses the same static builds
BEADS_URL = "https://github.com/steveyegge/beads/releases/download/v1.3.4/beads_1.3.4_linux_{arch}.tar.gz"
WORK = "/app"            # the task's working directory (Terminal-Bench convention); the janus project lives at /app/.janus
LEDGER = "/logs/agent/janus-ledger.tsv"   # under Harbor's agent log dir, so the sub-agents' spend comes home with the trial

# One local host sized for the task container (2 cpus, 4 GB); one API model, named by the run config (model_name).
JANUS_TOML = """[project]
name = "lhtb"
beads = ".janus"

[hosts.box]
local = true
ram_gb = 4
cores = 2
reserve_gb = 0.5
agent_gb = 0.5
job_gb = 1
min_disk_gb = 1

[providers.claude_api]
enabled = true
cap_usd = {cap}
budget_per_unit = {per_unit}
ledger = "{ledger}"
key_env = "ANTHROPIC_API_KEY"
key_file = "~/.config/janus/anthropic.env"

[models.main]
provider = "claude_api"
vendor = "anthropic"
kind = "api"
model = "main"
api_id = "{api_id}"
hosts = ["box"]

[routing]
allow_tools = ["claude"]
allow_auth = ["api_key"]
critical = ["main"]
review = ["main"]
normal = ["main"]
"""

HOOKS = """{"hooks": {
 "SessionStart": [{"hooks": [{"type": "command", "command": "janus hook SessionStart", "timeout": 60}]}],
 "PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit|apply_patch", "hooks": [{"type": "command", "command": "janus hook PreToolUse", "timeout": 60}]}],
 "Stop": [{"hooks": [{"type": "command", "command": "janus hook Stop", "timeout": 60}]}]}}
"""


class JanusClaudeCode(ClaudeCode):
    """claude-code + janus in the container. Run config: import_path: bench.lhtb.agent:JanusClaudeCode."""

    def __init__(self, logs_dir, *args, cap_usd=20, budget_per_unit=3, **kwargs):
        self._cap_usd, self._per_unit = cap_usd, budget_per_unit
        super().__init__(logs_dir, *args, **kwargs)

    @staticmethod
    def name() -> str:
        return "janus-claude-code"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        # tools, as root: git and python3 (3.11+, tomllib) are in every chosen image; pueue and bd are static release binaries
        await self.exec_as_root(environment, command=(
            "set -e; arch=$(uname -m); case $arch in x86_64) parch=x86_64; barch=amd64;; aarch64) parch=aarch64; barch=arm64;; *) echo unsupported $arch; exit 1;; esac; "
            f"for x in pueue pueued; do curl -fsSL -o /usr/local/bin/$x {PUEUE_URL}/$x-$parch-unknown-linux-musl && chmod +x /usr/local/bin/$x; done; "
            f"curl -fsSL {BEADS_URL.replace('{arch}', '$barch')} | tar -xz -C /usr/local/bin bd && chmod +x /usr/local/bin/bd; "
            "python3 -c 'import tomllib' && pueue --version && bd --version"))
        # janus itself: this checkout's janus.py, package, board.sh and skill, at /opt/janus; `janus` on PATH for every user
        for src, dst in (("janus", "/opt/janus/janus"), ("skill", "/opt/janus/skill")):
            await environment.upload_dir(REPO / src, dst)
        for f in ("janus.py", "board.sh"):
            await environment.upload_file(REPO / f, f"/opt/janus/{f}")
        await self.exec_as_root(environment, command=(
            "chmod -R a+rX /opt/janus && chmod +x /opt/janus/janus.py /opt/janus/board.sh && ln -sf /opt/janus/janus.py /usr/local/bin/janus && janus --help >/dev/null"))
        # the project, as the agent: key file, janus.toml, beads, pueued, the skill and hooks Claude Code reads from the project
        api_id = self._parsed_model_name or "claude-sonnet-5-5"
        toml = JANUS_TOML.format(cap=self._cap_usd, per_unit=self._per_unit, ledger=LEDGER, api_id=api_id)
        await self.exec_as_agent(environment, command=(
            "set -e; umask 077; mkdir -p ~/.config/janus && printf 'ANTHROPIC_API_KEY=%s\\n' \"$ANTHROPIC_API_KEY\" > ~/.config/janus/anthropic.env; umask 022; "
            f"mkdir -p {WORK}/.janus {WORK}/.claude ~/.claude/skills/janus && cd {WORK} && "
            f"printf %s {shlex.quote(toml)} > .janus/janus.toml && printf %s {shlex.quote(HOOKS)} > .claude/settings.json && "
            "cp /opt/janus/skill/SKILL.md ~/.claude/skills/janus/SKILL.md && "
            "(cd .janus && { [ -d .beads ] || bd init >/dev/null 2>&1; } && bd list >/dev/null) && "
            "(pueue status >/dev/null 2>&1 || pueued -d) && sleep 1 && pueue status >/dev/null && "
            # the watcher the Mac gets from launchd (every 120 s): park/resume, tidy, orphans; its log comes home with the trial
            "(nohup sh -c 'while sleep 90; do janus watch; done' >> /logs/agent/janus-watch.log 2>&1 </dev/null &) && "
            "janus doctor | tail -3"),
            env={"ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")})
