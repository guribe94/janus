"""Findings about work in flight: failed gates, known failures, finished units not gated or not landed, idle backlog,
and the project's own git state."""
import re, time
from janus.actions.backlog import fill_candidates, fill_plan
from janus.backlog import backlog_cfg, backlog_issues, item_actors, item_holds, unit_items
from janus.beads import Beads
from janus.context import Err, LE, handoff_path, handoff_stale, now, sh
from janus.intents import intent_stale, intents
from janus.gates import gate_ids, gate_results, kf_active, known_failures, landed, unapplied_patch
from janus.platform import watcher_loaded, watcher_service
from janus.probe import room
from janus.project import own_label
from janus.units import report_path

def gate_findings(cfg, hs=None):
    """Failed gates of the last 12 h: only each gate's latest result counts, and none while a newer run of that gate is queued or
    running (a superseded failure never comes back after its ack expires)."""
    try: lines = cfg.p("gate_log").read_text().splitlines()[-200:]
    except OSError: return []
    busy = {(own_label(cfg, t) or "")[5:] for p in (hs or {}).values() for t in ((p.get("pueue") or {}).get("tasks") or {}).values()
            if (own_label(cfg, t) or "").startswith("gate:") and any(k in str(t.get("status")) for k in ("Queued", "Running"))}
    latest = {}
    for l in lines:
        if "RESULT:" in l and len(l) > 20: latest[l[20:].split()[0]] = l
    fails, cut = [], now() - 12 * 3600; judged = gate_results(cfg); codes = {}
    for n, l in latest.items():
        if "RESULT: FAIL" not in l or n in busy or judged.get(n, ("", ""))[1] == "PASS": continue   # only known failures: passed
        try:
            if time.mktime(time.strptime(l[:19], "%Y-%m-%d %H:%M:%S")) < cut: continue
        except ValueError: continue
        fails.append((l[:19], n, [t.split("=")[0] for t in l.split() if re.match(r"EXIT_\w+=(?!0$)", t)]))
        codes[n] = [(s, int(c)) for s, c in re.findall(r"EXIT_(\w+)=(\d+)", l)]
    out = [(f"gate:{n}@{t[11:]}", f"gate {n} FAILED at {t[11:]} ({' '.join(x[5:] for x in k[:4])}{'...' if len(k) > 4 else ''}); re-queue or classify")
           for t, n, k in fails]
    # soak 39: a gate whose steps all exit at once with a usage / not-found code (2, 126, 127) in ~0 min is a SETUP problem (a
    # broken script, or a migration that left a script calling the retired CLI), not a test failure. Say so, so nobody reads it as
    # failing tests.
    times = cfg.jget("gate-times.json")
    for t, n, _ in fails:
        cs = codes.get(n, []); last = (times.get(n) or [None])[-1]
        if cs and all(c in (2, 126, 127) for _, c in cs) and (last is None or last <= 0.5):
            out.append((f"gate-setup:{n}", f"gate {n} failed at {t[11:]} with every step exiting at once (" + ", ".join(f"EXIT_{s}={c}" for s, c in cs)
                        + (f" in {last:g} min" if last is not None else "") + "): a setup problem (a broken gate script or a script still calling a "
                        + f"retired CLI), not the tests: fix the script (janus gate-log {n}) and re-queue"))
    cnt = {}
    for t, n, ks in fails:
        for k in set(ks): cnt.setdefault(k, set()).add(n)
    out += [(f"gate-common:{k}", f"{k[5:]} fails in {len(v)} gates ({', '.join(sorted(v)[:3])}): suspect the test or fixture, not the units")
            for k, v in cnt.items() if len(v) >= 2]
    return out

