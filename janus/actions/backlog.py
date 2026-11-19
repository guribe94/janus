"""janus backlog (import, list, set actor) and janus fill (launch ready backlog units up to capacity)."""
import argparse, os, re, stat
from pathlib import Path
from janus.actions.launch import brief_plan, choose, cmd_launch, pick_model, place
from janus.backlog import backlog_cfg, backlog_issues, backlog_items, backlog_ready_ids, covered_items, draft_holds, eligible_items, held_text, hold_drafts, item_actors, item_holds, norm_item, stranded_cover, unit_items
from janus.beads import Beads
from janus.branch import landing_branch, verify_published
from janus.budgets import budget_for, finish_reserve
from janus.context import Err, atomic_write, say, sh, slug, ts
from janus.ledger import committed, reserved
from janus.probe import host_liveness, probe_all, stranded_hosts
from janus.units import all_units, herdr_agents, report_path, report_text, unit_any, unit_state

def cmd_backlog(cfg, a):
    if a.action == "set":   # janus backlog set <id> actor=<x>
        if not a.args or len(a.args) != 2 or not a.args[1].startswith(("actor=", "hold=", "status=")): raise Err("usage: janus backlog set <item id> actor=<agent|owner|hardware|...> | hold=<reason> (hold=off: no longer held) | status=DONE --unit <unit>")
        if a.args[1].startswith("status="): return accept_item(cfg, a.args[0], a.args[1][7:], getattr(a, "unit", None), getattr(a, "commit", None))
        iss = backlog_issues(cfg); x = issue_of(iss, a.args[0])
        if not x: raise Err(f"no backlog item {a.args[0]} in beads (janus backlog import first)")
        if a.args[1].startswith("hold="):   # rows 29/99: an item parked on purpose is not idle backlog until the hold is taken off
            v = slug(a.args[1][5:]); v = "" if v in ("off", "none", "no") else v
            if not v: return release_hold(cfg, a.args[0], iss)   # every source: the beads labels, then the drafts' HOLD: lines
            labels = [l for l in (x.get("labels") or []) if not l.startswith("hold:")] + [f"hold:{v}"]
            if cfg.run.dry: say(f"+ bd update {x['id']} --set-labels {','.join(labels)}"); return
            Beads(cfg).run("update", x["id"], "--set-labels", ",".join(labels)); say(f"{a.args[0]}: held ({v})"); return
        v = slug(a.args[1][6:]) or "agent"; labels = [l for l in (x.get("labels") or []) if not l.startswith("actor:")] + ([f"actor:{v}"] if v != "agent" else [])
        if cfg.run.dry: say(f"+ bd update {x['id']} --set-labels {','.join(labels)}"); return
        Beads(cfg).run("update", x["id"], "--set-labels", ",".join(labels)); say(f"{a.args[0]}: actor {v}"); return
    items = backlog_items(cfg)
    if items is None: raise Err("no checklist: set [backlog] file (or paths.checklist) in janus.toml")
    if a.action == "list":
        act = item_actors(cfg); src = {}; hold = item_holds(cfg, src=src); idx = brief_index(cfg) if hold else []; ever = all_units(cfg) if hold else {}
        for i, t, st, d, rd in items:
            k = norm_item(i); b = (usable_brief(cfg, i, idx, ever) or (None, None))[1] if k in hold and not d else None
            say(f"{'done ' if d else 'held ' if k in hold else 'ready' if rd else 'wait '} {i:8} {act.get(k, 'agent'):8} {st[:24]:24} {t[:90]}"
                + (f"\n      held: {hold[k]} ({'; '.join(src.get(k, []))}); " + (f"unlaunched brief {b} waits (not launched while held)" if b else "no usable brief")
                   if k in hold and not d else ""))
        return
    bd = Beads(cfg); ex = backlog_issues(cfg); n = {"created": 0, "updated": 0, "closed": 0, "reopened": 0, "same": 0}
    for i, t, st, d, rd in items:
        ref = f"backlog:{i}"; labels = ["backlog", "status:" + (slug(st) or "none")] + (["backlog-ready"] if rd else [])
        x = ex.get(ref); title = f"backlog: {i} {t}"[:150]
        if cfg.run.dry: say(f"+ {ref}: {'close' if d and x else 'skip' if d else 'update' if x else 'create'} ({st[:30]})"); continue
        if d:
            if x and x.get("status") != "closed": bd.run("close", x["id"], "--reason", f"checklist: {st[:60]}"); n["closed"] += 1
            continue
        if not x:
            r = bd.run("create", title, "-l", ",".join(labels), "-d", f"From {backlog_cfg(cfg)['file']}: {t}\nstatus: {st}", "--external-ref", ref)
            n["created"] += 1; continue
        if x.get("status") == "closed": bd.run("reopen", x["id"]); n["reopened"] += 1
        labels += [l for l in (x.get("labels") or []) if l.split(":")[0] in ("actor", "hold")]   # set by janus backlog set: kept
        if sorted(x.get("labels") or []) != sorted(labels) or x.get("title") != title:
            bd.run("update", x["id"], "--title", title, "--set-labels", ",".join(labels)); n["updated"] += 1
        else: n["same"] += 1
    if not cfg.run.dry: cfg.touch("backlog_imported")
    say(f"backlog: {len(items)} checklist items; " + ", ".join(f"{v} {k}" for k, v in n.items()))

