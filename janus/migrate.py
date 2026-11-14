"""janus migrate SOURCE [--to TARGET] [--apply]: the one-time move of a project to the project-owned .janus under TARGET (default:
SOURCE itself). SOURCE is the old orchestration root (the folder holding janus.toml); TARGET is the workspace root that will own
.janus (it need not be a Git root, and need not be where the old root was: an old root nested inside it is the case this exists
for). There is no permanent second layout: afterwards find_root knows only TARGET/.janus/janus.toml.

Dry run unless --apply. KEPT: the config (name, an explicit project id, every other setting, relative paths rebased to TARGET),
unit and task identities (they live in the bd database, which is never written), every card, message, brief and result (renamed,
never copied or rewritten), the state folder with the session registration, the Git metadata of the unit worktrees (git worktree
move), and the registry (~/.config/janus/projects names the new root). RETAINED AUTOMATICALLY: the user's active instructions
(open bd issues labelled instruction, and bd memories) are read (read only) into MEMORY.md's Instructions section, keyed by their
bd id, so nothing has to be typed again. NOT MOVED: application source, worktrees that no unit uses, and the bd database (it stays
where it is, [project] beads points at it; moving it is a database operation for your approval). TEMPORARY: each moved folder
leaves a link at its old path so records that name it still resolve; .janus/migration/bd-path-updates.tsv lists the record fields
to update (nothing writes them here); remove the links after that is approved. REFUSED, with the reason: a janus job queued or
running, an agent or the orchestrator alive, any board outbox with a post in it, a host or queue that cannot say (unknown is not
none), an unreadable unit list, a target that already has a config or collides with what moves."""
import os, re, shutil
from pathlib import Path
from janus.beads import Beads
from janus.context import CONF, Cfg, Err, atomic_write, hrun, qp, say, session_alive, sh, slug
from janus.memory import memory_read, memory_write
from janus.probe import probe
from janus.project import project_id
from janus.units import herdr_agents, unit_state

FOLDERS = (("briefs", "briefs"), ("out", "out"), ("board_dir", "board"), (None, "board-outbox"))   # ([paths] key, default folder)
BUSY = ("Queued", "Running", "Paused", "Stashed", "Locked")

def folder(cfg, key, dflt):
    """The old absolute place of a janus-owned folder (the [paths] entry, else the default beside the config)."""
    v = cfg.paths.get(key) if key else None
    return (cfg.root / (v or dflt)).resolve()

def refusals(cfg, units, hs):
    """Every reason the project must not move now (empty: safe), from the one set of probes (hs) taken for it."""
    why, tag = [], f"@{project_id(cfg)}"
    for n, h in cfg.hosts.items():
        p = hs[n]
        if not p["ok"]: why.append(f"host {n} cannot say what runs there ({p['err']}): status unknown"); continue
        if p.get("pueue") is None: why.append(f"host {n}: its queue did not answer: status unknown"); continue
        for t in ((p.get("pueue") or {}).get("tasks") or {}).values():
            st = t.get("status"); k = st if isinstance(st, str) else next(iter(st or {}), "")
            if k in BUSY and str(t.get("label") or "").endswith(tag): why.append(f"{n}:{t.get('id')} ({t.get('label')}) is {k.lower()}: its scripts and paths are the old ones")
        if not h.get("local"):   # a post that waited under a minute reads as 0 in the probe: look at the files
            rc, o, e = hrun(h, f"cd {qp(h.get('root', '/tmp'))} 2>/dev/null && ls -A board-outbox 2>/dev/null | head -3; true", timeout=25)
            if rc: why.append(f"host {n}: its board outbox cannot be read ({(e or o).strip()[:60]}): status unknown")
            elif o.strip(): why.append(f"host {n}: board posts wait in its outbox ({o.split()[0]}...)")
    ag = herdr_agents(cfg)
    for u in units:
        st = unit_state(u, hs, ag)
        if st in ("working", "idle", "blocked", "queued", "unknown"): why.append(f"unit {u['name']} is {st}")
    if session_alive(cfg): why.append("the registered orchestrator session is alive")
    for d in {cfg.state, folder(cfg, "board_dir", "board").parent}:
        ob = d / "board-outbox"
        if ob.is_dir() and any(ob.iterdir()): why.append(f"{ob} holds posts that were not synced")
    return why

