"""The project memory (.janus/MEMORY.md): the active user instructions and the durable decisions, facts and pitfalls.

One file, one dated line per entry, `- <date> [key] <text> (source: <where>)`. An entry with a key replaces the older entry of the
same key; nothing expires and nothing is truncated: a binding instruction stays until someone replaces or forgets it. The file is
loaded in full before the handoff at every start, reset and resume, and heads every worker brief. Tool, model, provider and
auth route restrictions are NOT written here: they live once in janus.toml ([providers] enabled, [routing]) where janus
enforces them."""
import re
from janus.context import atomic_write, memory_path, ts

MEM_SECTIONS = {"instruction": "Instructions", "decision": "Decisions", "fact": "Facts", "pitfall": "Pitfalls"}

MEM_RX = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (?:\[([^\]]+)\] )?(.*?)(?: \(source: (.+)\))?$")

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
    L = ["# Project memory", "(janus remember / janus forget; one dated line per entry; a keyed entry replaces the older one of its key)"]
    for sec in MEM_SECTIONS.values():
        L += ["", f"## {sec}"] + [mem_line(e) for e in entries if e[0] == sec]
    atomic_write(memory_path(cfg), "\n".join(L) + "\n")

def memory_text(cfg):
    """MEMORY.md in full, or "" when it holds no entry."""
    return memory_path(cfg).read_text().strip() if memory_read(cfg) else ""

def memory_history(cfg, e, why):
    with cfg.f("memory-history.md").open("a") as f: f.write(f"{ts()} {why} ({e[0]}): {mem_line(e)[2:]}\n")
