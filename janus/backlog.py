"""The backlog: checklist items, who does them (actors), their beads issues and the units that cover them; ready work.

Queries only: janus backlog and janus fill (actions/backlog.py) change things."""
import re
from pathlib import Path
from janus.beads import Beads
from janus.context import Err, slug
from janus.probe import live_units
from janus.units import verdict

# The backlog lives in beads: `janus backlog import` turns every unfinished item of the project's status checklist into a beads
# issue labelled backlog and status:<its status>, keyed by external ref backlog:<item id> (idempotent: a re-import updates
# status labels, closes items that became done, reopens ones that came back). check then raises "idle with backlog" when a host
# has room while backlog items have no open unit, and "finished, not landing" when a finished unit has no gate, or its gates
# passed and it is not on the landing branch.

LOG_RX = re.compile(r"^(?:janus|orch):\s*(?:decisions?|log)\b", re.I)   # the toolkit's own decision/log bead, not work (item 15)
LOG_LABELS = {"janus-log", "orch-log"}   # finding 8: only the toolkit's own log label, not any bead whose label merely ends -log

ITEM_RX = re.compile(r"^ITEM:\s*(\S+)", re.M)   # a brief's explicit item mark (actions/backlog.BRIEF_KEYS reads the same line)

def norm_item(x):
    """The one form an item id is compared in: a checklist id, the id in a bead's external ref (backlog:<id>), a unit's --item and a
    brief's ITEM: line name the same item whatever their case or a trailing full stop (an ITEM: x1 brief is for checklist row X1)."""
    return str(x or "").strip().rstrip(".,;").lower()

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

def item_actors(cfg, iss=None):
    """{item id (norm_item): actor} (row 54): who does the item. The checklist's actor column ([backlog] actor_col, default "actor"),
    else the beads label actor:<x> that `janus backlog set <id> actor=<x>` sets (the label wins). Unset: agent. Only agent items
    count as idle backlog or are filled; owner, hardware and any other actor never are. iss: the backlog issues already read."""
    c = {}; backlog_items(cfg, c); a = {norm_item(k): v for k, v in c.items()}
    try:
        for r, i in (backlog_issues(cfg) if iss is None else iss).items():
            x = next((l[6:] for l in (i.get("labels") or []) if l.startswith("actor:")), None)
            if x: a[norm_item(r[8:])] = x
    except Err: pass
    return a

HOLD_RX = re.compile(r"^HOLD:[ \t]*(.*)$")   # a draft's active hold line; a released one reads RELEASED-HOLD: (janus backlog set <id> hold=off)

def draft_holds(text):
    """[(line index, reason)] of the active HOLD: lines in the head (first 12 lines) of a draft's text: the one parse item_holds
    and the release (actions/backlog.release_hold) share."""
    return [(n, m.group(1).strip() or "held") for n, l in enumerate(text.splitlines()[:12]) if (m := HOLD_RX.match(l))]

def hold_drafts(cfg):
    """The drafts a HOLD: line can hold an item from (briefs/drafts/*.md, sorted): each holds the item norm_item(its stem)."""
    return sorted((cfg.p("briefs", "briefs") / "drafts").glob("*.md"))

def item_holds(cfg, iss=None, src=None):
    """{item id: reason} of the items held on purpose (rows 29/99: a deliberately parked item or draft kept idle-backlog:nobrief
    coming back, acked again and again). Held: the beads label hold:<reason> that `janus backlog set <id> hold=<reason>` sets
    (hold=off clears it and the drafts' HOLD: lines), or a `HOLD: <reason>` line in the head of its draft (briefs/drafts/<id>.md).
    A held item is not idle backlog and is not filled: it neither counts nor nags until the hold is taken off; janus backlog list
    shows it. Keys: norm_item. src: a dict, filled {item id: [where each hold is]} (the label wins the reason; every source is named)."""
    hs = {}; src = {} if src is None else src
    try:
        for r, i in (backlog_issues(cfg) if iss is None else iss).items():
            x = next((l[5:] for l in (i.get("labels") or []) if l.startswith("hold:")), None)
            if x: hs[norm_item(r[8:])] = x; src.setdefault(norm_item(r[8:]), []).append(f"beads label hold:{x} on {i.get('id')}")
    except Err: pass
    try: drafts = hold_drafts(cfg)
    except OSError: drafts = []
    for f in drafts:
        try: m = draft_holds(f.read_text(errors="replace"))
        except OSError: continue
        if m: hs.setdefault(norm_item(f.stem), m[0][1]); src.setdefault(norm_item(f.stem), []).append(f"HOLD: line in {f.name} (drafts)")
    return hs

def backlog_issues(cfg):
    r = Beads(cfg).run("list", "--label", "backlog", "--all", "-n", "0")
    r = r.get("issues", r) if isinstance(r, dict) else r
    return {i.get("external_ref"): i for i in r if str(i.get("external_ref", "")).startswith("backlog:")}

