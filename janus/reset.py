"""The orchestrator's context reset, at a safe boundary.

The runtime compacts a long context on its own, lossily. janus resets it first, above [project] reset_tokens (default 120000),
using what each tool supports and checking that it happened: Claude Code `/clear` and Codex `/new` start a new session (their
SessionStart hook then registers it; the harness binaries' own command tables were read for this), and the reset counts as done
only when the registered session id and transcript are new. Any other tool is reported unsupported: nothing is claimed.
Before it, at the turn's Stop, the orchestrator is asked once to bring HANDOFF.md up to date (MEMORY.md is always current);
after it the new session is told to read both."""
import json, os, subprocess, sys, time
from janus.context import JANUS_MARK, JANUS_SELF, atomic_write, clean_env, herdr, memory_path, handoff_path, now, ts, wake_clear

# tool kind -> the slash command that starts a new session there (read from each binary's command table, not assumed)
RESET_CMD = {"claude": "/clear", "codex": "/new"}

def log_line(cfg, m): cfg.f("watch.log").open("a").write(f"{ts()} {m}\n")

def _num(v):
    """A usable token count: a non-negative int (a float is truncated); anything else (None, text, bool, negative, nan) is 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)): return 0
    try: return max(0, int(v))
    except (ValueError, OverflowError): return 0

def _dict(v): return v if isinstance(v, dict) else {}

def record_tokens(rec):
    """The context one transcript record reports, or 0. Claude: message.usage (input + cache creation + cache read, disjoint counts).
    Codex: event_msg token_count's info.last_token_usage, or a token_usage_record's usage, both the latest request's usage whose
    input_tokens already includes cached_input_tokens (a subset, not added); total_token_usage and the turn/thread totals are
    cumulative over the session and are never read."""
    rec = _dict(rec); p = _dict(rec.get("payload"))
    if "usage" in _dict(rec.get("message")):
        u = _dict(rec["message"]["usage"])
        return sum(_num(u.get(k)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if rec.get("type") == "event_msg" and p.get("type") == "token_count":
        return _num(_dict(_dict(p.get("info")).get("last_token_usage")).get("input_tokens"))
    if rec.get("type") == "token_usage_record": return _num(_dict(p.get("usage")).get("input_tokens"))
    return 0

def context_tokens(path):
    """The current context in tokens: the newest usable usage record in the transcript (Claude message.usage or Codex token_count /
    token_usage_record, see record_tokens), read from the file's tail; else the file size / 4 (a rough upper estimate)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 1_000_000)); lines = f.read().decode(errors="replace").splitlines()
    except OSError: return 0
    for l in reversed(lines):
        if "usage" not in l and "token_count" not in l: continue
        try: t = record_tokens(json.loads(l))
        except (ValueError, AttributeError, TypeError, RecursionError): continue
        if t: return t
    return n // 4

def orch_tokens(cfg):
    tr = cfg.f("transcript").read_text().strip() if cfg.f("transcript").exists() else ""
    return context_tokens(tr) if tr else 0

def stop_reset(cfg, d):
    """The orchestrator's Stop hook. Under the threshold: nothing. Over it, at most one reminder to update HANDOFF.md (a Stop that
    comes after the reminder, or with the handoff newer than the request, is the safe boundary): then a detached
    `janus watch --reset` starts. Returns the reminder text to block with, or None. Never blocks twice, never in a user's wait."""
    tok = orch_tokens(cfg)
    if tok <= cfg.reset_tokens: return None
    if now() - cfg.mtime("reset_asked") < 600:
        log_line(cfg, f"context reset not repeated: one was asked under 10 min ago ({tok} tokens)"); return None
    asked = cfg.mtime("handoff_asked")
    if not d.get("stop_hook_active") and not (asked and handoff_path(cfg).exists() and handoff_path(cfg).stat().st_mtime > asked):
        cfg.touch("handoff_asked")
        return (f"[janus] The context is {tok} tokens, over the reset threshold: bring {handoff_path(cfg)} up to date now (objective, decisions, "
                f"blockers, next actions: curated, short; binding instructions belong in {memory_path(cfg)} via janus remember), then end the turn. "
                "janus then starts a new session and loads both.")
    cfg.touch("reset_asked"); log_line(cfg, f"context reset at Stop: {tok} tokens over {cfg.reset_tokens}")
    if not os.environ.get("JANUS_IN_TESTS"):
        subprocess.Popen([sys.executable, str(JANUS_SELF), "watch", "--reset"], cwd=cfg.root, env=clean_env({"JANUS_ROOT": str(cfg.root)}),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return None

def reset_now(cfg, wait_s=60, verify_s=90):
    """`janus watch --reset`: once the orchestrator's turn has ended, start a new session with the tool's own command and verify it:
    the registered session id and transcript must be new. Returns True when verified; False (logged why) when not, with the old
    registration restored so nothing is lost; an unsupported tool is said so, never reported as reset."""
    pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    kind = (cfg.f("session_kind").read_text().strip() if cfg.f("session_kind").exists() else "") or cfg.c.get("orchestrator", {}).get("kind", "claude")
    cmd = RESET_CMD.get(kind)
    if not cmd: log_line(cfg, f"context reset UNSUPPORTED for {kind!r}: no verified new-session command (supported: {', '.join(RESET_CMD)})"); return False
    if not pane: log_line(cfg, "context reset skipped: no orchestrator pane"); return False
    if not handoff_path(cfg).exists(): log_line(cfg, "context reset skipped: no HANDOFF.md to carry the state"); return False
    for _ in range(max(1, wait_s // 5)):
        time.sleep(5)
        if ((herdr("agent", "get", pane) or {}).get("agent") or {}).get("agent_status") != "working": break
    else: log_line(cfg, f"context reset skipped: the orchestrator kept working for {wait_s} s"); return False
    old = cfg.f("session").read_text().strip() if cfg.f("session").exists() else ""
    old_tr = cfg.f("transcript").read_text().strip() if cfg.f("transcript").exists() else ""
    atomic_write(cfg.f("session"), f"pending:{int(now())}:{pane}")   # the new session's SessionStart hook claims it (hooks.cmd_hook)
    herdr("agent", "prompt", pane, cmd)
    for _ in range(max(1, verify_s // 3)):
        time.sleep(3)
        cur = cfg.f("session").read_text().strip() if cfg.f("session").exists() else ""
        tr = cfg.f("transcript").read_text().strip() if cfg.f("transcript").exists() else ""
        if cur and not cur.startswith("pending:") and cur != old and tr != old_tr:
            herdr("agent", "prompt", pane, JANUS_MARK + wake_clear(cfg, "the session was reset"))
            log_line(cfg, f"context reset verified: new session {cur[:12]} (was {old[:12]}), {kind} {cmd}"); return True
    atomic_write(cfg.f("session"), old)
    log_line(cfg, f"context reset NOT verified: no new session registered within {verify_s} s after {cmd}; the old registration is restored"); return False