BRIEF_KEYS = re.compile(r"^(ITEM|KIND|TIER|TEST-HOST|PRIORITY|BUDGET):\s*(\S+)", re.M)

def brief_index(cfg):
    """[(path, unit name, {ITEM, KIND, TIER, TEST-HOST, PRIORITY}, head text)] of every brief, newest first."""
    out = []
    for f in sorted(cfg.p("briefs", "briefs").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(f, errors="replace") as fh: head = "".join(next(fh, "") for _ in range(12))
        except OSError: continue
        out.append((f, f.stem, {k: v.rstrip(".,;") for k, v in BRIEF_KEYS.findall(head)}, head))
    return out

ready_ids = backlog_ready_ids   # the one query (janus/backlog.py), shared with launch's recheck

def out_used(cfg, name):
    """A brief's unit name that cannot be launched again: an out/<name> folder with anything in it (a run's report or output). An
    empty folder is no run: an older janus made it before a refusal, and the unit was then skipped forever."""
    od = cfg.p("out", "out") / name
    try: return od.is_dir() and any(od.iterdir()) or (od.exists() and not od.is_dir())
    except OSError: return True

def brief_dir(cfg):
    """The briefs folder as the orchestrator writes it ([paths] briefs, else .janus/briefs), from the project root."""
    p = cfg.p("briefs", "briefs")
    return str(p.relative_to(cfg.root)) if p.is_relative_to(cfg.root) else str(p)

def fill_candidates(cfg, units, launch=False, missing=None, blocked=None, held=None):
    """Ready backlog items with a brief and no unit, highest priority first: [(priority, order, item, brief path, unit name,
    kind, tier)]. Items: backlog.eligible_items over the beads backlog (open, backlog-ready, listed by bd ready, of an agent, not
    held, not covered by an open unit; priority from beads). launch (an unattended launch): beads must answer and hold the
    checklist, else Err; never the unchecked checklist. Otherwise, without beads, the checklist's ready rows (its order), for a
    plan only. A brief is for an item when its ITEM: line names it (an explicit mark: a brief that merely mentions an item may be
    old or a review); a brief whose unit name was ever launched, or whose out/<name> folder holds output, is spent. The newest
    brief wins. missing: the eligible items with no usable brief, by id (fill and check name them; they were left out in silence).
    blocked: eligible items bd ready does not list (they wait on a dependency). held: the items left out only by a hold, as
    [(item, reason, [where the hold is], unlaunched usable brief's unit name or None)]: never candidates, named so a held item with
    a brief is not mistaken for a missing one."""
    src, hl = {}, []; iss = {}
    try:
        iss = backlog_issues(cfg); cov = covered_items(cfg, units, iss)
        if not iss: raise Err("the checklist is not in beads (janus backlog import)")
        try: rdy = ready_ids(cfg)
        except Err:
            if launch: raise
            rdy = None   # a plan or a finding only: membership unknown
        hold = item_holds(cfg, iss, src); items = eligible_items(iss, rdy, cov, item_actors(cfg, iss), hold, blocked, hl)
    except Err as e:
        if launch: raise Err(f"beads cannot say what is ready ({e}): nothing is launched from the unchecked checklist")
        src.clear(); act = item_actors(cfg, {}); hold = item_holds(cfg, {}, src); cov = covered_items(cfg, units, {}); iss = {}
        rows = [(n, i, t) for n, (i, t, st, d, rd) in enumerate(backlog_items(cfg) or []) if rd and norm_item(i) not in cov and act.get(norm_item(i), "agent") == "agent"]
        items = [(2, n, i, t) for n, i, t in rows if norm_item(i) not in hold]; hl = [i for n, i, t in rows if norm_item(i) in hold]
    ever = all_units(cfg); out = []; idx = brief_index(cfg); taken = set(); strand = stranded_cover(cfg, units, iss)
    if held is not None:
        held.extend((i, hold[norm_item(i)], src.get(norm_item(i), []), (usable_brief(cfg, i, idx, ever) or (None, None))[1]) for i in hl)
    for pr, order, item, title in sorted(items):
        hit = usable_brief(cfg, item, idx, ever, taken) or (relaunch_brief(cfg, strand[norm_item(item)][0], ever, taken) if norm_item(item) in strand else None)
        if not hit:
            if missing is not None: missing.append(item)
            continue
        f, name, keys = hit
        tier = "critical" if keys.get("TIER", "").upper() in ("CRITICAL", "HIGH") else "normal"
        kind = keys.get("KIND", "implement").lower()
        pr2 = int(keys["PRIORITY"]) if keys.get("PRIORITY", "").isdigit() else pr
        out.append((pr2, order, item, f, name, kind if kind in ("implement", "review", "audit", "cleanup") else "implement", tier)); taken.add(name)
    return sorted(out, key=lambda x: (x[0], x[1]))

def usable_brief(cfg, item, idx, ever, taken=()):
    """(path, unit name, keys) of the newest brief that is for item (its ITEM: line) and can still be launched: its unit name is
    valid, was never launched (ever: names of every unit recorded) or taken by this plan, and its out folder holds no output."""
    for f, name, keys, head in idx:
        if norm_item(keys.get("ITEM")) != norm_item(item): continue
        if name in ever or name in taken or out_used(cfg, name) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name): continue
        return f, name, keys
    return None