def instructions(cfg):
    """[(key, text, source)] of the user's active instructions, read only: open bd issues labelled instruction (the full prompt is
    in the description), and bd memories ('key: text' lines)."""
    out = []
    try: issues = Beads(cfg).issues("instruction", "open,in_progress")
    except Err: issues = []
    for i in issues:
        t = re.sub(r"\s+", " ", (i.get("description") or i.get("title") or "")).strip()
        t = t[:600] + ("..." if len(t) > 600 else "")
        if t: out.append((slug(f"bd-{i['id']}"), t, f"bd:{i['id']}"))
    rc, o, e = sh(["bd", "--readonly", "memories"], cwd=cfg.beads, timeout=30)
    for l in (o.splitlines() if rc == 0 else []):
        m = re.match(r"^\s*(?:[-*]\s*)?\**([A-Za-z0-9._-]{2,60})\**\s*[:=]\s*(\S.*)$", l)
        if m: out.append((slug(m.group(1)), m.group(2).strip()[:600], f"bd memory {m.group(1)}"))
    return out

def rebase(cfg, dst, mapping, value):
    """A config path value (relative to the old root) as one relative to dst, after the moved folders took their new places."""
    if not isinstance(value, str) or value.startswith(("/", "~", "$")): return value
    a = (cfg.root / value).resolve()
    for old, new in mapping.items():
        if a == old or old in a.parents: a = new / a.relative_to(old); break
    return os.path.relpath(a, dst)

def new_config(cfg, dst, mapping, id_):
    """The config text for dst/.janus: [project] state dropped, an explicit id, the moved folders' [paths] entries dropped (those
    places are the defaults now), every other relative path (beads, ledger, checklist, backlog file) rebased to dst."""
    text = (cfg.root / "janus.toml").read_text(); sec, out = "", []
    has_id = bool(re.search(r"(?m)^\s*id\s*=", text)); gone = {"briefs", "out", "worktrees", "board_dir"}
    for l in text.splitlines():
        m = re.match(r"^\s*\[([^\]]+)\]", l)
        if m: sec = m.group(1)
        kv = re.match(r'^(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)"([^"]*)"(.*)$', l)
        if sec == "project" and re.match(r"^\s*state\s*=", l): continue
        if sec == "paths" and kv and kv.group(2) in gone: continue
        if kv and ((sec == "project" and kv.group(2) == "beads") or (sec == "paths" and kv.group(2) not in gone) or kv.group(2) == "ledger" or (sec == "backlog" and kv.group(2) == "file")):
            l = f'{kv.group(1)}{kv.group(2)}{kv.group(3)}"{rebase(cfg, dst, mapping, kv.group(4))}"{kv.group(5)}'
        out.append(l)
        if m and sec == "project" and not has_id: out.append(f'id = "{id_}"   # the project identity: kept as it was (it was the name)')
    return "\n".join(out) + "\n"

def plan(cfg, dst):
    """(folder moves [(old, new)], worktree moves [(old, new, unit)], worktrees left where they are [path])."""
    moves = [(o, dst / ".janus" / n) for k, n in FOLDERS for o in [folder(cfg, k, n)] if o.is_dir() and not o.is_symlink()]
    wt = folder(cfg, "worktrees", "worktrees"); wmoves, used = [], []
    try: units = Beads(cfg).units()
    except Err: units = []
    for u in units:
        c = Path(u.get("cwd") or "/")
        if wt in c.resolve().parents and (c / ".git").is_file(): wmoves.append((c.resolve(), dst / ".janus" / "worktrees" / c.name, u["name"])); used.append(c.resolve())
    left = [str(d) for d in sorted(wt.iterdir()) if d.resolve() not in used] if wt.is_dir() else []
    return moves, wmoves, left

