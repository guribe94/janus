"""The backlog: checklist items, who does them (actors), their beads issues and the units that cover them; ready work.

Queries only: janus backlog and janus fill (actions/backlog.py) change things."""
import re
from pathlib import Path
from janus.beads import Beads
from janus.context import Err, slug

# The backlog lives in beads: `janus backlog import` turns every unfinished item of the project's status checklist into a beads
# issue labelled backlog and status:<its status>, keyed by external ref backlog:<item id> (idempotent: a re-import updates
# status labels, closes items that became done, reopens ones that came back). check then raises "idle with backlog" when a host
# has room while backlog items have no open unit, and "finished, not landing" when a finished unit has no gate, or its gates
# passed and it is not on the landing branch.

LOG_RX = re.compile(r"^(?:janus|orch):\s*(?:decisions?|log)\b", re.I)   # the toolkit's own decision/log bead, not work (item 15)
LOG_LABELS = {"janus-log", "orch-log"}   # finding 8: only the toolkit's own log label, not any bead whose label merely ends -log

ITEM_RX = re.compile(r"^ITEM:\s*(\S+)", re.M)   # a brief's explicit item mark (actions/backlog.BRIEF_KEYS reads the same line)

def ready_work(cfg):
    """Ready work items in beads: not units (launched work), not janus's decision log (the `janus-log`/`orch-log` label, or a
    `janus:`/`orch: decisions`/`log` title -- only those; a bead whose title or label merely contains log is real work,
    finding 8), not owner instructions (audit H7), not intents (janus intent), and not work another actor does (label
    actor:<owner|hardware|...>): only what an agent could be launched on (skill-fix52)."""
    skip = {"unit", "instruction", "backlog", "intent"}   # backlog items have their own finding (idle with backlog)
    out = []
    for i in Beads(cfg).run("ready"):
        labels = set(i.get("labels") or []); title = str(i.get("title", ""))
        if skip & labels or labels & LOG_LABELS or any(l.startswith("actor:") and l != "actor:agent" for l in labels): continue
        if title.startswith(("unit:", "janus:", "instr:", "intent:")) or LOG_RX.match(title): continue
        out.append(i)
    return out

def backlog_cfg(cfg):
    b = dict(cfg.c.get("backlog", {})); b.setdefault("file", cfg.paths.get("checklist"))
    return b

def backlog_items(cfg, actors=None):
    """[(id, title, status, done, ready)] from the checklist ([backlog] file, default paths.checklist): markdown table rows (the
    header row names the columns: id_col '#', title_col 'item', status_col 'status') and '- [ ] item' / '- [x] item' lines.
    done: status starts with one of [backlog] done (default DONE). ready: not done, and the status starts with one of
    [backlog] ready (default: every status), so items waiting on the owner can be left out of 'idle with backlog'."""
    b = backlog_cfg(cfg)
    if not b.get("file"): return None
    try: text = (cfg.root / b["file"]).read_text()
    except OSError: return None
    idc, tc, sc, ac = (b.get(k, d).lower() for k, d in (("id_col", "#"), ("title_col", "item"), ("status_col", "status"), ("actor_col", "actor")))
    dn = [x.upper() for x in b.get("done", ["DONE"])]; rd = [x.upper() for x in b.get("ready", [])]
    items, hdr, seen = [], None, set()
    def add(i, t, st):
        if not t or i in seen: return
        seen.add(i); d = any(st.upper().startswith(x) for x in dn)
        items.append((i, t, st, d, not d and (not rd or any(st.upper().startswith(x) for x in rd))))
    for l in text.splitlines():
        s = l.strip()
        if s.startswith("#"): hdr = None; continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-*:?", c) for c in cells): continue
            low = [c.lower() for c in cells]
            if tc in low and sc in low: hdr = low; continue
            if hdr and len(cells) >= len(hdr):
                r = dict(zip(hdr, cells)); t = r.get(tc, "")
                add(r.get(idc) or slug(t), t, r.get(sc, ""))
                if actors is not None and r.get(ac, "").strip(): actors[r.get(idc) or slug(t)] = r[ac].strip().lower()
            continue
        m = re.match(r"^[-*] \[( |x|X)\] (.+)$", s)
        if m: add(slug(m.group(2)), m.group(2), "DONE" if m.group(1) != " " else "OPEN")
    return items

def item_actors(cfg):
    """{item id: actor} (row 54): who does the item. The checklist's actor column ([backlog] actor_col, default "actor"), else
    the beads label actor:<x> that `janus backlog set <id> actor=<x>` sets (the label wins). Unset: agent. Only agent items
    count as idle backlog or are filled; owner, hardware and any other actor never are."""
    a = {}; backlog_items(cfg, a)
    try:
        for r, i in backlog_issues(cfg).items():
            x = next((l[6:] for l in (i.get("labels") or []) if l.startswith("actor:")), None)
            if x: a[r[8:]] = x
    except Err: pass
    return a

def backlog_issues(cfg):
    r = Beads(cfg).run("list", "--label", "backlog", "--all", "-n", "0")
    r = r.get("issues", r) if isinstance(r, dict) else r
    return {i.get("external_ref"): i for i in r if str(i.get("external_ref", "")).startswith("backlog:")}

def unit_items(cfg, units):
    """Backlog item ids that open units cover: their --item, or an ITEM: <id> line in the head of their brief (the mark fill
    reads too). Only these explicit ids: a brief that merely mentions an id (every word of its first lines used to count)
    hid that ready item from the idle-backlog findings and from fill (skill-fix52)."""
    out = set()
    for u in units:
        if u.get("item"): out.add(u["item"])
        try: out.update(x.rstrip(".,;") for x in ITEM_RX.findall("\n".join(Path(u["brief"]).read_text().splitlines()[:12])))
        except (OSError, KeyError): pass
    return out