def relaunch_name(cfg, old, ever, taken=()):
    """The next free name for a relaunch of unit old: <base>-r<N+1> (old ends in -r<N>), else <old>-r2; at most 32 characters,
    never a name ever launched, taken by this plan, with output in out/ or with a brief of its own. None when none is free."""
    m = re.fullmatch(r"(.+?)-r(\d+)", old); base, n = (m.group(1), int(m.group(2))) if m else (old, 1)
    for k in range(n + 1, n + 50):
        s = f"-r{k}"; name = base[:32 - len(s)].rstrip("-_") + s
        if (re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) and name not in ever and name not in taken and not out_used(cfg, name)
                and not (cfg.p("briefs", "briefs") / f"{name}.md").exists()): return name
    return None

def relaunch_brief(cfg, u, ever, taken=()):
    """(the stranded unit u's brief, a new unit name, its keys): the candidate fill relaunches u's item with when no unlaunched brief
    is for it. cmd_fill writes the brief under the new name (relaunch_text) and seeds the unit from u (--resume-of). None when u's
    brief is no longer readable here or no name is free."""
    try:
        f = Path(u["brief"])
        with open(f, errors="replace") as fh: head = "".join(fh.readlines()[:12])
    except (OSError, KeyError, TypeError): return None
    name = relaunch_name(cfg, u["name"], ever, taken)
    own = {k: str(u[x]) for k, x in (("KIND", "kind"), ("TIER", "tier")) if u.get(x)}   # what it was launched as, unless its brief says
    return (f, name, {**own, **{k: v.rstrip(".,;") for k, v in BRIEF_KEYS.findall(head)}}) if name else None

def relaunch_text(cfg, u, brief, live):
    """The brief of a relaunch of the stranded unit u: its own brief, then why it was relaunched and what it starts from."""
    st, since = live.get(u.get("host"), ("?", None))
    patch = cfg.p("out", "out") / u["name"] / "backup.patch"
    return (Path(brief).read_text().rstrip("\n") + f"\n\n## Relaunch of {u['name']} (janus fill)\n\n{u['name']} ran on {u.get('host')}, which is {st}"
            + (f" (no answer since {ts(since)})" if since else "") + f". Your report starts from its last copy here (out/{u['name']}/REPORT.md)"
            + (f", and its changes as last synced ({ts(u['saved_at']) if u.get('saved_at') else 'time unknown'}) were applied to your tree from out/{u['name']}/backup.patch"
               " when they applied cleanly (your report says if they did not)" if patch.is_file() and patch.stat().st_size else "; no patch of its changes was synced, so start from the report")
            + f". Work done on {u.get('host')} after that sync is not here: redo it. Do not touch {u['name']}'s copy on its host.\n")

