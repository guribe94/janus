"""Findings about the session memory (too long, stale, a gone source, one subject twice) and the standing rules in beads."""
import json, os, re, time
from pathlib import Path
from orchkit.context import now, sh
from orchkit.memory import memory_read, mset
from orchkit.units import JOBREF

def mem_source_gone(cfg, s):
    """True when a cited source is a path that no longer exists (a sha or a job id is not checked here)."""
    if not s or re.fullmatch(r"[0-9a-f]{7,40}", s) or JOBREF.fullmatch(s) or not ("/" in s or s.endswith((".md", ".py", ".toml", ".log", ".json"))): return False
    p = Path(os.path.expanduser(s.split(":")[0] if re.match(r"^[^/]+:\d+$", s) else s))
    return not (p if p.is_absolute() else cfg.root / p).exists()

RULE_STOP = {"a", "an", "the", "of", "on", "in", "to", "for", "and", "or", "is", "are", "be", "by", "with", "at", "from", "owner",
             "must", "always", "never", "only", "all", "every", "use", "do", "not", "no"}

def rule_subject(key):
    """A standing rule's subject: the first three words of its beads memory key that are not filler (claude-api-on-the-new-key-cap
    and claude-api-on-the-new-key-keychain share 'claude api new')."""
    w = [x for x in re.split(r"[^a-z0-9]+", key.lower()) if x and x not in RULE_STOP and not x.isdigit()]
    return "-".join(w[:3]) if len(w) >= 2 else None

def rule_findings(cfg):
    """rule-subject:<subject> when two standing rules (beads memories) are on the same subject: a newer rule replaces the older
    (bd remember <text> --key <the old key>), it never sits beside it (orch-swarm item 7)."""
    rc, o, e = sh(["bd", "memories", "--json"], cwd=cfg.beads)
    try: mem = json.loads(o) if rc == 0 and o.strip() else {}
    except Exception: return []
    if not isinstance(mem, dict): return []
    by = {}
    for k in mem: by.setdefault(rule_subject(k), []).append(k)
    return [(f"rule-subject:{sj}", f"{len(ks)} standing rules on one subject ({', '.join(sorted(ks)[:4])}): keep the current one under one key "
             f"(bd remember \"<rule>\" --key {sorted(ks)[0]}) and bd forget the others") for sj, ks in by.items() if sj and len(ks) > 1]

def memory_findings(cfg):
    es = memory_read(cfg); F = []
    if len(es) > mset(cfg, "cap_lines"):
        F.append(("memory-long", f"MEMORY.md holds {len(es)} entries, over [memory] cap_lines {mset(cfg, 'cap_lines')}: orch forget what no longer guides the agents"))
    cut = now() - mset(cfg, "review_days") * 86400
    seen = {}
    for i, (sec, d, k, t, s) in enumerate(es, 1):
        if mem_source_gone(cfg, s): F.append((f"memory-source:{i}", f"memory entry {i} cites {s}, which is gone: check it, then orch forget {i} or remember it again with a live source"))
        try:
            if time.mktime(time.strptime(d, "%Y-%m-%d")) < cut: F.append((f"memory-review:{i}", f"memory entry {i} is from {d} (over {mset(cfg, 'review_days')} days): still true? orch memory --keep {i} (re-dates it), or orch forget {i}"))
        except ValueError: pass
        if k: seen.setdefault(k, []).append(i)
    F += [(f"memory-subject:{k}", f"memory entries {', '.join(map(str, v))} are on the same subject [{k}]: keep one (orch forget the other)") for k, v in seen.items() if len(v) > 1]
    return F
