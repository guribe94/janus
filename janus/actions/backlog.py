"""janus backlog (import, list, set actor) and janus fill (launch ready backlog units up to capacity)."""
import argparse, re
from janus.actions.launch import choose, cmd_launch, pick_model
from janus.backlog import backlog_cfg, backlog_issues, backlog_items, item_actors, unit_items
from janus.beads import Beads
from janus.budgets import budget_for, finish_reserve, waste_block
from janus.context import Err, say, slug
from janus.ledger import committed, reserved
from janus.probe import probe_all
from janus.units import all_units, herdr_agents, unit_state

def cmd_backlog(cfg, a):
    if a.action == "set":   # janus backlog set <id> actor=<x>
        if not a.args or len(a.args) != 2 or not a.args[1].startswith("actor="): raise Err("usage: janus backlog set <item id> actor=<agent|owner|hardware|...>")
        x = backlog_issues(cfg).get(f"backlog:{a.args[0]}")
        if not x: raise Err(f"no backlog item {a.args[0]} in beads (janus backlog import first)")
        v = slug(a.args[1][6:]) or "agent"; labels = [l for l in (x.get("labels") or []) if not l.startswith("actor:")] + ([f"actor:{v}"] if v != "agent" else [])
        if cfg.run.dry: say(f"+ bd update {x['id']} --set-labels {','.join(labels)}"); return
        Beads(cfg).run("update", x["id"], "--set-labels", ",".join(labels)); say(f"{a.args[0]}: actor {v}"); return
    items = backlog_items(cfg)
    if items is None: raise Err("no checklist: set [backlog] file (or paths.checklist) in janus.toml")
    if a.action == "list":
        act = item_actors(cfg)
        for i, t, st, d, rd in items: say(f"{'done ' if d else 'ready' if rd else 'wait '} {i:8} {act.get(i, 'agent'):8} {st[:24]:24} {t[:90]}")
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
        labels += [l for l in (x.get("labels") or []) if l.startswith("actor:")]   # set by janus backlog set: kept
        if sorted(x.get("labels") or []) != sorted(labels) or x.get("title") != title:
            bd.run("update", x["id"], "--title", title, "--set-labels", ",".join(labels)); n["updated"] += 1
        else: n["same"] += 1
    if not cfg.run.dry: cfg.touch("backlog_imported")
    say(f"backlog: {len(items)} checklist items; " + ", ".join(f"{v} {k}" for k, v in n.items()))

BRIEF_KEYS = re.compile(r"^(ITEM|KIND|TIER|TEST-HOST|PRIORITY):\s*(\S+)", re.M)

def brief_index(cfg):
    """[(path, unit name, {ITEM, KIND, TIER, TEST-HOST, PRIORITY}, head text)] of every brief, newest first."""
    out = []
    for f in sorted(cfg.p("briefs", "briefs").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(f, errors="replace") as fh: head = "".join(next(fh, "") for _ in range(12))
        except OSError: continue
        out.append((f, f.stem, {k: v.rstrip(".,;") for k, v in BRIEF_KEYS.findall(head)}, head))
    return out

def fill_candidates(cfg, units):
    """Ready backlog items with a brief and no unit, highest priority first: [(priority, order, item, brief path, unit name,
    kind, tier)]. Items: the beads backlog (open, backlog-ready; priority from beads), else the checklist (its order). A brief
    is for an item when its ITEM: line names it (an explicit mark: a brief that merely mentions an item may be old or a
    review); a brief whose unit name was ever launched, or that has an out/<name> folder, is spent. The newest brief wins."""
    try: iss = backlog_issues(cfg)
    except Err: iss = {}
    if iss: items = [(int(i.get("priority", 2) if i.get("priority") is not None else 2), n, r[8:], i.get("title", "")[9:]) for n, (r, i) in enumerate(iss.items())
                     if i.get("status") in ("open", "in_progress") and "backlog-ready" in (i.get("labels") or [])]
    else: items = [(int(2), n, i, t) for n, (i, t, st, d, rd) in enumerate(backlog_items(cfg) or []) if rd]
    cov = unit_items(cfg, units); ever = all_units(cfg); out = []; idx = brief_index(cfg); taken = set(); act = item_actors(cfg)
    for pr, order, item, title in sorted(items):
        if item in cov or act.get(item, "agent") != "agent": continue
        for f, name, keys, head in idx:
            if keys.get("ITEM", "").lower() != item.lower(): continue
            if name in ever or name in taken or (cfg.p("out", "out") / name).exists(): continue
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name): continue
            tier = "critical" if keys.get("TIER", "").upper() in ("CRITICAL", "HIGH") else "normal"
            kind = keys.get("KIND", "implement").lower()
            pr2 = int(keys["PRIORITY"]) if keys.get("PRIORITY", "").isdigit() else pr
            out.append((pr2, order, item, f, name, kind if kind in ("implement", "review", "audit", "cleanup") else "implement", tier)); taken.add(name)
            break
    return sorted(out, key=lambda x: (x[0], x[1]))