def fill_plan(cfg, hs, units, limit=None, launch=False, cands=None, missing=None, held=None, pinned=None):
    """(plan [(name, item, model, agent host, build host, budget, why)], stops [why the rest does not launch now]): candidates
    in priority order, each placed by choose() against the open units plus those planned before it, until the hosts'
    reservations or the provider budgets are used. limit (--max): at most that many per agent host. launch: see fill_candidates
    (beads unreadable: no plan, the stop says why). A unit placed on another host has its brief checked for that host
    (brief_plan) here, before anything exists: a brief it refuses is a stop with its reason, and the other candidates go on.
    cands, missing, held: fill_candidates' result when the caller has it (held: named with a stop, never planned). pinned: gets
    (unit, item, host) for each candidate its brief or [placement] pins to a stranded host: it is not moved (a pin is usually
    hardware), it waits, and check names it."""
    plan, stops, virt, spent, keep = [], [], [dict(u) for u in units], {}, {}
    down = stranded_hosts(cfg)
    if cands is None:
        missing = [] if missing is None else missing; held = [] if held is None else held
        try: cands = fill_candidates(cfg, units, launch, missing, held=held)
        except Err as e: return plan, [str(e)]
    bd = brief_dir(cfg)
    miss = f"; ready without a usable brief: {', '.join(missing[:8])}{'...' if len(missing) > 8 else ''} (write {bd}/<unit>.md with an ITEM: <id> line)" if missing else ""
    hel = held_text(held) if held else ""
    if not cands: return plan, [f"no ready backlog item has an unlaunched brief (a brief is for an item when {bd}/<unit>.md has an ITEM: <id> line)" + miss] + ([hel] if hel else [])
    for pr, order, item, f, name, kind, tier in cands:
        a = argparse.Namespace(unit=name, host=None, test_host=None, kind=kind, tier=tier)
        try: m = pick_model(cfg, tier, kind)   # unattended: the first route entry or a stated refusal, a fallback only where [routing] fallback = true
        except Err as e: stops.append(f"{name}: {e}"); continue
        mc = cfg.models[m]; budget, bwhy = budget_for(cfg, mc, kind, tier) if mc.get("kind") == "api" else (None, "")
        bl = dict(BRIEF_KEYS.findall("".join(open(f, errors="replace").readlines()[:12]))).get("BUDGET")   # the brief's own BUDGET: $12 line wins over the learned budget
        if budget is not None and bl:
            try: budget = float(bl.lstrip("$"))
            except ValueError: stops.append((name, f"BUDGET: {bl} is not a number")); continue
        if down:
            _, pwhy, want = place(cfg, a, Path(f), mc, virt, hs)
            if want in down:
                if pinned is not None: pinned.append((name, item, want))
                stops.append((name, f"pinned to {want} ({pwhy}), which is down: not moved (fix the host, or change the brief's TEST-HOST / [placement])")); continue
        hold = budget
        pv = cfg.prov.get(mc.get("provider"), {})
        if budget is not None and "cap_usd" in pv:
            if mc["provider"] not in keep: keep[mc["provider"]] = finish_reserve(cfg, mc["provider"], units)   # item 4: the open units' reserve
            k, need = keep[mc["provider"]]; left = pv["cap_usd"] - committed(cfg, pv) - spent.get(mc["provider"], 0) - k
            hold = reserved(cfg, budget) if k else budget   # with a reserve the plan counts what launch will hold (budget and margin)
            if left < hold:
                stops.append(f"budget: {mc['provider']} has ${left:.2f} left" + (f" (${k:.2f} kept for {len(need)} open unit(s) to finish)" if k else "") + f", {name} needs ${budget:g}"); break
        try: hn, b, why = choose(cfg, a, f, mc, virt, hs)
        except Err as e: stops.append((name, str(e).removeprefix("refused, "))); continue
        if limit and sum(1 for x in plan if x[3] == hn) >= limit:   # row 250: --max counts each host on its own; the other hosts go on
            stops.append((name, f"--max {limit} reached on {hn}")); continue
        if not cfg.hosts[hn].get("local") and cfg.hosts[hn].get("root"):   # the launch would refuse it there: refused now, before an out dir, tree or record exists
            try:
                with cfg.unlocked(): brief_plan(cfg, cfg.hosts[hn], name, (cfg.p("worktrees", "worktrees") / name).resolve(), Path(f).read_text())
            except (Err, OSError) as e: stops.append((name, f"brief for {hn}: {str(e).removeprefix('refused: ')}")); continue
        if budget is not None: spent[mc["provider"]] = spent.get(mc["provider"], 0) + hold
        plan.append((name, item, m, hn, b, budget, f"{why}; priority {pr}", kind, tier, f))
        virt.append({"name": name, "host": hn, "build": b, "state": "working", "item": item})
    if not stops: stops.append("every ready candidate is planned")
    if miss: stops.append(miss[2:])
    if hel: stops.append(hel)
    grp = {}   # one line per reason: the candidates it holds back
    for s in stops:
        n, w = s if isinstance(s, tuple) else (None, s)
        grp.setdefault(w, []).append(n)
    return plan, [(f"{len(ns)} candidate(s) ({', '.join(ns[:5])}{'...' if len(ns) > 5 else ''}): " if ns[0] else "") + w for w, ns in grp.items()]