def path_updates(units, mapping):
    """[(unit, field, old, new)]: the unit-record fields that name a moved path (for review; never written here)."""
    rows = []
    for u in units:
        for k, v in u.items():
            if isinstance(v, str):
                for old, new in mapping.items():
                    if v == str(old) or v.startswith(str(old) + "/"): rows.append((u["name"], k, v, str(new) + v[len(str(old)):])); break
    return rows

def migrate(source=None, to=None, apply=False):
    from janus.context import old_layout
    src = Path(source).resolve() if source else old_layout()
    if src is None or not (src / "janus.toml").is_file():
        if src and (src / CONF).is_file(): say(f"{src} is already migrated ({CONF})"); return 0
        say("janus migrate: no old-layout project (a janus.toml in the folder given, or here)"); return 2
    dst = Path(to).resolve() if to else src
    if not dst.is_dir(): say(f"janus migrate: target {dst} is not a folder"); return 2
    if (dst / CONF).exists(): say(f"{dst}: {CONF} exists already: refusing to merge"); return 1
    cfg = Cfg(src, conf=src / "janus.toml")
    try: units = Beads(cfg).units()
    except Err as e: say(f"REFUSED: the unit list cannot be read ({e}): status unknown"); return 1
    hs = {n: probe(n, h, cfg=cfg) for n, h in cfg.hosts.items()}
    why = refusals(cfg, units, hs); moves, wmoves, left = plan(cfg, dst); id_ = project_id(cfg)
    mapping = {**{o: n for o, n in moves}, **{o: n for o, n, _ in wmoves}}
    mapping[folder(cfg, "worktrees", "worktrees")] = dst / ".janus" / "worktrees"
    state_old = cfg.state; ents = sorted(state_old.iterdir()) if state_old.is_dir() and state_old != dst / ".janus" else []
    clash = [str(n) for _, n in [*[(o, n) for o, n in moves], *[(o, n) for o, n, _ in wmoves]] if n.exists()] + [str(dst / ".janus" / e.name) for e in ents if (dst / ".janus" / e.name).exists()]
    if clash: why.append("the target already holds: " + ", ".join(clash[:4]))
    ins = instructions(cfg); upd = path_updates(units, mapping)
    say(f"migrate {src} -> {dst} ({'apply' if apply else 'dry run'}): project {cfg.name}, id {id_}, {len(units)} unit(s)")
    say(f"  config: {src}/janus.toml -> {dst / CONF} (state setting dropped, id written, relative paths rebased)")
    if ents: say(f"  state: {len(ents)} entr(ies) of {state_old} -> {dst / '.janus'} (sessions, handoff, memory, runs, logs)")
    for o, n in moves: say(f"  {o} -> {n}  (temporary link left at the old path)")
    for o, n, u in wmoves: say(f"  worktree of unit {u}: git worktree move {o} -> {n}  (temporary link left)")
    if left: say(f"  left where they are (no unit uses them): {len(left)} worktree folder(s) under {folder(cfg, 'worktrees', 'worktrees')}")
    say(f"  instructions kept automatically: {len(ins)} into MEMORY.md (read only from bd); {len(upd)} unit-record field(s) to update later: .janus/migration/bd-path-updates.tsv")
    say(f"  registry: ~/.config/janus/projects names {dst}" if dst != src else "  registry: unchanged (the root does not change)")
    say(f"  not moved: the bd database ({cfg.beads / '.beads'}): [project] beads keeps pointing at it; moving it needs your approval")
    if why: say("REFUSED:"); [say(f"  - {w}") for w in why]; return 1
    if not apply: say(f"dry run: nothing changed; add --apply to do it"); return 0
    mig = dst / ".janus" / "migration"; done = []
    try:
        (dst / ".janus").mkdir(exist_ok=True); mig.mkdir(parents=True, exist_ok=True)
        for e in ents: shutil.move(str(e), str(dst / ".janus" / e.name)); done.append((dst / ".janus" / e.name, e))
        for o, n in moves:
            os.rename(o, n); done.append((n, o)); os.symlink(n, o); done.append((None, o))
        (dst / ".janus" / "worktrees").mkdir(exist_ok=True)
        for o, n, u in wmoves:
            rc, so, se = sh(["git", "-C", str(o), "worktree", "move", str(o), str(n)], timeout=120)
            if rc: raise Err(f"git worktree move {o}: {(se or so).strip()[-120:]}")
            done.append((n, o)); sh(["git", "-C", str(n), "worktree", "repair"], timeout=60); os.symlink(n, o); done.append((None, o))
        shutil.copy2(src / "janus.toml", mig / "janus.toml.orig")
        atomic_write(dst / CONF, new_config(cfg, dst, mapping, id_)); done.append((None, dst / CONF))
        tidy_handoff(dst, mig); keep_instructions(dst, ins)
        atomic_write(mig / "bd-path-updates.tsv", "unit\tfield\told\tnew\n" + "".join("\t".join(r) + "\n" for r in upd))
        if dst != src: move_registry(src, dst)
        (src / "janus.toml").unlink()   # last: until here the old layout stands and undo restores it
    except Exception as e:
        undo(done); raise Err(f"migration failed and was undone: {e}")
    say(f"migrated: {dst / CONF}; originals in {mig}")
    say("old root is inactive (no janus.toml). Pending your approval: update the bd records listed in .janus/migration/bd-path-updates.tsv, move the bd database if wanted, then remove the temporary links: "
        + " ".join(f"rm {o}" for o, _ in [*moves, *[(o, n) for o, n, _ in wmoves]]))
    return 0