def fill_plan(cfg, hs, units, limit=None):
    """(plan [(name, item, model, agent host, build host, budget, why)], stops [why the rest does not launch now]): candidates
    in priority order, each placed by choose() against the open units plus those planned before it, until the hosts'
    reservations or the provider budgets are used."""
    plan, stops, virt, spent, keep = [], [], [dict(u) for u in units], {}, {}
    cands = fill_candidates(cfg, units)
    if not cands: return plan, ["no ready backlog item has an unlaunched brief (a brief is for an item when it has an ITEM: <id> line)"]
    for pr, order, item, f, name, kind, tier in cands:
        if limit and len(plan) >= limit: stops.append(f"--max {limit} reached; {len(cands) - len(plan)} more candidate(s)"); break
        a = argparse.Namespace(unit=name, host=None, test_host=None, kind=kind, tier=tier)
        try: m = pick_model(cfg, tier, kind, None)
        except Err as e: stops.append(f"{name}: {e}"); continue
        mc = cfg.models[m]; budget, bwhy = budget_for(cfg, mc, kind, tier) if mc.get("kind") == "api" else (None, ""); hold = budget
        pv = cfg.prov.get(mc.get("provider"), {})
        if budget is not None and "cap_usd" in pv:
            if mc["provider"] not in keep: keep[mc["provider"]] = finish_reserve(cfg, mc["provider"], units)   # item 4: the open units' reserve
            k, need = keep[mc["provider"]]; left = pv["cap_usd"] - committed(cfg, pv) - spent.get(mc["provider"], 0) - k
            hold = reserved(cfg, budget) if k else budget   # with a reserve the plan counts what launch will hold (budget and margin)
            if left < hold:
                stops.append(f"budget: {mc['provider']} has ${left:.2f} left" + (f" (${k:.2f} kept for {len(need)} open unit(s) to finish)" if k else "") + f", {name} needs ${budget:g}"); break
        try: hn, b, why = choose(cfg, a, f, mc, virt, hs)
        except Err as e: stops.append((name, str(e).removeprefix("refused, "))); continue
        if budget is not None: spent[mc["provider"]] = spent.get(mc["provider"], 0) + hold
        plan.append((name, item, m, hn, b, budget, f"{why}; priority {pr}", kind, tier, f))
        virt.append({"name": name, "host": hn, "build": b, "state": "working", "item": item})
    if not stops: stops.append("every ready candidate is planned")
    grp = {}   # one line per reason: the candidates it holds back
    for s in stops:
        n, w = s if isinstance(s, tuple) else (None, s)
        grp.setdefault(w, []).append(n)
    return plan, [(f"{len(ns)} candidate(s) ({', '.join(ns[:5])}{'...' if len(ns) > 5 else ''}): " if ns[0] else "") + w for w, ns in grp.items()]

def cmd_fill(cfg, a):
    """Launch ready backlog units that have briefs, highest priority first, until every host's reservations or the budget are
    used, and no further. --dry-run lists what it would launch now and why it stops there."""
    if cfg.run.dry: cfg.run.ro = True   # a dry run changes nothing anywhere (the probe leaves the hosts' reservation files alone)
    hs = probe_all(cfg); units = Beads(cfg).units(); ag = herdr_agents(cfg)
    for u in units: u["state"] = unit_state(u, hs, ag)
    wp = waste_block(cfg)
    if wp: say(f"fill: PAUSED: {wp}"); return 1
    plan, stops = fill_plan(cfg, hs, units, a.max)
    say(f"fill: {len(plan)} unit(s) fit now" + (" (dry run: nothing launched)" if cfg.run.dry else ""))
    for name, item, m, hn, b, budget, why, kind, tier, f in plan:
        say(f"  {name} [{item}] {kind}/{tier} model {m} on {hn}, builds on {b}" + (f", ${budget:g}" if budget is not None else "") + f": {why}")
    say("stops: " + "; ".join(dict.fromkeys(stops)))
    if cfg.run.dry: return 0
    rc = 0
    for name, item, m, hn, b, budget, why, kind, tier, f in plan:
        la = argparse.Namespace(unit=name, kind=kind, tier=tier, host=None, model=m, brief=str(f), cwd=None, budget=None, test_host=None,
                                allow_main=False, of=None, report=None, resume_of=None, item=item, dry_run=False)
        try: cmd_launch(cfg, la)
        except Err as e: say(f"fill: {name} not launched: {e}"); rc = 1
    cfg.touch("last_fill")
    return rc