def fill_repo(cfg, name, brief):
    """ISSUES row 83: the repository a fill launch makes the unit's worktree from, exactly as janus launch --new-worktree <repo>
    does. None when <worktrees>/<name> exists (launch takes that tree) or the project is in no git repository (the unit runs in
    the project root, as before). The repository: the brief's `REPO: <path>` line, else [fill] repo (a path from the project root: the
    project root need not be a Git root, the repository may be nested in it), else the project root itself when it is a repository's
    top; never a parent's repository found by walking up. Before, fill passed no tree at all, so every unit got the project root, a main checkout, and was
    refused: each fill launch needed a manual janus launch --new-worktree."""
    if (cfg.p("worktrees", "worktrees") / name).is_dir(): return None
    try: m = re.search(r"^REPO:\s*(\S+)", "".join(open(brief, errors="replace").readlines()[:12]), re.M)
    except OSError: m = None
    r = m.group(1) if m else cfg.c.get("fill", {}).get("repo")
    if r: return str((cfg.root / os.path.expanduser(r)).resolve())
    rc, o, e = sh(["git", "-C", str(cfg.root), "rev-parse", "--show-toplevel"])
    return o.strip() if rc == 0 and o.strip() and Path(o.strip()).resolve() == cfg.root.resolve() else None

def cmd_fill(cfg, a):
    """Launch ready backlog units that have briefs, highest priority first, until every host's reservations or the budget are
    used, and no further. --dry-run lists what it would launch now and why it stops there."""
    if cfg.run.dry: cfg.run.ro = True   # a dry run changes nothing anywhere (the probe leaves the hosts' reservation files alone)
    with cfg.unlocked(): hs = probe_all(cfg)   # the hosts' answers take as long as the slowest host: not under the writer lock (the watcher's pass holds it)
    try: units = Beads(cfg).units()
    except Err as e: raise Err(f"fill: beads cannot be read ({e}): nothing launched")
    ag = herdr_agents(cfg)
    for u in units: u["state"] = unit_state(u, hs, ag)
    plan, stops = fill_plan(cfg, hs, units, a.max, launch=True)   # fail closed: only what beads says is ready, never the bare checklist
    strand = stranded_cover(cfg, units); live = host_liveness(cfg)
    def src(item, f):   # the stranded unit a launch continues: only a relaunch of its own brief (another brief for the item is other work, never mixed with it)
        o = (strand.get(norm_item(item)) or [None])[0]
        return o if o and Path(f).resolve() == Path(o.get("brief") or "/").resolve() else None
    say(f"fill: {len(plan)} unit(s) fit now" + (" (dry run: nothing launched)" if cfg.run.dry else ""))
    for name, item, m, hn, b, budget, why, kind, tier, f in plan:
        o = src(item, f); was = [u["name"] for u in strand.get(norm_item(item), [])]
        say(f"  {name} [{item}] {kind}/{tier} model {m} on {hn}, builds on {b}" + (f", ${budget:g}" if budget is not None else "") + f": {why}"
            + (f"; relaunch of {o['name']} ({o.get('host')} {live.get(o.get('host'), ('?',))[0]})" if o else
               f"; its own brief, not seeded from {', '.join(was)} (stranded; still open)" if was else ""))
    say("stops: " + "; ".join(dict.fromkeys(stops)))
    if cfg.run.dry: return 0
    if not plan and any(s.startswith("beads cannot say") for s in stops): raise Err("fill: " + stops[0])   # an error, so the watcher's step counts it
    rc = 0
    for name, item, m, hn, b, budget, why, kind, tier, f in plan:
        o = src(item, f)
        try:
            if o:   # the stranded unit's own brief: written again under the new name
                nb = cfg.p("briefs", "briefs") / f"{name}.md"; nb.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(nb, relaunch_text(cfg, o, f, live)); f = nb
        except (OSError, KeyError) as e: say(f"fill: {name} not launched: its brief could not be written ({e})"); rc = 1; continue
        la = argparse.Namespace(unit=name, kind=kind, tier=tier, host=None, model=m, brief=str(f), cwd=None, budget=budget, test_host=None,
                                allow_main=False, of=None, report=None, resume_of=o["name"] if o else None, relaunch_of=o["name"] if o else None,
                                item=item, dry_run=False, auto=True,   # auto: launch rechecks the item
                                new_worktree=fill_repo(cfg, name, f), from_ref=cfg.c.get("fill", {}).get("from"))   # row 83
        try: cmd_launch(cfg, la)
        except Err as e: say(f"fill: {name} not launched: {e}"); rc = 1
    cfg.touch("last_fill")
    return rc

def issue_of(iss, item):
    """The backlog issue of an item, by its external ref only (backlog:<id>, compared in norm_item form)."""
    return next((i for r, i in iss.items() if norm_item(r[8:]) == norm_item(item)), None)

