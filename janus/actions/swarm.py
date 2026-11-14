"""The shared project memory and the status view: janus remember/forget/memory, transcript, index, report, and janus ledger
(check, repair, reconcile)."""
import json, os, re, time
from janus.backlog import backlog_items, item_actors, unit_items
from janus.beads import Beads
from janus.board import board, board_dir
from janus.budgets import OUTCOMES, run_outcomes
from janus.context import Err, Locked, NUM, atomic_write, memory_path, refuse_agent, say, slug, ts
from janus.findings.units import unreconciled
from janus.ledger import RECONCILED, ledger_done, ledger_path, ledger_read, committed, left_cost, recost, recost_plan, reconciled_plan, spend
from janus.memory import MEM_SECTIONS, mem_line, memory_history, memory_read, memory_write
from janus.probe import probe_all
from janus.transcripts import read_transcript
from janus.units import ENDED, herdr_agents, report_summary, unit_any, unit_state

def cmd_ledger(cfg, a):
    """janus ledger check: every provider ledger, exit 1 when one is malformed. janus ledger recost: correct the done rows that
    charge a resumed run its session's whole total (--dry-run lists them first). janus ledger repair: keep the first running row
    and the first done row of each run id, drop rows that cannot be parsed, after a backup <ledger>.bak-<time>; the file is
    replaced by write-then-rename under the lock."""
    pvs = [(n, pv) for n, pv in cfg.prov.items() if "ledger" in pv and (not a.provider or n == a.provider)]
    if not pvs: raise Err("no provider has a ledger in janus.toml")
    worst = 0
    if a.action == "recost":   # rows that charge a resumed run its session's whole total: each run pays its own part (once)
        if not cfg.run.dry: refuse_agent("janus ledger recost")
        for n, pv in pvs:
            plan = recost_plan(cfg, pv); over = sum(c - own for _, _, c, own in plan)
            found = reconciled_plan(cfg, pv); bound = sum(c - own for _, _, c, own, _ in found)   # row 222: agent-gone runs booked at their budget
            if not plan and not found: say(f"{n}: nothing to correct"); continue
            for k, un, c, own in plan: say(f"{'would correct' if cfg.run.dry else 'corrected'} {k} ({un}): ${c:.2f} (its session's total) -> ${own:.2f} (its own cost)")
            for k, un, c, own, src in found: say(f"{'would correct' if cfg.run.dry else 'corrected'} {k} ({un}): ${c:.2f} (its budget, counted as the upper bound) -> ${own:.2f} (the cost its {src} reports)")
            parts = ", ".join(x for x in (f"${over:.2f} charged twice" if plan else "", f"${bound:.2f} counted at the bound and not spent" if found else "") if x)
            if cfg.run.dry: say(f"{n}: {len(plan) + len(found)} row(s), {parts}; committed ${committed(cfg, pv):.2f} -> ${committed(cfg, pv) - over - bound:.2f} [dry-run: nothing changed]"); continue
            bak = recost(cfg, pv, plan, found)
            say(f"{n}: corrected {len(plan) + len(found)} row(s), ${over + bound:.2f} less; committed now ${committed(cfg, pv):.2f}; backup {bak}")
        return 0
    if a.action == "reconcile":   # runs whose agent is gone and that have no done row: done unknown, budget as the upper bound
        if not cfg.run.dry: refuse_agent("janus ledger reconcile")
        units = Beads(cfg).units(); hs = probe_all(cfg); ag = herdr_agents(cfg)
        for u in units: u["state"] = unit_state(u, hs, ag)
        for n, pv in pvs:
            rs = unreconciled(cfg, pv, units)
            if not rs: say(f"{n}: nothing to reconcile"); continue
            for k, un, b, why in rs:
                cost, rep = left_cost(cfg, ledger_read(cfg, pv)[0], k)   # what the run itself reported, when its json is here (a closed unit's api.json too: row 222)
                if rep:
                    if cfg.run.dry: say(f"would close {k} ({un}) at ${cost:.2f} (the cost its json reports): {why}"); continue
                    ok = ledger_done(cfg, pv, k, un, cost, "reconciled")
                    say(f"{'closed' if ok else 'already done:'} {k} ({un}) at ${cost:.2f} (the cost its json reports): {why}"); continue
                if cfg.run.dry: say(f"would mark {k} ({un}) {RECONCILED}, ${b:g} counted (its budget, the upper bound): {why}"); continue
                ok = ledger_done(cfg, pv, k, un, b, "unknown")
                say(f"{'marked' if ok else 'already done:'} {k} ({un}) {RECONCILED}, ${b:g} counted: {why}")
        return 0
    for n, pv in pvs:
        p = ledger_path(cfg, pv)
        if a.action == "check":
            runs, bad, notes = ledger_read(cfg, pv)
            say(f"{n}: {p}: {sum(len(r['running']) + len(r['done']) for r in runs.values())} rows, {len(runs)} runs, "
                f"${spend(runs):.2f} committed" + (f" of ${pv['cap_usd']}" if "cap_usd" in pv else "") + (f"; MALFORMED, {len(bad)} problem(s):" if bad else "; valid"))
            for x in bad[:20] + notes[:10]: say("  " + x)
            if len(bad) > 20: say(f"  ... {len(bad) - 20} more")
            worst = max(worst, 1 if bad else 0); continue
        if cfg.run.dry: say(f"+ back up {p}, keep the first running and done row per run id, write-then-rename"); continue
        with Locked(p):
            for _ in range(5):
                try: raw = p.read_bytes()
                except FileNotFoundError: say(f"{n}: no ledger yet"); break
                keep, seen, dropped = [], set(), 0
                for l in raw.decode(errors="replace").splitlines():
                    f = l.split("\t")
                    ok = len(f) == 5 and f[1].strip() and f[4].strip() and ((f[2] == "running" and NUM.match(f[3].strip())) or (NUM.match(f[2].strip()) and f[3].startswith("done")))
                    k = (f[4], "running" if ok and f[2] == "running" else "done") if ok else None
                    if not ok or k in seen: dropped += bool(l.strip()); continue
                    seen.add(k); keep.append(l)
                bak = p.with_name(p.name + time.strftime(".bak-%Y%m%d-%H%M%S")); bak.write_bytes(raw)
                tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp"); tmp.write_text("".join(x + "\n" for x in keep))
                if p.stat().st_size != len(raw): tmp.unlink(); continue   # an unlocked writer appended meanwhile: read again
                os.replace(tmp, p)
                say(f"{n}: repaired {p}: kept {len(keep)} rows, dropped {dropped}; backup {bak}")
                break
            else: raise Err(f"{p} kept changing while it was repaired: stop the writer and retry")
        runs, bad, _ = ledger_read(cfg, pv)
        if bad: say(f"  still malformed: {bad[0]}"); worst = 1
    return worst

