"""The project memory (.janus/MEMORY.md): the active user instructions and the durable decisions, facts and pitfalls.

One file, one dated line per entry, `- <date> [key] <text> (source: <where>)`. An entry with a key replaces the older entry of the
same key; nothing expires and nothing is truncated: a binding instruction stays until someone replaces or forgets it. The file is
loaded in full before the handoff at every start, reset and resume; a worker's brief carries its instructions and pitfalls
(agent_memory). Tool, model, provider and
auth route restrictions are NOT written here: they live once in janus.toml ([providers] enabled, [routing]) where janus
enforces them."""
import re
from janus.context import atomic_write, handoff_path, memory_path, ts

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

AGENT_SECTIONS = ("Instructions", "Pitfalls")   # what binds an agent's work; decisions and facts are read on need

def agent_memory_path(cfg, h): return str(memory_path(cfg)) if h.get("local") else f"{h['root']}/MEMORY.md"

def agent_memory(cfg, h):
    """The memory an agent on host h is given, as lines: the instructions and pitfalls in full (binding, never cut), then where the
    decisions and facts are (the file here, the copy janus ships to a remote host's root). An agent got all of it at launch and
    again at every resume: 94 KB in one project, most of a small model's context and of a free route's request budget."""
    es = memory_read(cfg); L = []
    for sec in AGENT_SECTIONS:
        sel = [mem_line(e) for e in es if e[0] == sec]
        if sel: L += [f"{sec}:", *sel]
    rest = sum(1 for e in es if e[0] not in AGENT_SECTIONS)
    if rest: L.append(f"Decisions and facts ({rest} entries, not repeated here): {agent_memory_path(cfg, h)}; read the ones your work touches.")
    return L

TEXT_MAX_KB = {"handoff": 16, "memory": 48}   # [project] handoff_max_kb / memory_max_kb

def long_texts(cfg):
    """[(key, text)]: handoff:long and memory:long, for a HANDOFF.md or MEMORY.md over its size ([project] handoff_max_kb, default
    16; memory_max_kb, default 48). Both are loaded whole at every session start, and nothing measured them: one project's handoff
    grew to 197 KB (appended at each checkpoint, void workarounds still read as current) and its memory to 94 KB, most of a
    session's context before its first command. Nothing is cut here: the orchestrator curates."""
    F = []; pr = cfg.c.get("project", {})
    for k, p in (("handoff", handoff_path(cfg)), ("memory", memory_path(cfg))):
        try: kb = p.stat().st_size / 1024
        except OSError: continue
        mx = pr.get(f"{k}_max_kb", TEXT_MAX_KB[k])
        if kb <= mx: continue
        F.append((f"{k}:long", f"{p.name} is {kb:.0f} KB, over {mx} KB ([project] {k}_max_kb), and is loaded whole at every session start: "
                  + (f"rewrite it in place to the current objective, decisions, blockers and next actions; move older checkpoints to {p.parent / 'HANDOFF-archive.md'}"
                     if k == "handoff" else
                     f"it holds {len(memory_read(cfg))} entries: janus memory lists them; janus forget each one a newer entry supersedes or that no longer binds")))
    return F

def memory_history(cfg, e, why):
    with cfg.f("memory-history.md").open("a") as f: f.write(f"{ts()} {why} ({e[0]}): {mem_line(e)[2:]}\n")