def hold_sources(cfg, item, iss):
    """What holds item now, read strictly for its release: ([(issue, its hold: labels)], [(draft path, [(line index, reason)])]) of
    every backlog issue and draft that names it (norm_item of the external ref backlog:<id>, of the draft's stem). A drafts folder
    or a matching draft that cannot be read (or is not UTF-8 text) is an Err naming it: never taken for not held."""
    k = norm_item(item); d = cfg.p("briefs", "briefs") / "drafts"
    labs = [(i, hl) for r, i in iss.items() if norm_item(r[8:]) == k and (hl := [l for l in (i.get("labels") or []) if l.startswith("hold:")])]
    try:
        if d.is_dir(): os.listdir(d)   # an unreadable folder raises here: glob would answer it as empty
        fs = [f for f in hold_drafts(cfg) if norm_item(f.stem) == k]
    except OSError as e: raise Err(f"{item}: the drafts folder {d} cannot be read ({e}), so its HOLD: lines are unknown: nothing changed")
    drafts = []
    for f in fs:
        try: hl = draft_holds(f.read_bytes().decode())
        except (OSError, UnicodeDecodeError) as e: raise Err(f"{item}: draft {f} cannot be read as text ({e}), so whether it holds {item} is unknown: make it readable, then run again (nothing changed)")
        if hl: drafts.append((f, hl))
    return labs, drafts

def hold_where(labs, drafts):
    """hold_sources' answer, one phrase per hold."""
    return [f"beads {i.get('id')} label {l}" for i, hl in labs for l in hl] + [f"{f} line {n + 1} (HOLD: {r})" for f, hl in drafts for n, r in hl]

def release_hold(cfg, item, iss):
    """janus backlog set <item> hold=off (none, no, or empty) [--dry-run]: take every hold off item: first the hold: labels of its
    backlog issues in beads, then each active HOLD: line in the head of its drafts becomes a RELEASED-HOLD: line (its reason and
    the rest of the draft kept; no draft is removed). A dry run names every change and makes none. A failure names what still
    holds it; run again: what was released is not touched again. It says "no longer held" only after beads and the drafts, read
    again, show no hold."""
    labs, drafts = hold_sources(cfg, item, iss)
    if not labs and not drafts: say(f"{item}: not held (no hold: label in beads, no HOLD: line in the head of a matching draft): nothing changed"); return
    keep = lambda i: ",".join(l for l in (i.get("labels") or []) if not l.startswith("hold:"))
    if cfg.run.dry:
        for i, hl in labs: say(f"+ bd update {i['id']} --set-labels {keep(i)}   (clears {', '.join(hl)})")
        for f, hl in drafts:
            for n, r in hl: say(f"+ {f} line {n + 1}: HOLD: {r} -> RELEASED-HOLD: {r}")
        say(f"{item}: {len(hold_where(labs, drafts))} hold source(s) would be released [dry-run: nothing changed]"); return
    done = []
    for n, (i, hl) in enumerate(labs):   # the labels first
        try: Beads(cfg).run("update", i["id"], "--set-labels", keep(i))
        except Err as e: raise Err(f"{item}: still held: bd update {i['id']} failed ({e}); not released: {', '.join(hold_where(labs[n:], drafts))}"
                                   + (f"; released: {', '.join(done)}" if done else "; nothing changed") + ". Run the same command again")
        done += hold_where([(i, hl)], [])
    bad = []
    with cfg.writer():   # then the drafts: each read again under the lock and replaced in one step (write-then-rename), its mode kept
        for f, _ in drafts:
            try:
                text = f.read_bytes().decode(); hl = draft_holds(text); ls = text.splitlines(keepends=True)
                for n, _r in hl: ls[n] = "RELEASED-HOLD:" + ls[n][len("HOLD:"):]
                if hl: atomic_write(f, "".join(ls), stat.S_IMODE(f.stat().st_mode))
                done += hold_where([], [(f, hl)])
            except (OSError, UnicodeDecodeError) as e: bad.append(f"{f} ({e})")
    if bad: raise Err(f"{item}: still held: HOLD: line(s) not released in {'; '.join(bad)}" + (f"; released: {', '.join(done)}" if done else "") + ". Run the same command again")
    try: left = hold_where(*hold_sources(cfg, item, backlog_issues(cfg)))
    except Err as e: raise Err(f"{item}: released {', '.join(done)}, but beads or its drafts cannot be read again to confirm ({e}): run the same command again")
    if left: raise Err(f"{item}: still held by {', '.join(left)} (written meanwhile?): run the same command again")
    say(f"{item}: no longer held (released: {', '.join(done)})")

def cells(line):
    """[(start, end)] of the cells of a markdown table row that begins with '|': each cell's text is line[start:end] (its spaces
    kept), the cell index as backlog_items counts them."""
    s = line.index("|"); bars = [s] + [k for k in range(s + 1, len(line)) if line[k] == "|"]
    out = [(x + 1, y) for x, y in zip(bars, bars[1:])]
    if line.rstrip()[-1:] != "|": out.append((bars[-1] + 1, len(line)))
    return out

