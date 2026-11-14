"""Agent transcripts: find a unit's Claude Code or Codex transcript (here or on its host), condense it, grep or tail it.

One condenser for both formats (templates.CONDENSE); a remote transcript is read over one ssh, never copied whole."""
import json, re, shlex, sys
from pathlib import Path
from janus.board import board_dir
from janus.context import hrun, qp, sh
from janus.templates import CONDENSE
from janus.units import api_out, report_path

# One condenser for Claude Code (jsonl under ~/.claude/projects) and Codex (rollout jsonl under ~/.codex/sessions) transcripts.
# It runs where the transcript is: here, or on the agent's host as `python3 -` with this source on stdin (standard library only).

def session_of(cfg, u):
    """The agent's session id: its api.json (session_id; Codex: thread_id), else its newest transcript (a run stopped before
    it wrote its json): here ~/.claude/projects/<cwd>, on a remote host the same under that host's agent_env HOME."""
    d = api_out(cfg, u); sid = d.get("session_id") or d.get("thread_id")
    if sid: return sid
    h = cfg.hosts.get(u.get("host"), {})
    if h.get("local"):
        f = transcript_of(cfg, u); return f.stem if f else None
    rc, o, e = hrun(h, f"s={remote_slug_sh(u)}\n{h.get('agent_env', '')}\nls -t \"$HOME/.claude/projects/$s\"/*.jsonl 2>/dev/null | head -1")
    return Path(o.strip()).stem if rc == 0 and o.strip() else None

def transcript_of(cfg, u):
    """The Claude Code transcript of a local API unit: under ~/.claude/projects/<its cwd>, started after the unit, whose first
    prompt names its brief."""
    d = Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(u.get("rcwd") or u.get("cwd") or ""))
    try: fs = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError: return None
    for f in fs[:20]:
        if f.stat().st_mtime < u.get("started", 0): break
        try:
            with open(f) as fh: head = "".join(next(fh, "") for _ in range(8))
        except OSError: continue
        if any(b and (b in head or Path(b).name in head) for b in (u.get("rbrief"), u.get("brief"))): return f
    return None

def unit_sessions(cfg, u):
    """The agent session ids of a unit's runs (api.json and each run's api-<run>.json: Claude session_id, Codex thread_id)."""
    od = cfg.p("out", "out") / u["name"]; sids = []
    for f in sorted(od.glob("api*.json"), key=lambda p: p.stat().st_mtime):
        try: d = json.loads(f.read_text())
        except Exception: continue
        sid = d.get("session_id") or d.get("thread_id")
        if sid and re.fullmatch(r"[A-Za-z0-9-]{8,64}", sid): sids.append(sid)
    return list(dict.fromkeys(sids))

def read_transcript(cfg, u, grep="", tail=40):
    """(rc, condensed text) of the unit's transcript(s), read on the host that holds them."""
    h = cfg.hosts.get(u.get("host"), {"local": True}); kind = cfg.models.get(u.get("model"), {}).get("kind")
    sids = unit_sessions(cfg, u); cwd = u.get("rcwd") or u.get("cwd") or ""
    if kind == "opencode": return 3, "(opencode keeps its sessions in its own database: not readable by janus; read the agent's report)"
    codex = kind == "codex" or (kind == "api" and "codex" in json.dumps(cfg.prov.get(cfg.models.get(u.get("model"), {}).get("provider"), {}).get("cmd", "")))
    if codex:
        mode, pats = ("ids", [f"~/.codex/sessions/**/*{s}*.jsonl" for s in sids]) if sids else ("codex-cwd", ["~/.codex/sessions/**/rollout-*.jsonl"])
    elif sids: mode, pats = "ids", [f"~/.claude/projects/*/{s}.jsonl" for s in sids]
    elif h.get("local"):
        f = transcript_of(cfg, u); mode, pats = "ids", [str(f)] if f else []
        if not f: return 3, "(no transcript found)"
    else: mode, pats = "newest", ["~/.claude/projects/$s/*.jsonl"]   # a remote Claude agent with no session id yet: its newest there
    argv = [mode, grep, str(tail), str(u.get("started", 0) - 60), cwd, *pats]
    if h.get("local"): rc, o, e = sh([sys.executable, "-", *argv], inp=CONDENSE, timeout=120)
    else:   # $s: the slug of the cwd as expanded on that host (review-fixes9 #4)
        args = shlex.join(argv).replace(shlex.quote("~/.claude/projects/$s/*.jsonl"), '"$HOME/.claude/projects/$s/*.jsonl"')
        rc, o, e = hrun(h, f"s={remote_slug_sh(u) if mode == 'newest' else '-'}\n{h.get('agent_env', '')}\npython3 - {args}", inp=CONDENSE, timeout=120)
    return rc, (o or e).rstrip()