def bead_items(iss):
    """{bead id (norm_item): item id (norm_item)} of the backlog issues, by their external ref backlog:<id> only: a unit recorded
    with the bead id (an older --item orch-7) names the same checklist item as one recorded with X1."""
    return {norm_item(i.get("id")): norm_item(r[8:]) for r, i in (iss or {}).items() if i.get("id")}

def unit_items(cfg, units, iss=None):
    """Backlog item ids that open units cover: their --item, or an ITEM: <id> line in the head of their brief (the mark fill
    reads too). Only these explicit ids: a brief that merely mentions an id (every word of its first lines used to count)
    hid that ready item from the idle-backlog findings and from fill (skill-fix52). Ids in norm_item form; a bead id is
    resolved to its item through the backlog issues' external refs (iss: those already read; None: read now, none when beads
    cannot be read). Nothing is guessed from prose."""
    out = set()
    for u in units:
        if u.get("item"): out.add(norm_item(u["item"]))
        try: out.update(norm_item(x) for x in ITEM_RX.findall("\n".join(Path(u["brief"]).read_text().splitlines()[:12])))
        except (OSError, KeyError, TypeError): pass
    if out and iss is None:   # read only when there is an id to resolve
        try: iss = backlog_issues(cfg)
        except Err: iss = {}
    ids = bead_items(iss)
    return {ids.get(x, x) for x in out}

def holding_units(cfg, units):
    """The open units that hold their item: those not on a stranded host (probe.live_units), and a stranded host's units that
    finished (a final VERDICT here) or were already relaunched once: relaunching those would redo done work over and over.
    Closing such a unit is the orchestrator's call; capacity still counts only live_units."""
    live = live_units(cfg, units); ids = {id(u) for u in live}
    return live + [u for u in units if id(u) not in ids and (u.get("relaunched") or verdict(cfg, u))]

def covered_items(cfg, units, iss=None):
    """The items open units cover for selection: unit_items of holding_units. A unit on a host down past [capacity]
    down_after_min, or marked gone, no longer holds its item unless it finished or was relaunched: fill relaunches it elsewhere."""
    return unit_items(cfg, holding_units(cfg, units), iss)

def stranded_cover(cfg, units, iss=None):
    """{item (norm_item form): the open units on stranded hosts that cover it, the work before its reviews, newest started first}
    for the items no holding unit covers: what fill relaunches, each from the first of them (its brief, report and backup.patch);
    a review of work stranded with it is relaunched only when nothing else covered the item."""
    hold = holding_units(cfg, units); ids = {id(u) for u in hold}; cov = unit_items(cfg, hold, iss); out = {}
    for u in sorted((u for u in units if id(u) not in ids), key=lambda u: (u.get("kind") == "review", -(u.get("started") or 0))):
        for k in unit_items(cfg, [u], iss):
            if k not in cov: out.setdefault(k, []).append(u)
    return out

STRANDED = {   # status -> (what is true, what to do): stranded_status's words, shown by the host finding and the monitor
    "relaunched": ("relaunched", ""), "lead": ("janus fill relaunches its item on a host that answers", ""),
    "finished": ("finished there", "land or close it by its verdict"), "item-closed": ("its item is closed", "close it"),
    "behind": ("another unit covers its item", "close it, or relaunch it by hand: janus launch <new> --resume-of <unit>"),
    "no-item": ("no backlog item of its own", "relaunch it by hand (janus launch <new> --resume-of <unit>) or close it")}

def stranded_status(cfg, units, iss=None):
    """{unit name: one of STRANDED} for each open unit on a host janus gave up on (probe.live_units leaves it out): the one rule
    fill, the host finding and the monitor read. A unit that finished or was relaunched holds its item (holding_units); fill
    relaunches an open item from the first unit stranded_cover names for it."""
    live = {id(u) for u in live_units(cfg, units)}
    lead = {id(v[0]) for v in stranded_cover(cfg, units, iss).values()}
    shut = {norm_item(r[8:]) for r, i in (iss or {}).items() if i.get("status") == "closed"}
    out = {}
    for u in units:
        if id(u) in live: continue
        its = unit_items(cfg, [u], iss)
        out[u["name"]] = ("relaunched" if u.get("relaunched") else "finished" if verdict(cfg, u) else "item-closed" if its and its <= shut
                          else "lead" if id(u) in lead else "behind" if its else "no-item")
    return out

