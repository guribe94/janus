"""The watcher's autopilot (owner-approved; on by default in a project with a spending cap, [autopilot] enabled sets it): the fixed-rule actions of
the ending policy (docs/DESIGN.md, "Keeping work moving"), applied on each watcher pass. Each action is recorded once per cause
(.janus/autopilot.json), as a line in .janus/autopilot.log, an ack of its finding with the reason, and a beads comment on the
unit. Never on a unit the orchestrator paused (`janus ack <key naming the unit> hold`); launches go through `janus launch`,
so capacity, budget, caps and the waste pause apply exactly as for the orchestrator. Everything else stays with it.

The always-on parts (resume parked units, wrap up a capped run, cancel orphan jobs) live in tend_parked and the watcher;
they record their actions here too."""
import argparse, contextlib, io
from pathlib import Path
from janus.beads import Beads
from janus.board import board, board_dir, board_questions
from janus.context import Err, now, ts
from janus.memory import memory_read
from janus.project import own_card, own_label
from janus.transcripts import card_fields
from janus.probe import probe
from janus.units import agent_ended, api_out, herdr_agents, park_jobs, report_path, report_text, unit_state

AP = {"enabled": False, "relaunch_max": 2, "host_down_min": 20}

def has_cap(cfg): return any(pv.get("cap_usd") and "ledger" in pv for pv in cfg.prov.values())   # a cap janus enforces

def apc(cfg, k):
    """An [autopilot] setting. `enabled` left unset is OFF (row 225: it was on whenever a provider had a cap, so a project that
    never asked for it had the watcher relaunch and resume its agents); the owner turns the unattended loop on by name."""
    v = cfg.c.get("autopilot", {}).get(k)
    return v if v is not None else AP[k]

def fill_watcher(cfg):
    """Whether the watcher launches ready backlog units ([fill] watcher; unset: as [autopilot] enabled, so off with no setting)."""
    v = cfg.c.get("fill", {}).get("watcher")
    return bool(apc(cfg, "enabled")) if v is None else bool(v)

def unattended_state(cfg):
    """One line for doctor: whether the unattended loop is on, and the setting that says so."""
    ap, fw = bool(apc(cfg, "enabled")), fill_watcher(cfg)
    src = "janus.toml sets it" if cfg.c.get("autopilot", {}).get("enabled") is not None else "no [autopilot] enabled in janus.toml, and unset is off"
    return (f"unattended loop: autopilot {'on' if ap else 'off'}, watcher fill {'on' if fw else 'off'} ({src}): "
            + ("the watcher relaunches a crashed unit and resumes one stopped by its budget" if ap else "nothing relaunches or resumes a unit while no "
               "orchestrator is at work; set [autopilot] enabled = true to turn it on"))

def unattended_findings(cfg, units):
    """unattended:off: a part of the unattended loop is off while there is work it would keep moving (open units; ready backlog
    for the fill), so the orchestrator knows that nothing recovers that work while it is away (budget-follow item 8)."""
    backlog = bool(cfg.c.get("backlog", {}).get("file"))
    off = [k for k, on in (("[autopilot] enabled", apc(cfg, "enabled") or not units), ("[fill] watcher", fill_watcher(cfg) or not backlog)) if not on]
    if not off: return []
    acts = [a for k, a in (("[autopilot] enabled", "relaunch a unit that crashed or resume one stopped by its budget"), ("[fill] watcher", "launch ready backlog units")) if k in off]
    unset = cfg.c.get("autopilot", {}).get("enabled") is None and ("[autopilot] enabled" in off or cfg.c.get("fill", {}).get("watcher") is None)
    why = "janus.toml has no [autopilot] enabled, and unset is off" if unset else "janus.toml turns it off"
    if cfg.c.get("fill", {}).get("watcher") is None: off = list(dict.fromkeys("[autopilot] enabled" if k == "[fill] watcher" else k for k in off))   # an unset fill follows the autopilot
    return [("unattended:off", f"the unattended loop is off ({why}): while no orchestrator is at work the watcher will not {' or '.join(acts)}. To turn it on set "
             f"{' = true and '.join(off)} = true in janus.toml; if the owner wants it off, "
             "ack this with the reason (janus ack unattended:off <reason>)")]