def checklist_set(cfg, item, status=None, evidence=None):
    """Write an item's row of the checklist: its status cell (status) and/or its evidence cell ([backlog] evidence_col: the sha is
    added once, after what is there). Only those cells change; every other byte of the file stays. The row is found by its id
    cell ([backlog] id_col) in a table whose header names the id, status and evidence columns; no row, two rows (ambiguous), a
    checkbox line, an escaped '|' or a table without those columns is refused with the reason. Returns what changed ([] when
    nothing had to). The caller holds the writer lock (the file is read and replaced in one step: write-then-rename)."""
    b = backlog_cfg(cfg); fn = b.get("file")
    if not fn: raise Err("no checklist: set [backlog] file (or paths.checklist) in janus.toml")
    idc, sc, ec = (str(b.get(k, d)).lower() for k, d in (("id_col", "#"), ("status_col", "status"), ("evidence_col", "")))
    if evidence and not ec: raise Err(f"unsupported: [backlog] evidence_col is not set, so {fn} has no evidence cell to write the landing sha in")
    p = cfg.root / fn; text = p.read_text(); lines = text.split("\n"); hdr = None; hits = []
    for n, l in enumerate(lines):
        s_ = l.strip()
        if s_.startswith("#"): hdr = None; continue
        if s_.startswith("|"):
            cs = [c.strip() for c in s_.strip("|").split("|")]
            if all(re.fullmatch(r":?-*:?", c) for c in cs): continue
            low = [c.lower() for c in cs]
            if idc in low and sc in low: hdr = low; continue
            if hdr and len(cs) >= len(hdr) and norm_item(cs[hdr.index(idc)]) == norm_item(item): hits.append((n, hdr))
            continue
        m = re.match(r"^[-*] \[( |x|X)\] (.+)$", s_)
        if m and norm_item(slug(m.group(2))) == norm_item(item): raise Err(f"unsupported: {item} is a checkbox line in {fn} (line {n + 1}), which has no status or evidence cell: edit it by hand")
    if not hits: raise Err(f"no row of {item} in a table of {fn} with columns {idc!r} and {sc!r}")
    if len(hits) > 1: raise Err(f"ambiguous: {item} has {len(hits)} rows in {fn} (lines {', '.join(str(n + 1) for n, _ in hits)}): make its id unique")
    n, hdr = hits[0]; l = lines[n]
    if "\\|" in l: raise Err(f"unsupported: the row of {item} in {fn} (line {n + 1}) has an escaped '|': edit it by hand")
    if evidence and ec not in hdr: raise Err(f"unsupported: the table of {item} in {fn} has no {ec!r} column for the evidence")
    want, did = {}, []
    if status:
        cur = l[slice(*cells(l)[hdr.index(sc)])].strip()
        if cur.upper() != status.upper(): want[hdr.index(sc)] = status; did.append(f"status {cur or '-'} -> {status}")
    if evidence:
        k = hdr.index(ec); cur = l[slice(*cells(l)[k])].strip()
        if evidence not in cur:
            want[k] = evidence if cur in ("", "-") else f"{cur}, {evidence}"; did.append(f"evidence {evidence}")
    for k in sorted(want, reverse=True):   # from the right: the earlier cells' positions stay valid
        a_, z = cells(l)[k]; l = l[:a_] + f" {want[k]} " + l[z:]
    if did and not cfg.run.dry: lines[n] = l; atomic_write(p, "\n".join(lines))
    return did

