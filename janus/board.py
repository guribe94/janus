"""The message board wrappers: one card schema, one reader (section 7, Step 2).

board_script/board_dir/board, board_rows/board_questions, board_findings, board_files/board_alerts. A project may still
override paths.board (board.sh stays a callable path). The board lives in .janus/board by default."""
from janus.context import Err, JANUS_SELF, sh, now, herdr, atomic_write, JANUS_MARK
import hashlib, os, re, shlex, sys, time
from pathlib import Path

def board_script(cfg):
    """The message board script: paths.board (a project's own), else the board.sh shipped next to janus.py (janus-swarm item 6)."""
    return cfg.p("board") if cfg.paths.get("board") else JANUS_SELF.parent / "board.sh"

def board_shipped(cfg):
    """(name, text) of the shipped board sent into a remote unit's <root>/bin: the shell path every launcher names, and the
    Python module it execs next to it. A project's own paths.board script is shipped by itself (no shim)."""
    return [("board.sh", board_script(cfg).read_text()), ("msgboard.py", (Path(__file__).resolve().parent / "msgboard.py").read_text())]

def board_dir(cfg): return cfg.p("board_dir", "board")

def board(cfg, *args, env=None, timeout=25):
    """Run the board script with BOARD_DIR set (the shipped board keeps its files there; a project's own script may ignore it).
    The shipped board's files (.master, .hosts, .notify) exist from the first call: posts never land in an outbox here, and a
    remote unit's rule names <root>/bin/board.sh (not this machine's path)."""
    if not cfg.paths.get("board") and not cfg.run.dry: board_files(cfg)   # .master, and .hosts before a remote unit's rule is written
    from janus.project import project_id   # a card janus creates carries the project's id (cross-project safety)
    return sh([str(board_script(cfg)), *args], env={"BOARD_DIR": str(board_dir(cfg)), "BOARD_PROJECT": project_id(cfg), **(env or {})}, timeout=timeout)

def board_rows(cfg):
    """Every open card on the board: {name: {"state", "silent", "gone", "text"}} (audit H8: all cards, not only janus's units)."""
    rc, o, e = board(cfg, "summary", env={"BOARD_SILENT_MIN": str(cfg.silent // 60)})
    rows = {}
    for l in (o.splitlines()[1:] if rc == 0 else []):
        f = l.split()
        if len(f) < 3 or l[:1] in (" ", "=", "\t"): continue   # the shipped board's group headings and its needs-answer lines
        rows[f[0]] = {"state": f[2], "silent": bool(re.search(r"\bSILENT \d+m", l)), "gone": "GONE" in l,
                      "text": re.sub(r"\s+", " ", l.split(None, 4)[-1] if len(f) > 4 else "")[:140]}
    return rows

def board_questions(cfg):
    """Open questions [(id, agent, asked, text)] from the shipped board (board.sh questions); None when the board script has no
    question ids (a project's own script: its question cards are judged by state)."""
    rc, o, e = board(cfg, "questions")
    if rc: return None
    return [tuple((l.split("\t") + ["", "", ""])[:4]) for l in o.splitlines() if l.strip()]

def stale_board_findings(cfg):
    """board:stale:<path>: a project's own board script (paths.board, or a leftover bin/board.sh) with no `answer` subcommand, a
    pre-janus copy that prints its help instead of answering and misleads whoever runs it; janus answer q<N> <text> is the route."""
    F = []
    for p in {board_script(cfg), cfg.root / "bin" / "board.sh"}:
        try: txt = p.read_text()
        except OSError: continue
        if p != JANUS_SELF.parent / "board.sh" and "msgboard" not in txt and "answer" not in txt:
            F.append((f"board:stale:{p}", f"{p} is an old board script with no answer subcommand: do not use it; answer a question with janus answer q<N> <text>"
                      + " and delete it (and paths.board in janus.toml if it names it: janus then uses its shipped board)"))
    return F

def cmd_answer(cfg, a):
    """janus answer q<N> <text>: answer one board question (a direction to its agent); works from any directory and any tool."""
    rc, o, e = board(cfg, "answer", a.id, a.answer)
    if rc: raise Err(f"answer {a.id}: {(e or o).strip()[:160]}")
    return 0, [f"answered {a.id}"]

def board_findings(cfg, names):
    """question:<id> (stays until answered), board:stale:<path>."""
    F = stale_board_findings(cfg)
    for q, ag, at, tx in board_questions(cfg) or []:
        try: age = int((now() - time.mktime(time.strptime(at, "%Y-%m-%d %H:%M"))) / 60)
        except ValueError: age = 0
        F.append((f"question:{q}", f"{ag}{'' if ag in names else ' (not a unit)'} asks [{q}] ({age} min): {tx[:160]}; read "
                  f"janus memory first; then janus answer {q} <text>"))
    return F

def board_files(cfg):
    bd = board_dir(cfg); bd.mkdir(parents=True, exist_ok=True)
    me = next((n for n, h in cfg.hosts.items() if h.get("local")), "local")
    if not (bd / ".master").exists(): (bd / ".master").write_text(me + "\n")
    hosts = "".join(f"{n} {h['ssh']} {h['root']}\n" for n, h in cfg.hosts.items() if not h.get("local") and h.get("ssh") and h.get("root"))
    if not (bd / ".hosts").exists() or (bd / ".hosts").read_text() != hosts: atomic_write(bd / ".hosts", hosts)
    # with the writer's PATH (the watcher's): the agent whose post runs it may not have herdr on its PATH
    nt = (f"cd {shlex.quote(str(cfg.root))} && PATH={shlex.quote(os.environ.get('PATH', ''))} JANUS_ROOT={shlex.quote(str(cfg.root))} "
          f"{shlex.quote(sys.executable)} {shlex.quote(str(JANUS_SELF))} watch --alert\n")
    if not (bd / ".notify").exists() or (bd / ".notify").read_text() != nt: atomic_write(bd / ".notify", nt)

def board_alerts(cfg, log):
    """Prompt the orchestrator at once on a new board question or block (janus-swarm item 6): each is told once."""
    qs = board_questions(cfg)
    items = [(f"q:{q}", f"QUESTION [{q}] from {ag}: {tx[:200]}") for q, ag, at, tx in (qs or [])]
    items += [(f"b:{c}:{hashlib.md5(r['text'].encode()).hexdigest()[:8]}", f"BLOCKED {c}: {r['text'][:200]}") for c, r in board_rows(cfg).items()
              if r["state"] == "blocked" or (qs is None and r["state"] == "question")]
    seen = cfg.jget("alerted.json"); new = [(k, t) for k, t in items if k not in seen]
    if not new: return None
    pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    msg = (JANUS_MARK + "Board needs you now: " + " | ".join(t for _, t in new) + ". Read janus memory first; "
           "answer with janus answer <id> <text> (a block: board.sh direct <agent> <text>); janus remember what every agent should know.")
    if not pane or herdr("agent", "prompt", pane, msg) is None: log(f"board alert not delivered (no orchestrator pane): {msg[:300]}"); return None
    log(f"board alert: {msg[:600]}"); cfg.jput("alerted.json", {**{k: v for k, v in seen.items() if now() - v < 7 * 86400}, **{k: now() for k, _ in new}})
    return msg

__all__ = ["board_script", "board_shipped", "board_dir", "board", "board_rows", "board_questions", "board_findings", "cmd_answer", "board_files", "board_alerts"]
