"""The session memory (.orch/MEMORY.md): read, write, the [memory] settings, and the learned-post marks.

One small, dated, sourced, single-writer file that heads every brief (section 9: keep)."""
import json, re
from orchkit.context import atomic_write, now, ts

# .orch/MEMORY.md is the session's curated memory: Decisions / Facts / Pitfalls, one dated line per entry. Only the orchestrator
# writes it (orch remember / forget); every agent's brief starts with it (brief_header) and the orchestrator's SessionStart hook
# injects it. An entry may carry a subject ([key]): a newer entry on the same subject replaces the older one, never sits beside it.

MEM_SECTIONS = {"decision": "Decisions", "fact": "Facts", "pitfall": "Pitfalls"}

MEMORY_DEFAULTS = {"cap_lines": 30, "review_days": 14, "agent_docs": ["AGENTS.md"]}

MEM_RX = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (?:\[([^\]]+)\] )?(.*?)(?: \(source: (.+)\))?$")

def mset(cfg, k): return cfg.c.get("memory", {}).get(k, MEMORY_DEFAULTS[k])

def memory_path(cfg): return cfg.f("MEMORY.md")

def memory_read(cfg):
    """[(section, date, key or None, text, source or None)] in file order."""
    try: lines = memory_path(cfg).read_text().splitlines()
    except OSError: return []
    sec, out = None, []
    for l in lines:
        if l.startswith("## "): sec = l[3:].strip(); continue
        m = MEM_RX.match(l)
        if m and sec: out.append((sec, m.group(1), m.group(2), m.group(3), m.group(4)))
    return out

def mem_line(e):
    sec, d, k, t, s = e
    return f"- {d} " + (f"[{k}] " if k else "") + t + (f" (source: {s})" if s else "")

def memory_write(cfg, entries):
    L = ["# Session memory", "(curated by the orchestrator with orch remember / orch forget; one dated line per entry)"]
    for sec in MEM_SECTIONS.values():
        L += ["", f"## {sec}"] + [mem_line(e) for e in entries if e[0] == sec]
    atomic_write(memory_path(cfg), "\n".join(L) + "\n")

def memory_text(cfg):
    """MEMORY.md for a brief or the SessionStart context, or "" when it holds no entry."""
    return memory_path(cfg).read_text().strip() if memory_read(cfg) else ""

def memory_history(cfg, e, why):
    with cfg.f("memory-history.md").open("a") as f: f.write(f"{ts()} {why} ({e[0]}): {mem_line(e)[2:]}\n")

def learned_mark(cfg, lid, how):
    """Record that learned post <agent>:<n> was remembered or rejected (check then stops listing it)."""
    lid = lid.removeprefix("learned:"); d = cfg.jget("learned.json"); d[lid] = {"how": how, "t": int(now())}
    if not cfg.run.dry: cfg.f("learned.json").write_text(json.dumps(d))