def git_findings(cfg):
    """git:dirty, git:unpushed, git:noupstream of the project's own repository."""
    F = []
    rc, o, e = sh(["git", "status", "--porcelain", "-uall"], cwd=cfg.root)
    if rc == 0:
        dirty = o.splitlines()
        # janus's own state files (acks, notes, open, stops, running, hosts, stamps, job runs, the pueue shim) change on every
        # check and ack: never a finding. The orchestrator's notes (.md files in the state dir) still count.
        sd = cfg.state.resolve().relative_to(cfg.root.resolve()).as_posix() + "/" if cfg.state.resolve().is_relative_to(cfg.root.resolve()) else None
        if sd: dirty = [l for l in dirty if not (l[3:].strip().strip('"').startswith(sd) and not l[3:].strip().strip('"').endswith(".md"))]
        hp = handoff_path(cfg).relative_to(cfg.root) if handoff_path(cfg).is_relative_to(cfg.root) else None
        if hp and handoff_path(cfg).exists() and any(l[3:].strip() == str(hp) for l in dirty):   # check's own rewrite of the Live block is not a change (row 50)
            rc2, old, _ = sh(["git", "show", f"HEAD:{hp}"], cwd=cfg.root)
            if rc2 == 0 and old.split(LE, 1)[-1] == handoff_path(cfg).read_text().split(LE, 1)[-1]: dirty = [l for l in dirty if l[3:].strip() != str(hp)]
        if dirty: F.append(("git:dirty", f"{len(dirty)} uncommitted path(s) in {cfg.root.name}: janus commit-state"))
        rc, o, e = sh(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=cfg.root)
        if rc == 0 and o.strip() != "0": F.append(("git:unpushed", f"{o.strip()} unpushed commit(s): janus commit-state pushes them"))
        elif rc: F.append(("git:noupstream", "git has no upstream: add a remote, then janus commit-state (it pushes with --set-upstream)"))
    return F

def backlog_findings(cfg, hs, units):
    b = backlog_cfg(cfg)
    if not b.get("file"): return []
    try: iss = backlog_issues(cfg)
    except Err: return []
    if not iss:
        return [("backlog:import", f"the checklist {b['file']} is not in beads: janus backlog import")]
    F = []
    if (cfg.root / b["file"]).exists() and (cfg.root / b["file"]).stat().st_mtime > cfg.mtime("backlog_imported") + 60:
        F.append(("backlog:stale", f"{b['file']} changed since the last janus backlog import: run it again"))
    cov = unit_items(cfg, units); act = item_actors(cfg); hold = item_holds(cfg)   # rows 29/99: held items neither count nor nag
    todo = [(r[8:], i.get("title", "")[9:]) for r, i in iss.items()
            if i.get("status") in ("open", "in_progress") and "backlog-ready" in (i.get("labels") or []) and r[8:] not in cov
            and act.get(r[8:], "agent") == "agent" and r[8:] not in hold]   # owner and hardware items are never idle backlog (row 54)
    if not todo: nobrief_seen(cfg, 0); return F
    plan, stops = fill_plan(cfg, hs, units)
    if plan:   # janus-fixes8 item 4: how many ready units with briefs fit now
        per = {}
        for x in plan: per[x[3]] = per.get(x[3], 0) + 1
        F.append(("idle-with-backlog", f"{len(plan)} ready backlog unit(s) with briefs would fit now ({', '.join(f'{k}: {v}' for k, v in per.items())}): "
                  + ", ".join(x[0] for x in plan[:6]) + ("..." if len(plan) > 6 else "") + "; run janus fill (janus fill --dry-run shows why it stops there)"))
    cands = fill_candidates(cfg, units); briefed = {x[2] for x in cands}
    held = [x[4] for x in cands if x[4] not in {p[0] for p in plan}]
    if held:   # skill-fix52: briefed units the plan cannot place were counted by no finding; named with fill's own reasons
        F.append(("backlog:held", f"{len(held)} ready backlog unit(s) with briefs do not launch now ({', '.join(held[:6])}{'...' if len(held) > 6 else ''}): "
                  + "; ".join(stops)[:300] + "; janus fill --dry-run shows each"))
    nob = [(i, t) for i, t in todo if i not in briefed]; nobrief_seen(cfg, len(nob))
    if nob and not plan and any("budget" in s for s in stops):
        F.append(("idle-backlog:budget", f"budget-limited: {'; '.join(s for s in stops if 'budget' in s)[:160]}; {len(nob)} ready item(s) wait"))
    if nob:   # skill-fix52: never hidden by the budget finding (it was an elif), nor dropped when no host has room
        # row 227: name the items and the cheap way to get their briefs; drafts that only wait for janus brief accept are named too
        dr = cfg.p("briefs", "briefs") / "drafts"; drafted = [i for i, _ in nob if (dr / f"{i}.md").is_file()]; top = [i for i, _ in nob if i not in drafted][:6]
        rooms = [(n, *room(cfg, n, p, units)) for n, p in hs.items() if p.get("ok")]
        tail = ("; ".join(f"{i} {t[:40]}" for i, t in nob[:6]) + ("..." if len(nob) > 6 else "")
                + (f"; draft their briefs with one cheap unit: janus brief draft {' '.join(top)}" if top else "")
                + (f"; {len(drafted)} draft(s) wait for janus brief accept: {' '.join(drafted[:6])}" if drafted else "")
                + "; an item no agent can do (hardware, the owner) is never agent work: janus backlog set <id> actor=hardware|owner")
        if any(r > 0 for _, r, _ in rooms):
            F.append(("idle-backlog:nobrief", f"{len(nob)} ready backlog item(s) have no unlaunched brief while hosts have room ("
                      + ", ".join(f"{n} {r}" for n, r, _ in rooms if r > 0) + "): " + tail))
        else:   # nothing can launch now, so not urgent; the items are still named, with why each host has no room
            F.append(("backlog:unbriefed", f"{len(nob)} ready backlog item(s) have no unlaunched brief and no host has room now ("
                      + "; ".join(f"{n}: {w}" for n, _, w in rooms)[:200] + "): " + tail))
    return F