def eligible_items(iss, ready_ids, cov, act, hold, blocked=None, held=None):
    """The one selection fill, check and the monitor share: [(priority, order, item id, title)] of the backlog issues (by their
    external ref backlog:<id> only, never their title) that are open, labelled backlog-ready, of an agent, not held and not
    covered by an open unit. ready_ids: the ids bd ready lists (no open blocker); an item it does not list is dependency-blocked
    (appended to blocked) and not eligible. None: membership unknown (not checked). held: the items left out only by their hold
    (open, backlog-ready, an agent's, not covered) are appended to it, so fill and status can say why they wait."""
    out = []
    for n, (r, i) in enumerate(iss.items()):
        item = r[8:]; k = norm_item(item)
        if i.get("status") not in ("open", "in_progress") or "backlog-ready" not in (i.get("labels") or []): continue
        if k in cov or act.get(k, "agent") != "agent": continue
        if k in hold:
            if held is not None: held.append(item)
            continue
        if ready_ids is not None and i.get("id") not in ready_ids:
            if blocked is not None: blocked.append(item)
            continue
        out.append((int(i.get("priority", 2) if i.get("priority") is not None else 2), n, item, str(i.get("title", ""))[9:]))
    return out

def backlog_ready_ids(cfg):
    """The ids of the backlog issues bd ready lists (open, no open blocker: a dependency-blocked or deferred item is not there)."""
    r = Beads(cfg).run("ready", "--label", "backlog", "-n", "0")
    return {i.get("id") for i in (r.get("issues", r) if isinstance(r, dict) else r)}

def item_eligible(cfg, item, units):
    """None when item is still eligible for an automatic launch now (eligible_items over beads read again: ready in bd ready,
    backlog-ready, of an agent, not held, not covered by units), else why not. Err when beads cannot be read (fail closed)."""
    try: iss = backlog_issues(cfg); rdy = backlog_ready_ids(cfg)
    except Err as e: raise Err(f"refused: beads cannot be read to recheck item {item} ({e}): not launched")
    blocked = []; ok = eligible_items(iss, rdy, covered_items(cfg, units, iss), item_actors(cfg, iss), item_holds(cfg, iss), blocked)
    if any(norm_item(x[2]) == norm_item(item) for x in ok): return None
    return "blocked by a beads dependency (bd ready does not list it)" if any(norm_item(x) == norm_item(item) for x in blocked) else \
        "not ready, held, not an agent's, closed or covered now (janus backlog list)"

def checklist_totals(cfg, units, iss=None):
    """The checklist's own totals (cmd_report, status and the monitor share them): {"total", "done", "left": [(id, title, status)],
    "actors": {actor: left items}, "covered": left items an open unit covers}; None without a checklist. Its DONE fraction is the
    share of checklist rows whose status is done, not a weighted measure of the project."""
    items = backlog_items(cfg)
    if items is None: return None
    act = {}; backlog_items(cfg, act); act = {norm_item(k): v for k, v in act.items()}
    cov = covered_items(cfg, units or [], iss); left = [x for x in items if not x[3]]; actors = {}
    for i, *_ in left: a = act.get(norm_item(i), "agent"); actors[a] = actors.get(a, 0) + 1
    return {"total": len(items), "done": len(items) - len(left), "left": [(i, t, st) for i, t, st, d, rd in left], "actors": actors,
            "covered": sum(1 for i, *_ in left if norm_item(i) in cov), "cov": cov, "act": act}

def held_text(held, n=6):
    """A bounded summary of the items left out only by a hold (held: [(item, reason, [where the hold is], unlaunched usable brief's
    unit name or None)]): the first n, each with its reason, its sources and whether a brief waits for it; janus backlog list shows all."""
    s = "; ".join(f"{i} ({r[:60]}; {', '.join(src)[:120] or '?'}; " + (f"unlaunched brief {b} waits" if b else "no usable brief") + ")" for i, r, src, b in held[:n])
    return f"{len(held)} held on purpose, not launched: {s}" + ("; ..." if len(held) > n else "") + " (janus backlog list: all; janus backlog set <id> hold=off releases one)"

def checklist_line(tot, missing=None, held=None):
    """One line of checklist_totals: the DONE fraction (named as the checklist's, not weighted completeness), what is left by actor,
    how many an open unit covers (open, not necessarily running), the ready items without a usable brief, by id, and the held
    ones (held_text)."""
    if tot is None: return "Checklist: none ([backlog] file or paths.checklist is not set)"
    n, d = tot["total"], tot["done"]
    return (f"Checklist: {d} of {n} checklist items DONE ({100 * d // max(n, 1)} %, the checklist's DONE fraction, not weighted project completeness); "
            f"{n - d} left" + (": " + ", ".join(f"{v} {k}" for k, v in sorted(tot["actors"].items())) if tot["actors"] else "")
            + f"; {tot['covered']} in open units (open, not necessarily running)"
            + ("" if missing is None else f"; {len(missing)} ready without a usable brief" + (": " + ", ".join(missing[:12]) + ("..." if len(missing) > 12 else "") if missing else ""))
            + (f"; {held_text(held, 4)}" if held else ""))