def record(cfg, key, cause, reason, unit=None, ack=None):
    """Record one autopilot action once per (key, cause). False when it was already recorded (do not act again)."""
    rec = cfg.jget("autopilot.json"); done = rec.setdefault("done", {}); k = f"{key}|{cause}"
    if k in done: return False
    done[k] = int(now()); rec["done"] = dict(list(done.items())[-500:]); cfg.jput("autopilot.json", rec)
    with cfg.f("autopilot.log").open("a") as f: f.write(f"{ts()} {key} {cause}: {reason}\n")
    if ack:
        acks = cfg.jget("acks.json"); acks[ack] = {"t": now(), "d": f"autopilot {cause}: {reason}"}; cfg.jput("acks.json", acks)
    if unit and unit.get("id"):
        try: Beads(cfg).comment(unit["id"], f"{ts()} autopilot {cause}: {reason}")
        except Err: pass
    return True

def forget(cfg, key, cause, why, ack=None):
    """Undo a record made before an action that then failed before doing anything (the next pass may try again)."""
    rec = cfg.jget("autopilot.json")
    if rec.get("done", {}).pop(f"{key}|{cause}", None) is None: return
    cfg.jput("autopilot.json", rec)
    if ack:
        acks = cfg.jget("acks.json")
        if acks.pop(ack, None) is not None: cfg.jput("acks.json", acks)
    with cfg.f("autopilot.log").open("a") as f: f.write(f"{ts()} {key} {cause}: not done ({why}); may be tried again\n")

def held(cfg, name):
    """The orchestrator paused this unit: an ack whose key names it, with the decision 'hold'."""
    return any(name in k.split(":") and str((v or {}).get("d", "")).strip().lower().startswith("hold")
               for k, v in cfg.jget("acks.json").items() if isinstance(v, dict))