def nobrief_seen(cfg, n):
    """Records how many ready items have no brief now (nobrief.json; cleared with the last one). ack reads it: an ack of
    idle-backlog:nobrief hides it once, and another is refused until this count is below the count at that ack (skill-fix52:
    four re-acks hid 63 ready items for hours while not one brief was written)."""
    st = cfg.jget("nobrief.json")
    if st.get("n", 0) != n: cfg.jput("nobrief.json", {**st, "n": n} if n else {})

def landing_findings(cfg, units, hs=None):
    ld = cfg.c.get("landing", {}); mins = ld.get("finished_min", 30); need = ld.get("gates_required", 1)
    pats = ld.get("gate_names", ["{unit}", "land-{unit}", "land-{unit}-*", "{unit}-*"])
    import fnmatch
    res = gate_results(cfg); queued = set()
    for p in (hs or {}).values():
        for t in ((p.get("pueue") or {}).get("tasks") or {}).values():
            lab = own_label(cfg, t) or ""
            if lab.startswith("gate:") and any(k in str(t.get("status")) for k in ("Queued", "Running")): queued.add(lab[5:])
    F = []
    for u in units:
        v = u.get("verdict") or ""
        up = unapplied_patch(cfg, u) if u.get("state") in ("done", "failed", "gone", "idle") else None
        if up:   # row 239: the work of a unit that ran without the repository exists only in its collected patch
            F.append((f"landing:{u['name']}:patch", f"{u['name']}'s collected patch {up} is not applied in {u.get('cwd')}: git -C {u.get('cwd')} apply --3way {up}, "
                      f"commit, then gate and land it; do not close it before (its work exists only in that patch)"))
        if u.get("state") not in ("done", "failed", "gone", "idle") or "DONE" not in v or u.get("kind", "implement") not in ld.get("kinds", ["implement"]): continue
        try: fin = report_path(cfg, u).stat().st_mtime
        except OSError: continue
        age = (now() - fin) / 60
        if age < mins: continue
        names = set(u.get("gates", [])) | {n for n in set(res) | queued if any(fnmatch.fnmatch(n, x.format(unit=u["name"])) for x in pats)}
        if names & queued: continue
        passed = sorted(n for n in names if res.get(n, ("", ""))[1] == "PASS")
        if not names:
            F.append((f"landing:{u['name']}:nogate", f"{u['name']} finished {age:.0f} min ago (VERDICT DONE) with no gate queued: janus gate <name> --unit {u['name']}"))
        elif len(passed) >= need and landed(cfg, u) is False:
            F.append((f"landing:{u['name']}:unlanded", f"{u['name']}: gate(s) {', '.join(passed)} passed, finished {age:.0f} min ago, not landed: land it, then janus close"))
    return F

