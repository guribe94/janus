"""The orchestrator's context reset at a safe boundary (fixes10 item 8, finished in imp-autopilot).

The runtime compacts a long context on its own, lossily. janus resets it first, below [project] reset_tokens (default 120000):
when a Stop passes (no late finding, notes fresh: everything worth keeping is in HANDOFF.md), stop_reset starts a detached
`janus watch --reset`, which sends /clear once the turn has ended and then the wake prompt. Every skipped reset says why in
watch.log; check raises context when the context is over the threshold and no reset was asked."""
import json, os, subprocess, sys, time
from janus.context import JANUS_MARK, JANUS_SELF, WAKE_CLEAR, clean_env, herdr, now, ts

def log_line(cfg, m): cfg.f("watch.log").open("a").write(f"{ts()} {m}\n")

def context_tokens(path):
    """The current context in tokens: the last assistant turn's usage in the transcript (input + cache creation + cache read),
    read from the file's tail; else the file size / 4 (a rough upper estimate)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 1_000_000)); lines = f.read().decode(errors="replace").splitlines()
    except OSError: return 0
    for l in reversed(lines):
        if '"usage"' not in l: continue
        try: u = (json.loads(l).get("message") or {}).get("usage") or {}
        except (ValueError, AttributeError): continue
        t = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if t: return t
    return n // 4

def orch_tokens(cfg):
    tr = cfg.f("transcript").read_text().strip() if cfg.f("transcript").exists() else ""
    return context_tokens(tr) if tr else 0

def stop_reset(cfg, blocked_by=None):
    """Called by the orchestrator's Stop hook. blocked_by: why this Stop is not a safe boundary (late findings, stale notes).
    Over the threshold it starts the reset, or logs why not. Returns what it did ("asked", or the reason it skipped)."""
    tok = orch_tokens(cfg)
    if tok <= cfg.reset_tokens: return "under the threshold"
    why = (f"not a safe boundary ({blocked_by})" if blocked_by else
           "a reset was asked under 10 min ago" if now() - cfg.mtime("reset_asked") < 600 else None)
    if why: log_line(cfg, f"context reset skipped at Stop ({tok} tokens over {cfg.reset_tokens}): {why}"); return why
    cfg.touch("reset_asked"); log_line(cfg, f"context reset at Stop: {tok} tokens over {cfg.reset_tokens}")
    if not os.environ.get("JANUS_IN_TESTS"):
        subprocess.Popen([sys.executable, str(JANUS_SELF), "watch", "--reset"], cwd=cfg.root, env=clean_env({"JANUS_ROOT": str(cfg.root)}),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return "asked"

def reset_now(cfg, wait_s=60):
    """`janus watch --reset`: once the orchestrator's turn has ended, /clear, then the wake prompt."""
    pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    if not pane: log_line(cfg, "context reset skipped: no orchestrator pane"); return
    for _ in range(max(1, wait_s // 5)):
        time.sleep(5)
        if ((herdr("agent", "get", pane) or {}).get("agent") or {}).get("agent_status") != "working": break
    else: log_line(cfg, f"context reset skipped: the orchestrator kept working for {wait_s} s"); return
    herdr("agent", "prompt", pane, "/clear"); time.sleep(5); herdr("agent", "prompt", pane, JANUS_MARK + WAKE_CLEAR.format(reason="the context was cleared"))
    log_line(cfg, "context reset: /clear, then wake")

def context_findings(cfg):
    """context: the orchestrator's context is over reset_tokens and no reset was asked in 15 min (it would soon be compacted)."""
    tok = orch_tokens(cfg)
    if tok > cfg.reset_tokens and now() - cfg.mtime("reset_asked") > 900:
        return [("context", f"orchestrator context is {tok} tokens, over [project] reset_tokens {cfg.reset_tokens}, and no reset was asked: "
                 "update the notes and end the turn (the Stop hook then resets it); watch.log says why a reset was skipped")]
    return []