def card_fields(cfg, name):
    """The board card's fields ({state, step, ...}) of an agent, {} without a card."""
    try: return dict(m.groups() for m in re.finditer(r"^- (\w+): ?(.*)$", (board_dir(cfg) / "agents" / f"{name}.md").read_text(), re.M))
    except OSError: return {}

def remote_slug(u):
    """Claude Code's projects folder name for a remote unit's cwd when it is known here (rcwd_abs, the expanded path recorded at
    launch; or an rcwd with no $HOME/~ in it), else None."""
    p = u.get("rcwd_abs") or (u.get("rcwd") if u.get("rcwd") and not re.search(r"[$~]", u["rcwd"]) else None)
    return re.sub(r"[^A-Za-z0-9-]", "-", p) if p else None

def remote_slug_sh(u):
    """A shell word that is the slug on the host itself: the cwd expanded there (review-fixes9 #4: $HOME/... gave -HOME-...).
    Evaluate it before agent_env, which may move HOME."""
    s = remote_slug(u)
    return shlex.quote(s) if s else f"\"$(printf %s {qp(u.get('rcwd') or '')} | sed 's/[^A-Za-z0-9-]/-/g')\""

def transcript_where(cfg, u):
    """Where the unit's agent transcript is (for peers.md): a local path, or <host>:<path> on the host that ran it."""
    h = cfg.hosts.get(u.get("host"), {}); sid = api_out(cfg, u).get("session_id")
    if h.get("local"):
        f = transcript_of(cfg, u) if not sid else next(iter((Path.home() / ".claude/projects").glob(f"*/{sid}.jsonl")), None)
        return str(f) if f else "-"
    esc = remote_slug(u)
    if not esc: return f"{u['host']}: janus transcript {u['name']} (its folder name depends on that host's $HOME)"
    return f"{u['host']}:~/.claude/projects/{esc}/" + (f"{sid}.jsonl" if sid else "(newest .jsonl)")


def pointer_text(cfg, u, topic="", why=""):
    """One line that makes an agent aware of another unit: what it is doing, its report path, and the exact command to read
    its transcript by grep. The janus introduce direction and the brief's RELATED/predecessor pointers are this one text."""
    name = u.get("name") or ""; h = cfg.hosts.get(u.get("host"), {})
    what = (card_fields(cfg, name).get("step") or u.get("state") or "").strip() or "-"
    rp = str(report_path(cfg, u)) if (h.get("local") or not u.get("host")) else f"{u['host']}:{h.get('root', '')}/out/{name}/REPORT.md"
    cmd = f"janus transcript {name} --grep {shlex.quote(topic or name)}"
    return (f"{why + '. ' if why else ''}{name} (host {u.get('host') or '-'}, {u.get('model') or '-'}, {u.get('state') or '-'}): "
            f"{what[:90]}; its report {rp}; read its transcript: {cmd}")


NAME_RX = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def related_names(text):
    """The unit names on a brief's RELATED: line (one line, comma or space separated); [] without one."""
    m = re.search(r"^RELATED:\s*(.*)$", text or "", re.M)
    return [n for n in re.split(r"[,\s]+", (m.group(1) if m else "").strip()) if NAME_RX.fullmatch(n)]