def cmd_report(cfg, a):
    """The status view, derived from the board, the units and the backlog now (nothing is stored): backlog progress, spend by
    outcome, open units. --brief: the summary lines only."""
    L = [f"janus report: {cfg.name}, {ts()}"]
    items = backlog_items(cfg)
    if items is not None:
        act = item_actors(cfg); done = [x for x in items if x[3]]; left = [x for x in items if not x[3]]
        try: cov = unit_items(cfg, Beads(cfg).units())
        except Err: cov = set()
        L.append(f"Backlog: {len(done)} of {len(items)} items done ({100 * len(done) // max(len(items), 1)} %); {len(left)} left: "
                 + ", ".join(f"{n} {k}" for k, n in sorted(Counter_(act.get(i, 'agent') for i, *_ in left).items())) + f"; {sum(1 for i, *_ in left if i in cov)} in open units")
        if not a.brief:
            for i, t, st, d, rd in left[:40]: L.append(f"  {'*' if i in cov else ' '} {i:10} [{act.get(i, 'agent')}] {st[:20]:20} {t[:80]}")
    outs = run_outcomes(cfg)
    for pn in dict.fromkeys(x[0] for x in outs):
        mine = [x for x in outs if x[0] == pn]; tot = sum(x[3] for x in mine)
        L.append(f"Spend {pn}: ${tot:.2f} in {len(mine)} run(s)")
        for k, lab in OUTCOMES:
            xs = [x for x in mine if x[4] == k]
            if xs: L.append(f"  {lab:28} ${sum(x[3] for x in xs):8.2f}  {len(xs):3} run(s)")
    try:
        us = Beads(cfg).units()
        L.append(f"Open units: {len(us)}" + ("" if a.brief else ": " + ", ".join(u["name"] for u in us)[:300]))
    except Err as e: L.append(f"Open units: unavailable ({e})")
    return 0, L

def Counter_(it):
    c = {}
    for x in it: c[x] = c.get(x, 0) + 1
    return c