def quiet(fn, *a):
    """Run a command function, returning what it printed (one line)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): fn(*a)
    return " | ".join(l.strip() for l in buf.getvalue().splitlines() if l.strip())[:300]

def live(cfg, u, hs):
    """Why the unit must not be relaunched now, or None (soak 43). Its state in this watcher pass is the one probed when the
    pass began: a resume started later in the pass (tend_parked, tend_resumable) or by `janus resume` runs under a new task, and
    the unit was judged crashed and duplicated while its own session ran. So the unit record as saved now and its host as it is
    now are asked; a host that cannot say counts as live. A unit whose report or record parks it on a job that has not finished
    (hs, this pass's probes) waits for that job."""
    from janus.actions.jobs import job_state
    try: cur = next((x for x in Beads(cfg).units() if x.get("id") == u.get("id")), None) or u
    except Err: cur = u
    ps = {u["host"]: probe(u["host"], cfg.hosts[u["host"]])}; ag = herdr_agents(cfg) if "herdr" in (u.get("via"), cur.get("via")) else {}
    for x in (u, cur):
        st = unit_state(x, ps, ag)
        if st in ("working", "queued", "blocked", "unknown") or (x.get("via") == "herdr" and st != "gone"):
            return f"its own session is {st} now" + (f" (task {x['task']})" if x.get("task") is not None else "")
    jobs = dict.fromkeys([*((cur.get("parked") or {}).get("jobs") or []), *park_jobs(cfg, report_text(cfg, cur) or "")[1]])
    busy = [j for j in jobs if job_state(hs, j)[0] not in ("done", "gone")]
    return f"it is parked on {', '.join(busy)}, not finished" if busy else None

def relaunch(cfg, u, cause, log, hs=None):
    """Relaunch a unit from its report (a new unit that starts from a copy of that report in its own out folder, same brief and
    tree), bounded by relaunch_max. Never while the unit's own session, a resume of it or a job it is parked on is live."""
    from janus.actions.launch import cmd_launch
    base = u.get("relaunch_of") or u["name"]; k = u.get("relaunches", 0)
    if u.get("relaunched"): return
    why = live(cfg, u, hs)
    if why: log(f"autopilot: {u['name']} looked {cause}, but {why}: not relaunched"); return
    if k >= apc(cfg, "relaunch_max"):
        if record(cfg, f"unit:{u['name']}", "relaunch-limit", f"{cause}, but already relaunched {k} times ([autopilot] relaunch_max): left to the orchestrator", u):
            log(f"autopilot: {u['name']} {cause}: relaunch limit reached")
        return
    new = f"{base[:28]}-r{k + 1}"
    a = argparse.Namespace(unit=new, kind=u.get("kind") or "implement", tier=u.get("tier") or "normal", host=None, model=u.get("model"),
                           brief=u.get("brief"), cwd=u.get("cwd"), budget=None, test_host=None, allow_main=False, of=None,
                           report=None, seed_report=str(report_path(cfg, u)), resume_of=None, item=u.get("item"), share_with=u["name"])
    # once only (panel-eng F1): recorded before the launch, the unit record saved best-effort after
    if not record(cfg, f"unit:{u['name']}", "relaunch", f"{cause}: relaunching from its report as {new}", u, ack=f"unit:{u['name']}:finished"): return
    try: out = quiet(cmd_launch, cfg, a)
    except Err as e:   # no room, budget, waste pause: tried again next pass
        forget(cfg, f"unit:{u['name']}", "relaunch", str(e), ack=f"unit:{u['name']}:finished")
        log(f"autopilot: relaunch of {u['name']} ({cause}) not possible now: {e}"); return
    record(cfg, f"unit:{u['name']}", f"relaunch:{cause}", f"relaunched from its report as {new} ({out})")
    log(f"autopilot: {u['name']} {cause}: relaunched as {new}")
    try:
        bd = Beads(cfg); u["relaunched"] = new; bd.save(u)
        nu = next(x for x in bd.units() if x["name"] == new); nu.update(relaunch_of=base, relaunches=k + 1); bd.save(nu)
    except (Err, StopIteration) as e: log(f"autopilot: {u['name']}: saving the relaunch on the unit records failed ({e})")

def ending(cfg, u):
    """Why a finished unit is relaunched: 'capped' (its wrap-up run ended too), 'crashed' (ended with no verdict), or None."""
    if not agent_ended(u) or u.get("_park") or u.get("parked") or u.get("relaunched") or u.get("via") in ("failed", "starting"): return None
    if u.get("resumable") or u.get("resume_limit"): return None   # budget-follow item 7: resumed in its own session, or left to the orchestrator
    if u.get("wrapup") and u.get("wrapup") == u.get("run") and not str(u.get("verdict") or "").startswith("VERDICT: DONE"): return "capped"
    if u.get("verdict"): return None
    if api_out(cfg, u).get("subtype") == "error_max_budget_usd": return None   # wrap_due: tend_parked wraps it up first
    return "crashed"

def carry_patch(cfg, u, apply=False):
    """Why a remote unit that ended cannot be relaunched like a local one, or None when it can (row 249). A relaunch starts from
    this machine's tree, so the code the unit wrote (out/<unit>/changes.patch, written by janus collect) must be in its worktree
    here: an empty patch (it changed nothing) and a patch already applied are fine; one that applies cleanly is applied when
    apply is set; anything else is left to the orchestrator, with the reason."""
    from janus.context import sh
    p = cfg.p("out", "out") / u["name"] / "changes.patch"; c = Path(u.get("cwd") or "/")
    if not p.is_file(): return f"no collected patch (janus collect {u['name']} writes it)"
    if not p.stat().st_size: return None
    if not c.is_dir() or c.resolve() == cfg.root.resolve(): return f"{c} is not a worktree of its own"
    ap = lambda *x: sh(["git", "-C", str(c), "apply", *x, str(p.resolve())])[0] == 0
    if ap("--check"): return None if not apply or ap() else f"git apply of {p} in {c} failed"
    return None if ap("--check", "-R") else f"its patch does not apply to {c}"

def autopilot_findings(cfg, units):
    """unit:<n>:relaunch: with the autopilot on, a crashed or capped unit of another host whose collected patch cannot be put into
    its worktree here is not relaunched (a relaunch would start without its code): the finding names the patch and the reason."""
    if not apc(cfg, "enabled"): return []
    F = []
    for u in units:
        if cfg.hosts.get(u.get("host"), {}).get("local") or held(cfg, u["name"]): continue
        why = ending(cfg, u); blk = carry_patch(cfg, u) if why else None
        if blk:
            n = u["name"]; p = cfg.p("out", "out") / n / "changes.patch"
            F.append((f"unit:{n}:relaunch", f"{n} {why} on {u.get('host')}; the autopilot did not relaunch it: {blk}. Its changes are kept in "
                      f"{p}: relaunch it with that patch applied first (git apply --3way in the new unit's tree), or close it"))
    return F

def answer_from_memory(cfg, log):
    """An open board question that asks for a MEMORY.md entry explicitly ([key]): answered with it. A key merely mentioned in the
    text is not an answer (panel-eng F7: "may I skip the gate?" is not answered by the gate fact)."""
    es = [e for e in memory_read(cfg) if e[2]]
    for q, ag, _, tx in board_questions(cfg) or []:
        hit = [e for e in es if f"[{e[2]}]" in (tx or "")]
        if len(hit) != 1: continue   # none, or ambiguous: the orchestrator answers
        e = hit[0]; text = f"(janus autopilot, from MEMORY.md [{e[2]}], {e[1]}) {e[3]}"
        if not record(cfg, f"question:{q}", "answer", f"answered from memory [{e[2]}]", ack=f"question:{q}"): continue
        rc, o, err = board(cfg, "answer", str(q), text)
        log(f"autopilot: question {q} from {ag}: answered from [{e[2]}]" + (f" (board failed: {(err or o).strip()[:80]})" if rc else ""))

def spec_of(cfg, hs, ref):
    """(jid, recorded job spec) of a job janus run queued (.janus/runs/jobs/<jid>.json), else (None, None)."""
    import json
    from janus.actions.jobs import job_of
    hn, _, tid = ref.partition(":"); t = (((hs.get(hn) or {}).get("pueue") or {}).get("tasks") or {}).get(tid)
    jid = job_of(t) if t else None
    try: return jid, json.loads((cfg.f("runs") / "jobs" / f"{jid}.json").read_text()) if jid else None
    except (OSError, ValueError): return jid, None

def requeue_stuck(cfg, hs, units, F, log):
    """age: a RUNNING job the check calls stuck (over its lane's age from its start, and no progress for the stall window): cancel
    it (files saved) and re-queue the recorded job on a host with room, once per job; a job already re-queued once is reported,
    not re-queued again. A job that only waits (queued, or held by admission) is left: cancelling it gains nothing."""
    from janus.actions.jobs import cancel_job, job_state, requeue, requeue_host
    for f in F:
        if not f.key.startswith("age:"): continue
        _, hn, tid = f.key.split(":", 2); ref = f"{hn}:{tid}"; jid, spec = spec_of(cfg, hs, ref)
        if not spec or (spec.get("unit") and held(cfg, spec["unit"])) or job_state(hs, ref)[0] != "running": continue
        if spec.get("requeued"):
            record(cfg, f.key, "stuck-again", f"{ref} ({jid}) is stuck again after a re-queue: left to the orchestrator"); continue
        to = requeue_host(cfg, hs, units, hn, spec)
        try: done = cancel_job(cfg, ref); new = requeue(cfg, {**spec, "requeued": ref}, to)
        except Err as e: log(f"autopilot: stuck {ref}: {e}"); continue
        record(cfg, f.key, "requeue", f"stuck {ref} cancelled ({'; '.join(done)[:80]}), re-queued on {to}: {new}", ack=f.key)
        if spec.get("unit"): board(cfg, "direct", spec["unit"], f"janus autopilot: your job {ref} was stuck past its age limit; re-queued as {new}: wait on that id")
        log(f"autopilot: stuck {ref} re-queued on {to}: {new}")

def device_bound(cfg, hn, spec):
    """A job with a device need (--usb, --needs device=<d>, or a bare --needs word that is one of host hn's devices)."""
    from janus.actions.jobs import parse_needs
    from janus.probe import FACTS
    if spec.get("usb"): return True
    try: n = parse_needs(spec.get("needs"))
    except Err: return True
    return bool(n.get("devices") or n.get("features", set()) & set((cfg.jget(FACTS).get(hn) or {}).get("devices") or []))

def move_off_down_hosts(cfg, hs, units, log):
    """A host unreachable past [autopilot] host_down_min: its unstarted jobs (queued or waiting for admission at the last pass
    that saw it) are re-queued elsewhere; running ones and jobs with a device need (--usb, --needs device=...) wait. When the host
    is back, every old copy not done or gone is cancelled (it may have started during a partition: never run twice, panel-eng F3)."""
    from janus.actions.jobs import cancel_job, job_state, requeue, requeue_host
    import json
    st = cfg.jget("autopilot.json"); last = st.get("unstarted", {}); moved = st.get("moved", {}); down = cfg.jget("hosts.json")
    for hn, p in hs.items():
        if p.get("ok"):
            last[hn] = [f"{hn}:{tid}" for tid, t in ((p.get("pueue") or {}).get("tasks") or {}).items()
                        if own_label(cfg, t) is not None and job_state(hs, f"{hn}:{tid}")[0] in ("queued", "admission")]   # own jobs only
            for old in [r for r in moved if r.startswith(hn + ":")]:   # back: the old copy must not run twice
                if job_state(hs, old)[0] not in ("done", "gone"):
                    try: log(f"autopilot: {hn} is back: cancelled the moved job {old}: " + "; ".join(cancel_job(cfg, old)))
                    except Err as e: log(f"autopilot: {hn} is back: {old} not cancelled: {e}"); continue
                moved.pop(old)
            continue
        if now() - (down.get(hn) or now()) < apc(cfg, "host_down_min") * 60: continue
        for ref in last.get(hn, []):
            if ref in moved: continue
            jd = cfg.f("runs") / "jobs"; spec = None
            for f in jd.glob("*.json") if jd.is_dir() else []:
                try: s = json.loads(f.read_text())
                except ValueError: continue
                if s.get("host") == hn and str(s.get("task")) == ref.split(":", 1)[1]: spec = s; break
            if not spec or (spec.get("unit") and held(cfg, spec["unit"])): continue
            if device_bound(cfg, hn, spec): continue   # pinned to its host by nature: it waits for it
            to = requeue_host(cfg, hs, units, hn, spec)
            if to == hn: continue
            try: new = requeue(cfg, spec, to)
            except Err as e: log(f"autopilot: moving {ref} off {hn}: {e}"); continue
            moved[ref] = new; record(cfg, f"host:{hn}", f"move:{ref}", f"{hn} down: unstarted {ref} re-queued on {to}: {new}")
            if spec.get("unit"): board(cfg, "direct", spec["unit"], f"janus autopilot: {hn} is down; your job {ref} was re-queued as {new}: wait on that id")
            log(f"autopilot: {hn} down: {ref} re-queued on {to}: {new}")
    st = cfg.jget("autopilot.json"); st.update(unstarted=last, moved=moved); cfg.jput("autopilot.json", st)

def archive_cards(cfg, units, log):
    """A board card that is not an open unit and whose agent posted done: archived."""
    names = {u["name"] for u in units}; ad = board_dir(cfg) / "agents"
    for c in ad.glob("*.md") if ad.is_dir() and not cfg.paths.get("board") else []:
        cf = card_fields(cfg, c.stem)
        if c.stem in names or cf.get("state") != "done" or not own_card(cfg, cf): continue   # only this project's cards
        rc, o, e = board(cfg, "close", c.stem)
        if not rc and record(cfg, f"board:{c.stem}", "archive", "finished card archived (not an open unit)"): log(f"autopilot: archived the finished card {c.stem}")

def tend_autopilot(cfg, hs, units, F, log):
    """One autopilot pass (the watcher calls it after tend_parked). On with a cap or [autopilot] enabled = true (apc)."""
    if not apc(cfg, "enabled") or cfg.run.dry: return
    from janus.actions.lifecycle import cmd_collect
    for u in units:
        if held(cfg, u["name"]): continue
        try:
            remote = not cfg.hosts.get(u.get("host"), {}).get("local")
            if remote and agent_ended(u) and not u.get("_park") and (u.get("collected_at") or 0) < (u.get("resumed_at") or u.get("started") or 1):
                out = quiet(cmd_collect, cfg, argparse.Namespace(unit=u["name"]))
                u["collected_at"] = int(now()); record(cfg, f"unit:{u['name']}", f"collect:{u.get('run')}", out, u); log(f"autopilot: collected {u['name']}")
            why = ending(cfg, u)
            # row 249: a unit of another host is relaunched like a local one once its collected patch is in its worktree here;
            # only when the patch cannot be carried does the orchestrator decide (F4: never a relaunch without its code)
            blk = carry_patch(cfg, u, apply=True) if why and remote and not u.get("relaunched") else None
            if blk:
                if record(cfg, f"unit:{u['name']}", f"remote-left:{u.get('run')}", f"{why} on {u.get('host')}: not relaunched ({blk}); see the unit:{u['name']}:relaunch finding", u):
                    log(f"autopilot: {u['name']} {why} on {u.get('host')}: left to the orchestrator ({blk})")
            elif why: relaunch(cfg, u, why, log, hs)
        except Err as e: log(f"autopilot: {u['name']}: {e}")
    for step in (lambda: answer_from_memory(cfg, log), lambda: requeue_stuck(cfg, hs, units, F, log),
                 lambda: move_off_down_hosts(cfg, hs, units, log), lambda: archive_cards(cfg, units, log)):
        try: step()
        except Err as e: log(f"autopilot: {e}")
