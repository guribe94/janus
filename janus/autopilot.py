"""The watcher's autopilot (owner-approved; opt-in with [autopilot] enabled = true, off by default): the fixed-rule actions of
the ending policy (docs/DESIGN.md, "Keeping work moving"), applied on each watcher pass. Each action is recorded once per cause
(.janus/autopilot.json), as a line in .janus/autopilot.log, an ack of its finding with the reason, and a beads comment on the
unit. Never on a unit the orchestrator paused (`janus ack <key naming the unit> hold`); launches go through `janus launch`,
so capacity, budget, caps and the waste pause apply exactly as for the orchestrator. Everything else stays with it.

The always-on parts (resume parked units, wrap up a capped run, cancel orphan jobs) live in tend_parked and the watcher;
they record their actions here too."""
import argparse, contextlib, io, re
from janus.beads import Beads
from janus.board import board, board_dir, board_questions
from janus.context import Err, now, ts
from janus.memory import memory_read
from janus.transcripts import card_fields
from janus.units import agent_ended, api_out, report_path

AP = {"enabled": False, "relaunch_max": 2, "host_down_min": 20}

def apc(cfg, k): return cfg.c.get("autopilot", {}).get(k, AP[k])

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

def relaunch(cfg, u, cause, log):
    """Relaunch a unit from its report (a new unit continuing the same report, same brief and tree), bounded by relaunch_max."""
    from janus.actions.launch import cmd_launch
    base = u.get("relaunch_of") or u["name"]; k = u.get("relaunches", 0)
    if u.get("relaunched"): return
    if k >= apc(cfg, "relaunch_max"):
        if record(cfg, f"unit:{u['name']}", "relaunch-limit", f"{cause}, but already relaunched {k} times ([autopilot] relaunch_max): left to the orchestrator", u):
            log(f"autopilot: {u['name']} {cause}: relaunch limit reached")
        return
    new = f"{base[:28]}-r{k + 1}"
    a = argparse.Namespace(unit=new, kind=u.get("kind") or "implement", tier=u.get("tier") or "normal", host=None, model=u.get("model"),
                           brief=u.get("brief"), cwd=u.get("cwd"), budget=None, test_host=None, allow_main=False, of=None,
                           report=str(report_path(cfg, u)), resume_of=None, item=u.get("item"), share_with=u["name"])
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
    if u.get("wrapup") and u.get("wrapup") == u.get("run") and not str(u.get("verdict") or "").startswith("VERDICT: DONE"): return "capped"
    if u.get("verdict"): return None
    if api_out(cfg, u).get("subtype") == "error_max_budget_usd": return None   # wrap_due: tend_parked wraps it up first
    return "crashed"

def answer_from_memory(cfg, log):
    """An open board question that names a MEMORY.md subject exactly ([key] or the key as a whole word): answered with it."""
    es = [e for e in memory_read(cfg) if e[2]]
    for q, ag, _, tx in board_questions(cfg) or []:
        hit = [e for e in es if f"[{e[2]}]" in (tx or "") or re.search(rf"(^|-){re.escape(e[2])}($|-)", re.sub(r"[^a-z0-9]+", "-", (tx or "").lower()))]
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
    """age: a job over its class age limit: cancel it (files saved) and re-queue the recorded job on a host with room, once per
    job; a job already re-queued once is reported, not re-queued again."""
    from janus.actions.jobs import cancel_job, requeue, requeue_host
    for f in F:
        if not f.key.startswith("age:"): continue
        _, hn, tid = f.key.split(":", 2); ref = f"{hn}:{tid}"; jid, spec = spec_of(cfg, hs, ref)
        if not spec or (spec.get("unit") and held(cfg, spec["unit"])): continue
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
            last[hn] = [r for r in (f"{hn}:{tid}" for tid in ((p.get("pueue") or {}).get("tasks") or {})) if job_state(hs, r)[0] in ("queued", "admission")]
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
        if c.stem in names or card_fields(cfg, c.stem).get("state") != "done": continue
        rc, o, e = board(cfg, "close", c.stem)
        if not rc and record(cfg, f"board:{c.stem}", "archive", "finished card archived (not an open unit)"): log(f"autopilot: archived the finished card {c.stem}")

def tend_autopilot(cfg, hs, units, F, log):
    """One autopilot pass (the watcher calls it after tend_parked). Off unless [autopilot] enabled = true."""
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
            if why: relaunch(cfg, u, why, log)
        except Err as e: log(f"autopilot: {u['name']}: {e}")
    for step in (lambda: answer_from_memory(cfg, log), lambda: requeue_stuck(cfg, hs, units, F, log),
                 lambda: move_off_down_hosts(cfg, hs, units, log), lambda: archive_cards(cfg, units, log)):
        try: step()
        except Err as e: log(f"autopilot: {e}")
