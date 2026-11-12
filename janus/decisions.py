"""Decision records and the owner's decision queue (docs/DECISIONS.md, docs/KNOWLEDGE.md).

A decision record is the durable memory of one important decision: `.janus/decisions/NNNN-title.md`, one immutable
file per decision, numbered in order, filled from `skill/templates/decision.md`, beside a generated
`.janus/decisions/INDEX.md` (one line each, newest first). A record is never edited after it is accepted; a reversal is
a new record that `--supersedes` the old one and marks the old one's Status line `superseded by NNNN` — the only line
that ever changes. `janus decisions --grep` searches them.

The owner's decision queue is `.janus/owner-queue.md`: questions waiting on the owner. `janus ask` records one and runs
the configured `[owner] notify` command (none by default); `janus answer <n>` closes it (and, with --decide, writes the
decision record the answer settles). check lists an open one as `owner-question:<n>`, and raises `decision-missing:<id>`
for an owner instruction classified as a decision (beads label `decision`) with no record that links it.

This module imports context, beads, board and memory only (the bottom of the package), so check builds on it."""
import re, time
from janus.beads import Beads
from janus.board import board_dir
from janus.context import Err, JANUS_SELF, atomic_write, refuse_agent, say, sh, slug, ts

DECNUM = re.compile(r"^(\d{4})-.*\.md$")

def decisions_dir(cfg): return cfg.f("decisions")

def template_text():
    """The shipped record template; the structure every record is filled from."""
    p = JANUS_SELF.parent / "skill/templates/decision.md"
    try: return p.read_text()
    except OSError: raise Err(f"the decision template {p} is missing (a release ships skill/templates/decision.md)")

def decision_files(cfg):
    """[(number, path)] of the records on disk, in number order (INDEX.md is not a record)."""
    d = decisions_dir(cfg)
    out = []
    for p in d.glob("*.md") if d.is_dir() else []:
        m = DECNUM.match(p.name)
        if m: out.append((int(m.group(1)), p))
    return sorted(out)

def next_num(cfg): return max((n for n, _ in decision_files(cfg)), default=0) + 1

def record_by_num(cfg, num):
    """The record numbered num as a dict, or None."""
    for n, p in decision_files(cfg):
        if n == num: return read_record(p)
    return None

