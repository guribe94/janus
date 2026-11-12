"""janus issue: log a problem met with the skill to the skill's own register.

The owner rule "log every oddity as an ISSUES row" lived only in docs; this makes it one command, so a finding an agent or the
orchestrator met is written down instead of remembered. The register is the file `[janus] issues_file` names (relative to the
project root, or absolute); each call appends one markdown table row `| <n> | <text> | <evidence> |`. The register is the
skill's, so a row carries generic names only (no project, host or person). `janus check` raises `issue:<key>` for a finding key
that has been acked `[janus] ack_issue_n` (3) or more times, and, when the register is configured, writes that row once per key
so a rule that keeps coming back is not forgotten. The acked-findings view (`janus check --acked`) shows what an ack is hiding.
"""
import re
from pathlib import Path
from janus.context import Err, say, ts, atomic_write

ISSUE_DEFAULTS = {"issues_file": None, "ack_issue_n": 3}

def iset(cfg, k): return cfg.c.get("janus", {}).get(k, ISSUE_DEFAULTS[k])

def issue_path(cfg):
    """The register file ([janus] issues_file), or None when it is not configured."""
    p = iset(cfg, "issues_file")
    if not p: return None
    p = Path(p).expanduser()
    return p if p.is_absolute() else cfg.root / p

def append_issue(cfg, text, evidence="", key=None):
    """Append one row to the register (refused when it is not configured). Idempotent by key (`[<key>]`) or by exact text: a row
    for the same key or text is never written twice. Returns (rows written, message)."""
    p = issue_path(cfg)
    if not p: raise Err("set [janus] issues_file in janus.toml (the skill's issue register) to log issues")
    old = p.read_text() if p.is_file() else ""
    if key and f"[{key}]" in old: return 0, f"issue {key}: already logged"
    if text and re.search(r"^\|[^|]*\|\s*" + re.escape(text.strip()) + r"\s*\|", old, re.M): return 0, "already logged"
    n = max((int(m.group(1)) for m in re.finditer(r"^\|\s*(\d+)\s*\|", old, re.M)), default=0) + 1
    row = f"| {n} | {text} | {evidence or '-'} |\n"
    if cfg.run.dry: say(f"+ append to {p}: {row.strip()}"); return 1, "dry run"
    if not old:
        p.parent.mkdir(parents=True, exist_ok=True); atomic_write(p, "# Issue register\n\n| # | issue | evidence and fix |\n|---|---|---|\n" + row)
    else:
        with open(p, "a") as f: f.write(row)
    return 1, f"logged issue {n} in {p}"

def cmd_issue(cfg, a):
    """janus issue "<text>" [--evidence <path|job>]: append one row to the skill's issue register. --key <finding key> makes it
    idempotent (a row for that key is written once), which is what check's issue:<key> finding points at."""
    if not (a.text or "").strip(): raise Err("janus issue \"<text>\" [--evidence <path>] [--key <finding key>]")
    ev = a.evidence or ""
    if a.key: ev = (f"[{a.key}] " + ev).strip()
    rc, msg = append_issue(cfg, a.text.strip(), ev, key=a.key)
    return 0, [msg]

def repeated_issue_findings(cfg):
    """issue:<key> for every finding key acked [janus] ack_issue_n or more times (the acks are a sign it keeps coming back, so
    it earns an issue row): name the key and `janus issue --key <key>`."""
    n0 = int(iset(cfg, "ack_issue_n")); c = cfg.jget("ack-counts.json")
    return [(f"issue:{k}", f"{k} has been acked {n} times (>= {n0}): open an issue row for it once (janus issue --key {k} \"<what "
             f"keeps happening and why>\"), so a recurring failure is tracked, not re-remembered") for k, n in sorted(c.items()) if n >= n0]

def auto_log_issues(cfg):
    """Append an issue row for each over-acked key not yet logged (once per key); a no-op when the register is not configured.
    Called by janus check as a writer, so a rule that keeps coming back is written down where the skill keeps its register."""
    if not issue_path(cfg): return
    done = cfg.jget("issued.json")
    for k, n in cfg.jget("ack-counts.json").items():
        if n < int(iset(cfg, "ack_issue_n")) or k in done: continue
        rc, _ = append_issue(cfg, f"a finding key keeps coming back ([{k}] acked {n} times): fix the root cause or make the "
                                  f"condition impossible", f"janus check; ack-counts {n}", key=k)
        if rc: done[k] = ts()
    cfg.jput("issued.json", done)

__all__ = ["ISSUE_DEFAULTS", "iset", "issue_path", "append_issue", "cmd_issue", "repeated_issue_findings", "auto_log_issues"]