def known_failure_findings(cfg, names):
    """known-failure:<id>: a known failure no open unit fixes (failures on main are fixed at their root cause, owner rule);
    known-failure:<id>:passes: an entry a gate of the last 24 h ran and passed (remove it)."""
    kf = known_failures(cfg); F = []
    if not kf: return F
    for i, (cause, unit) in kf.items():
        if not kf_active((cause, unit)) or unit not in names:
            F.append((f"known-failure:{i}", f"known failure {i} ({cause or 'no cause'}) has no open unit fixing it at the root"
                      + (f" (unit {unit} is not open)" if kf_active((cause, unit)) else "") + ": launch one, or remove the entry"))
    out = cfg.p("out", "out"); cut = now() - 86400; seen = set()
    for n in gate_results(cfg):
        f = out / n / "passed.txt"
        try:
            if f.stat().st_mtime < cut: continue
        except OSError: continue
        for i in gate_ids(cfg, n, "passed") or []:
            if i in kf and i not in seen:
                seen.add(i); F.append((f"known-failure:{i}:passes", f"known failure {i} passed in gate {n}: remove it from {cfg.c['landing']['known_failures']}"))
    return F

ORCH_WAIT = 3   # watcher passes an unregistered orchestrator (pane present without an agent, or session pending) is tolerated

def orchestrator_findings(cfg):
    """orchestrator:kind: the registered orchestrator's kind differs from janus.toml [orchestrator] kind; orchestrator:unregistered:
    the watcher has not seen the orchestrator it started (or the recorded pane's agent) for ORCH_WAIT passes and starts none."""
    F = []
    w = cfg.jget("orch_wait.json")
    if w.get("passes", 0) >= ORCH_WAIT:
        F.append(("orchestrator:unregistered", f"no orchestrator seen for {w['passes']} watcher passes ({w.get('why')}); the watcher starts no other while it "
                  "may be starting: look at its pane, then run janus register in it, or close the pane (herdr pane close) so the watcher starts a new one"))
    sk = cfg.f("session_kind").read_text().strip() if cfg.f("session_kind").exists() else ""
    if sk and sk != cfg.c.get("orchestrator", {}).get("kind", "claude"):
        F.append(("orchestrator:kind", f"the registered orchestrator is {sk}, but janus.toml [orchestrator] kind is {cfg.c.get('orchestrator', {}).get('kind')}: set it to {sk}"))
    return F

def session_findings(cfg, running, units=()):
    """instructions (owner instructions to classify), handoff (stale notes, or a missing state file), watcher (not loaded).
    An instruction an open intent tracks (janus intent add --instruction <id>) is in progress, not unclassified; it is
    flagged again when that intent goes stale (skill-fix52)."""
    F = []
    try:
        its = intents(cfg); live = {i["instruction"] for i in its if i.get("instruction") and not intent_stale(cfg, i, units)}
        ins = [i for i in Beads(cfg).issues("instruction") if i["id"] not in live]
        if ins: F.append(("instructions", f"{len(ins)} unclassified owner instruction(s) (close as done; bd remember --key <subject>: same subject replaces; "
                          "or, while its work is in progress, janus intent add --instruction <id> \"<what>\"): " + "; ".join(i['title'][:50] for i in ins[:3])))
    except Err: pass
    hp = handoff_path(cfg)
    if not hp.exists():   # item 2: a missing state file is a finding with its fix, never a traceback
        st = cfg.c.get("project", {}).get("state")
        F.append(("handoff:missing", f"{hp} is missing (janus.toml [project] state = {st}): run janus check as the orchestrator (it writes the Live state and "
                  f"the notes skeleton); if the state folder is really {cfg.state.name}, janus migrate rewrites [project] state, or set it by hand"))
    for s in handoff_stale(cfg, running): F.append(("handoff", s))
    if not watcher_loaded(): F.append(("watcher", f"watcher {watcher_service()[1]} ({watcher_service()[0]}) is not running: run install"))
    return F + watch_step_findings(cfg)

STEP_FAILS = 3   # passes in a row before a failing watcher step is a finding (one failure is retried by the next pass)

def watch_step_findings(cfg):
    """watcher:step:<name>: a step of the watcher's pass failed STEP_FAILS passes in a row (watch.watch_step records it and
    carries on with the other steps); it clears when the step works again."""
    return [(f"watcher:step:{n}", f"the watcher's step {n} failed {x['n']} passes in a row ({x.get('err', '?')}); its other steps still run and it is tried "
             f"again every pass. What that step does is not happening: find the cause in {cfg.f('watch.log')}")
            for n, x in sorted(cfg.jget("watch_steps.json").items()) if x.get("n", 0) >= STEP_FAILS]