def read_record(path):
    """A record parsed for listing, searching and superseding: its head fields and its whole text."""
    try: text = path.read_text()
    except OSError: return None
    def field(name):
        m = re.search(rf"^- {name}: (.*)$", text, re.M); return m.group(1).strip() if m else None
    m = DECNUM.match(path.name)
    num = int(m.group(1)) if m else 0
    title = next((l[2:].split("—", 1)[-1].strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
    return {"num": num, "title": title, "date": field("Date"), "status": field("Status") or "accepted",
            "by": field("Decided by"), "supersedes": field("Supersedes"), "instruction": field("Instruction"),
            "path": path, "text": text}

def decisions(cfg):
    """Every record as a dict, oldest first."""
    return [r for n, p in decision_files(cfg) if (r := read_record(p))]

def options_text(options):
    if not options: return "1. (none recorded)."
    return "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))

def fill_record(tmpl, rec):
    """The record text: the template's title line, its '- Key: ...' head and its '## Section' bodies, filled from rec.
    The sections and their order come from the template, so a change to it is a change to every record's shape."""
    head, secs, cur = [], [], None
    for line in tmpl.splitlines():
        if line.startswith("# "): continue                       # the title line, rebuilt below
        m = re.match(r"^- ([A-Za-z][A-Za-z ]*):", line) if cur is None else None
        if m: head.append(m.group(1)); continue
        if line.startswith("## "): cur = [line[3:].strip(), []]; secs.append(cur); continue
        if cur is not None: cur[1].append(line)
    vals = {"Date": rec["date"], "Status": "accepted", "Decided by": rec["by"], "Supersedes": rec.get("supersedes") or "none"}
    L = [f"# {rec['num']:04d} — {rec['title']}", ""]
    for k in head: L.append(f"- {k}: {vals.get(k, '')}")
    if rec.get("instruction"): L.append(f"- Instruction: {rec['instruction']}")
    L.append("")
    body = {"Context": rec.get("context") or "", "Options considered": options_text(rec.get("options")),
            "Decision": rec.get("decision") or "", "Why": rec.get("why") or "", "Consequences": rec.get("consequences") or ""}
    for name, _ in secs: L += [f"## {name}", "", body.get(name, "").rstrip(), ""]
    return "\n".join(L).rstrip() + "\n"

def record_path(cfg, num, title): return decisions_dir(cfg) / f"{num:04d}-{slug(title) or 'decision'}.md"

def write_decision(cfg, rec):
    """Write one record (immutable) and regenerate the index; a dry run prints it and changes nothing. Returns (num, path)."""
    num = rec.get("num") or next_num(cfg)
    path = record_path(cfg, num, rec["title"])
    text = fill_record(template_text(), {**rec, "num": num})
    if cfg.run.dry: say(f"+ write {path}"); say(text.rstrip()); return num, path
    atomic_write(path, text); write_index(cfg)
    return num, path

def supersede(cfg, num, new):
    """Mark record num 'superseded by new' — its Status line, the only line a record ever changes."""
    old = record_by_num(cfg, num)
    if not old: raise Err(f"no decision {num:04d} to supersede")
    text = re.sub(r"^- Status: .*$", f"- Status: superseded by {new:04d}", old["text"], count=1, flags=re.M)
    if cfg.run.dry: say(f"+ mark {old['path']} superseded by {new:04d}"); return
    atomic_write(old["path"], text)

def index_text(cfg):
    rows = sorted(decisions(cfg), key=lambda r: r["num"], reverse=True)
    return (f"# Decisions index ({ts()}; regenerated by janus decide/decisions; newest first)\n"
            + "".join(f"- {r['num']:04d} | {r['title']} | {r['date']} | {r['status']}\n" for r in rows))

def write_index(cfg):
    """Regenerate .janus/decisions/INDEX.md and its copy in the board folder (so the board sync carries it to every host)."""
    text = index_text(cfg)
    if not cfg.run.ro and not cfg.run.dry:
        atomic_write(decisions_dir(cfg) / "INDEX.md", text)
        if board_dir(cfg).is_dir(): atomic_write(board_dir(cfg) / "decisions.md", text)
    return text

def session_text(cfg):
    """The titles only, for the SessionStart context and the brief header: the map of what is already settled. A superseded
    record is shown as such (never as current) and the list is capped to the newest 40 (F12)."""
    rows = sorted(decisions(cfg), key=lambda r: r["num"], reverse=True)
    if not rows: return ""
    shown = rows[:40]
    L = [f"- {r['num']:04d} {r['title']}" + (f" ({r['status']})" if r["status"] and r["status"] != "accepted" else "") for r in shown]
    if len(rows) > len(shown): L.append(f"(+{len(rows) - len(shown)} older: janus decisions)")
    return ("Decisions (read the record before deciding anything it touches):\n" + "\n".join(L))

def link_instruction(cfg, iid):
    """An owner instruction's record of settlement: its beads id linked from the record and the 'decision' label on it."""
    try: Beads(cfg).run("label", "add", iid, "decision")
    except Err as e: say(f"decision written, but its instruction {iid} was not labelled: {e}")

def cmd_decide(cfg, a):
    refuse_agent("janus decide")
    title = re.sub(r"\s+", " ", a.title).strip()
    if not title: raise Err("a decision needs a title")
    if not (a.decision or "").strip(): raise Err("a decision needs --decision (what was decided)")
    sup = None
    if a.supersedes is not None:
        old = record_by_num(cfg, a.supersedes)
        if not old: raise Err(f"no decision {a.supersedes:04d} to supersede")
        sup = f"{old['num']:04d}-{slug(old['title']) or 'decision'}"
    num = next_num(cfg)
    rec = {"num": num, "title": title, "date": time.strftime("%Y-%m-%d"), "by": a.by, "supersedes": sup,
           "context": a.context or "", "options": a.options or [], "decision": a.decision, "why": a.why or "",
           "consequences": a.consequences or "", "instruction": a.instruction}
    write_decision(cfg, rec)
    if a.supersedes is not None: supersede(cfg, a.supersedes, num)
    if a.instruction: link_instruction(cfg, a.instruction)
    if cfg.run.dry: return 0, []
    return 0, [f"decision {num:04d} ({a.by}) written in {decisions_dir(cfg)}" + (f", superseding {a.supersedes:04d}" if a.supersedes else "")]

def cmd_decisions(cfg, a):
    write_index(cfg)
    rows = sorted(decisions(cfg), key=lambda r: r["num"], reverse=True)
    if a.grep:
        rx = re.compile(a.grep, re.I)
        out = []
        for r in rows:
            if rx.search(r["text"]):
                line = next((l for l in r["text"].splitlines() if rx.search(l)), r["title"])
                out.append(f"{r['num']:04d} {r['title']}: {re.sub(r'^- ', '', line).strip()[:160]}")
        return 0, out or [f"(no decision matches {a.grep!r})"]
    return 0, [f"{r['num']:04d} {r['date']} [{r['status']}] {r['title']}" for r in rows] or ["(no decision records)"]

# ---- the owner's decision queue ---------------------------------------------------------------------------------

Q_RX = re.compile(r"^## (\d+) — (.*?)\s*\[(open|answered(?: (\d{4}-\d{2}-\d{2}))?)\]\s*$")

def queue_path(cfg): return cfg.f("owner-queue.md")

def owner_queue(cfg):
    """The queued questions, oldest first: {"n", "q", "status", "asked", "options", "recommend", "answer"}."""
    try: lines = queue_path(cfg).read_text().splitlines()
    except OSError: return []
    out, cur = [], None
    for line in lines:
        m = Q_RX.match(line)
        if m:
            cur = {"n": int(m.group(1)), "q": m.group(2), "status": "open" if m.group(3) == "open" else "answered",
                   "answered": m.group(4), "asked": None, "options": [], "recommend": None, "answer": None}
            out.append(cur); continue
        if cur is None: continue
        for key, pre in (("asked", "- Asked: "), ("recommend", "- Recommend: "), ("answer", "- Answer: ")):
            if line.startswith(pre): cur[key] = line[len(pre):].strip() or None
        if line.startswith("- Options: "): cur["options"] = [x.strip() for x in line[11:].split("|") if x.strip()]
    return out

def render_queue(items):
    L = ["# Owner decision queue",
         "Questions waiting on the owner (janus ask \"<question>\"); one answer closes one (janus answer <n> \"<answer>\").", ""]
    for it in items:
        L.append(f"## {it['n']} — {it['q']} [{'open' if it['status'] == 'open' else 'answered ' + (it.get('answered') or '')}]")
        if it.get("asked"): L.append(f"- Asked: {it['asked']}")
        if it.get("options"): L.append("- Options: " + " | ".join(it["options"]))
        if it.get("recommend"): L.append(f"- Recommend: {it['recommend']}")
        L.append(f"- Answer: {it.get('answer') or ''}")
        L.append("")
    return "\n".join(L).rstrip() + "\n"

def notify_text(it):
    t = f"janus ask [{it['n']}]: {it['q']}"
    if it.get("options"): t += "\noptions: " + " | ".join(it["options"])
    if it.get("recommend"): t += f"\nrecommend: {it['recommend']}"
    return t

def owner_notify(cfg, it):
    """Run the configured [owner] notify command with the question on stdin (none by default); its output is ignored."""
    cmd = (cfg.c.get("owner") or {}).get("notify") or ""
    if not cmd: return None
    text = notify_text(it)
    if cfg.run.dry: say(f"+ notify: {cmd}  (< {text.splitlines()[0]})"); return None
    rc, o, e = sh(["sh", "-c", cmd], inp=text, timeout=20)
    if rc: say(f"owner notify exited {rc}: {(e or o).strip()[:160]}")
    return rc

def cmd_ask(cfg, a):
    refuse_agent("janus ask")
    q = re.sub(r"\s+", " ", a.question).strip()
    if not q: raise Err("nothing to ask")
    items = owner_queue(cfg)
    it = {"n": max((x["n"] for x in items), default=0) + 1, "q": q, "status": "open", "answered": None,
          "asked": time.strftime("%Y-%m-%d"), "options": a.options or [], "recommend": a.recommend, "answer": None}
    if not cfg.run.dry: atomic_write(queue_path(cfg), render_queue(items + [it]))
    else: say(f"+ {queue_path(cfg)}: {it['n']} — {q}")
    owner_notify(cfg, it)
    if cfg.run.dry: return 0, []
    return 0, [f"asked the owner [{it['n']}]: {q}" + (f" (recommend: {a.recommend})" if a.recommend else "")]

def cmd_answer(cfg, a):
    refuse_agent("janus answer")
    items = owner_queue(cfg)
    it = next((x for x in items if x["n"] == a.id), None)
    if not it: raise Err(f"no question {a.id} in {queue_path(cfg)}")
    if it["status"] != "open": raise Err(f"question {a.id} is already answered")
    ans = re.sub(r"\s+", " ", a.answer).strip()
    if not ans: raise Err("an answer needs text")
    done = {**it, "status": "answered", "answered": time.strftime("%Y-%m-%d"), "answer": ans}
    if cfg.run.dry:
        say(f"+ {queue_path(cfg)}: {a.id} answered: {ans}")
        if a.decide: say(f"+ decision record of the answer (by owner)")
        return 0, []
    atomic_write(queue_path(cfg), render_queue([done if x["n"] == a.id else x for x in items]))
    out = [f"answered [{a.id}]: {ans}"]
    if a.decide:
        num, path = write_decision(cfg, {
            "title": f"Owner answered: {it['q']}", "date": time.strftime("%Y-%m-%d"), "by": "owner", "supersedes": None,
            "context": "Open question for the owner: " + it["q"] + (f" (options: {' | '.join(it['options'])})" if it["options"] else ""),
            "options": it["options"], "decision": ans, "why": "The owner's answer to the queued question.", "consequences": ""})
        out.append(f"decision {num:04d} written: {path}")
    return 0, out

def decision_findings(cfg):
    """decision-missing:<id> (an owner instruction classified as a decision with no record) and owner-question:<n> (open)."""
    F = []
    try: ins = Beads(cfg).issues("instruction")
    except Err: ins = []
    linked = {r["instruction"] for r in decisions(cfg) if r.get("instruction")}
    for i in ins:
        if "decision" in (i.get("labels") or []) and i.get("id") not in linked:
            F.append((f"decision-missing:{i['id']}", f"owner instruction {i['id']} is classified as a decision with no record: "
                      f"janus decide \"<title>\" --by owner --decision \"<what>\" --instruction {i['id']}"))
    for q in owner_queue(cfg):
        if q["status"] == "open":
            F.append((f"owner-question:{q['n']}", f"question {q['n']} waits on the owner: {q['q'][:140]}"
                      + (f" (recommend: {q['recommend']})" if q.get("recommend") else "") + f"; janus answer {q['n']} \"<answer>\""))
    return F

__all__ = ["decisions_dir", "template_text", "decision_files", "next_num", "record_by_num", "read_record", "decisions",
           "fill_record", "write_decision", "supersede", "index_text", "write_index", "session_text", "cmd_decide",
           "cmd_decisions", "owner_queue", "render_queue", "queue_path", "cmd_ask", "cmd_answer", "decision_findings"]