def accept_item(cfg, item, status, unit, commit=None):
    """janus backlog set <item> status=DONE --unit <unit> [--commit <sha>] [--dry-run]: the owner's explicit acceptance of a whole
    item, never a side effect of a landing or a close. The unit must be linked to the item (its --item or brief ITEM: line) and its
    landing publication recorded (janus land --push), or, for a unit whose agent pushed without janus land, --commit: a commit its
    own report names. Either must be on the remote's landing branch now (fetched again: unknown is a refusal). Then, under the
    writer lock, the checklist row: status DONE and the sha as evidence; then the item's beads issue is closed. Run again it
    finishes what a failure left (each step is idempotent) and changes nothing more."""
    dn = [x.upper() for x in backlog_cfg(cfg).get("done", ["DONE"])]
    if status.upper() not in dn: raise Err(f"status={status}: only an item's acceptance is set here (status={dn[0]}); other statuses are the checklist's own edits")
    if not unit: raise Err("status=DONE needs --unit <unit>: the unit whose published landing is the item's evidence")
    u = unit_any(cfg, unit)
    if not u: raise Err(f"no unit {unit}")
    try: iss = backlog_issues(cfg)   # the item's issue is found before anything is written: no checklist DONE without it
    except Err as e: raise Err(f"refused: beads cannot be read to find {item}'s backlog issue ({e}): nothing changed")
    xs = [i for r, i in iss.items() if norm_item(r[8:]) == norm_item(item)]
    if not xs: raise Err(f"refused: no backlog issue for {item} in beads (janus backlog import first): nothing changed")
    if len(xs) > 1: raise Err(f"refused: ambiguous: {len(xs)} backlog issues for {item} ({', '.join(str(i.get('id')) for i in xs)}): nothing changed")
    x = xs[0]
    if norm_item(item) not in unit_items(cfg, [u], iss): raise Err(f"{unit} is not linked to item {item} (its --item or its brief's ITEM: line): refused")
    pub = u.get("published") or {}
    if commit:
        if pub.get("commit"): raise Err(f"{unit}'s publication is recorded ({pub['commit'][:12]}): --commit is only for a unit pushed without janus land")
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit) or commit.lower() not in report_text(cfg, u).lower():
            raise Err(f"--commit {commit}: not a commit {unit}'s report names ({report_path(cfg, u)}): {item} is not accepted")
        pub = {"commit": commit, "remote": "origin", "branch": landing_branch(cfg, published_repo(cfg, u))}
        made = lambda: sh(["git", "-C", str(published_repo(cfg, u)), "show", "-s", "--format=%ct", commit + "^{commit}"])[1].strip()
    if not pub.get("commit"): raise Err(f"{unit} has no verified publication recorded (janus land {unit} --push records it; a commit its agent pushed itself: --commit <sha>): {item} is not accepted")
    repo = published_repo(cfg, u)
    with cfg.unlocked(): ok = verify_published(repo, pub)   # git fetch: never under the writer lock
    if ok is None: raise Err(f"the publication of {pub['commit'][:12]} on {pub.get('remote')}/{pub.get('branch')} cannot be verified now (fetch failed or unknown commit): refused, try again")
    if not ok: raise Err(f"{pub['commit'][:12]} is not on {pub.get('remote')}/{pub.get('branch')} now: {item} is not accepted")
    if commit and int(made() or 0) < u.get("started", 0):   # the base it started from, which its report may name: not its work
        raise Err(f"--commit {commit} was made before {unit} started: not its work; {item} is not accepted")
    sha = (sh(["git", "-C", str(repo), "rev-parse", "--verify", pub["commit"] + "^{commit}"])[1].strip() or pub["commit"])[:12]
    if cfg.run.dry:
        did = checklist_set(cfg, item, dn[0], sha)
        say(f"+ {item}: " + ("; ".join(did) or "the checklist already says so") + f"; close its beads issue (unit {unit}, {sha}) [dry-run: nothing changed]"); return 0
    with cfg.writer(): did = checklist_set(cfg, item, dn[0], sha)   # short: one read and one rename of the file (the lock itself, an agent's call too)
    say(f"{item}: " + ("; ".join(did) if did else f"the checklist already says {dn[0]} with {sha}"))
    if x.get("status") == "closed": say(f"{item}: beads issue {x['id']} already closed"); return 0
    try: Beads(cfg).run("close", x["id"], "--reason", f"checklist: {dn[0]} (unit {unit}, {sha})")
    except Err as e: raise Err(f"{item}: the checklist is updated ({dn[0]}, evidence {sha}), but the beads sync is incomplete: issue {x['id']} was not closed ({e}). "
                               f"Run the same command again: it changes nothing more in the checklist and closes the issue")
    say(f"{item}: beads issue {x['id']} closed"); return 0

def published_repo(cfg, u):
    """A local repository to verify a unit's publication from: its worktree while it is there; else the repository recorded at
    janus land (published.repo); else the configured fill repository ([fill] repo), or the project root only when it is itself
    a repository's top. Never a parent's repository found by walking up: Err when there is none."""
    c = Path(u.get("cwd") or "/"); r = (u.get("published") or {}).get("repo"); fr = cfg.c.get("fill", {}).get("repo")
    for x in ([c] if c.is_dir() and c.resolve() != Path("/") else []) + ([Path(r)] if r else []) + ([(cfg.root / os.path.expanduser(fr)).resolve()] if fr else []):
        if x.is_dir() and sh(["git", "-C", str(x), "rev-parse", "--git-dir"])[0] == 0: return x
    rc, o, e = sh(["git", "-C", str(cfg.root), "rev-parse", "--show-toplevel"])
    if rc == 0 and o.strip() and Path(o.strip()).resolve() == cfg.root.resolve(): return cfg.root
    raise Err(f"refused: no local repository to verify {u.get('name')}'s publication from (its worktree is gone and none was recorded): set [fill] repo")