def cmd_transcript(cfg, a):
    u = unit_any(cfg, a.unit)
    saved = cfg.p("out", "out") / a.unit / "transcript.md"
    if not u and not saved.is_file(): raise Err(f"no unit {a.unit}")
    rc, txt = read_transcript(cfg, u, a.grep or "", a.tail) if u else (3, "")
    if rc and saved.is_file():   # the remote copy is gone: the condensed copy janus collect saved
        L = saved.read_text().splitlines()
        if a.grep: L = [l for l in L if re.search(a.grep, l, re.I)]
        txt = f"(saved copy {saved})\n" + "\n".join(L[-a.tail:] if a.tail > 0 else L); rc = 0
    return 0 if rc == 0 else 1, [txt]

def write_index(cfg):
    """out/INDEX.md, one line per unit (verdict, result, report path), newest first; a copy in the board folder (index.md) so
    the board sync carries it to every host. A unit's report.json is preferred over its report's prose (units.report_summary).
    Returns the number of units."""
    od = cfg.p("out", "out"); rows = []
    for rp in od.glob("*/REPORT.md"):
        try:
            try: j = json.loads(rp.with_name("report.json").read_text())
            except (OSError, ValueError): j = None
            v, r = report_summary(rp.read_text(errors="replace"), j); rows.append((rp.stat().st_mtime, rp.parent.name, v, r, rp))
        except OSError: continue
    rows.sort(reverse=True)
    text = (f"# Reports index ({ts()}; janus index, regenerated at janus commit-state)\n"
            + "".join(f"- {n} | {v} | {r or '-'} | {rp}\n" for _, n, v, r, rp in rows))
    if not cfg.run.dry and not cfg.run.ro:
        atomic_write(od / "INDEX.md", text)
        if board_dir(cfg).is_dir(): atomic_write(board_dir(cfg) / "index.md", text)
    return len(rows)

def cmd_index(cfg, a):
    n = write_index(cfg); say(f"{cfg.p('out', 'out') / 'INDEX.md'}: {n} unit(s)")

def cmd_remember(cfg, a):
    refuse_agent("janus remember")
    kind = next(k for k in MEM_SECTIONS if getattr(a, k))
    text = re.sub(r"\s+", " ", a.text).strip()
    if not text: raise Err("nothing to remember")
    if kind == "fact" and not a.source: raise Err("a fact needs --source <path|sha|job> (where it can be checked)")
    es = memory_read(cfg)
    if any(e[3] == text for e in es): raise Err("already remembered (exact duplicate)")
    key = slug(a.key) if a.key else None
    old = [e for e in es if key and e[2] == key]
    new = (MEM_SECTIONS[kind], time.strftime("%Y-%m-%d"), key, text, a.source)
    if cfg.run.dry: say(f"+ remember {mem_line(new)}" + (f" (replaces {len(old)} on [{key}])" if old else "")); return
    for e in old: memory_history(cfg, e, "replaced")
    memory_write(cfg, [e for e in es if e not in old] + [new])
    say(f"remembered ({MEM_SECTIONS[kind]})" + (f", replacing the older entry on [{key}]" if old else "") + f": {text}")
    if kind in ("instruction", "decision"): tell_running(cfg, f"{'replaced' if old else 'new'} {kind}{f' [{key}]' if key else ''}: {text}")

def tell_running(cfg, text):
    """A changed instruction or decision reaches each running agent as one addressed board direction (read before its next step).
    No answer is asked for and nothing forces a final response."""
    try: units = Beads(cfg).units()
    except Err: return
    for u in units:
        if u.get("state") in ENDED or u.get("verdict"): continue
        rc, o, e = board(cfg, "direct", u["name"], "[memory] " + text + " (MEMORY.md has the whole list; no reply needed)")
        if rc: say(f"not delivered to {u['name']}: {(e or o).strip()[:80]}")

def cmd_forget(cfg, a):
    refuse_agent("janus forget")
    es = memory_read(cfg); q = a.text.strip()
    hit = [es[int(q) - 1]] if q.isdigit() and 0 < int(q) <= len(es) else [e for e in es if e[3] == q] or [e for e in es if q in e[3] or q == e[2]]
    if len(hit) != 1: raise Err(f"{'no' if not hit else len(hit)} entries match {q!r}" + ("; give its number (janus memory) or more text" if hit else ""))
    if cfg.run.dry: say(f"+ forget {mem_line(hit[0])}"); return
    memory_history(cfg, hit[0], "forgotten"); memory_write(cfg, [e for e in es if e != hit[0]])
    say(f"forgotten (moved to {cfg.f('memory-history.md').name}): {hit[0][3]}")

def cmd_memory(cfg, a):
    return 0, [f"{i:3} {e[0][:-1].lower():11} {mem_line(e)[2:]}" for i, e in enumerate(memory_read(cfg), 1)] or [f"(no entries in {memory_path(cfg)})"]