def keep_instructions(dst, ins):
    """MEMORY.md Instructions: each active instruction as one keyed entry (existing entries of the same key are kept as they are)."""
    class C:   # just enough of a Cfg for memory_read/write: the state folder
        def f(self, n): return dst / ".janus" / n
    c = C(); es = memory_read(c); have = {e[2] for e in es if e[2]}; day = __import__("time").strftime("%Y-%m-%d")
    new = [("Instructions", day, k, t, s) for k, t, s in ins if k not in have]
    if new: memory_write(c, es + new)

def move_registry(src, dst):
    """The registry names the project's root: replace the old root by the new one (a line only; nothing else in the file)."""
    p = Path.home() / ".config/janus/projects"
    if not p.is_file(): return
    ls = p.read_text().split(); out = [str(dst) if Path(x).resolve() == src else x for x in ls]
    if str(dst) not in out: out.append(str(dst))
    atomic_write(p, "\n".join(dict.fromkeys(out)) + "\n")

def undo(done):
    for new, old in reversed(done):
        try:
            if new is None: Path(old).unlink(missing_ok=True)
            else:
                if Path(old).is_symlink(): Path(old).unlink()
                Path(old).parent.mkdir(parents=True, exist_ok=True)
                if (Path(new) / ".git").is_file(): sh(["git", "-C", str(new), "worktree", "move", str(new), str(old)])
                else: shutil.move(str(new), str(old))
        except OSError: pass

def tidy_handoff(dst, mig):
    """Drop the old generated Live-state block from HANDOFF.md (nothing generates it now, so it would only go stale); the notes
    stay under the curated headings' place. The original is kept in the migration folder."""
    p = dst / ".janus" / "HANDOFF.md"
    if not p.is_file(): return
    t = p.read_text(); shutil.copy2(p, mig / "HANDOFF.md.orig")
    if "<!-- LIVE:BEGIN" in t and "<!-- LIVE:END -->" in t:
        atomic_write(p, "# HANDOFF\n" + t.split("<!-- LIVE:END -->", 1)[1].strip() + "\n")

__all__ = ["migrate"]
