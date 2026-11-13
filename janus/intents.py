"""Intents: what the orchestrator means to get done and has not finished (janus intent add|done|list).

An intent is a beads issue labelled `intent` whose notes hold its record: what, why, done (what done means), instruction (the
owner instruction it answers) and units (the units that work on it), with t, the time it last moved. Every check prints the
open ones in the Live state of HANDOFF.md, so the session after a context clear sees them without reading notes, and raises
intent:<id>:stale when no linked unit runs and nothing moved for [intents] stale_h hours (default 2)."""
import calendar, json, time
from janus.beads import Beads
from janus.context import Err, now

LABEL = "intent"
RESTING = ("done", "failed", "gone", "idle")   # a unit in one of these states is not working on anything

def moved_at(i, rec):
    """When an intent last moved (epoch seconds): the t of its record (janus intent add sets it on create and on every update),
    else the issue's own updated_at, else created_at, else now. ISSUES row 94: an intent made by `bd create -l intent`, or whose
    notes were edited by hand, has no t; it counted from epoch 0 and check said 'has not moved for 498494 h'."""
    t = rec.get("t")
    if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0: return t
    for k in ("updated_at", "created_at"):
        try: return calendar.timegm(time.strptime(str(i.get(k) or "")[:19], "%Y-%m-%dT%H:%M:%S"))   # bd writes UTC (...Z)
        except ValueError: pass
    return now()

def intents(cfg):
    """The open intents: [{id, what, why, done, instruction, units, t}], oldest first."""
    out = []
    for i in Beads(cfg).issues(LABEL, "open,in_progress"):
        try: rec = json.loads(i.get("notes") or "{}")
        except ValueError: rec = {}
        if not isinstance(rec, dict): rec = {}
        out.append({"what": str(i.get("title", "")).removeprefix("intent: "), "units": [], **rec, "t": moved_at(i, rec), "id": i["id"]})
    return sorted(out, key=lambda x: x["t"])

def intent_stale(cfg, it, units):
    """Hours since the intent last moved when it is stale (no linked unit is running and that is over [intents] stale_h), else 0."""
    if any(u.get("name") in it["units"] and u.get("state") not in RESTING for u in units): return 0
    h = (now() - it["t"]) / 3600
    return h if h > cfg.c.get("intents", {}).get("stale_h", 2) else 0

def intent_line(it):
    return (f"{it['id']} | {it['what']}" + (f" | why: {it['why']}" if it.get("why") else "") + (f" | done means: {it['done']}" if it.get("done") else "")
            + (f" | instruction {it['instruction']}" if it.get("instruction") else "") + f" | units: {', '.join(it['units']) or 'none'}")

def intent_lines(cfg):
    """The Live state's Intents block ([] when there is none, or beads cannot be read: check reports that itself)."""
    try: its = intents(cfg)
    except Err: return []
    return ["Intents: (open; janus intent done <id> when one is finished)"] + ["- " + intent_line(i) for i in its] if its else []

def intent_findings(cfg, units):
    """intent:<id>:stale for every open intent nothing works on."""
    try: its = intents(cfg)
    except Err: return []
    return [(f"intent:{i['id']}:stale", f"intent {i['id']} ({i['what'][:60]}) has no running unit and has not moved for {h:.1f} h: launch its next unit and link it "
             f"(janus intent add --id {i['id']} --unit <unit>), or janus intent done {i['id']} when it is finished or dropped")
            for i in its for h in [intent_stale(cfg, i, units)] if h]

def cmd_intent(cfg, a):
    """janus intent add "<what>" [--why W] [--done-means D] [--instruction <id>] [--unit U ...] (with --id <intent>: update that
    one; any update moves it) | done <id> [<reason>] | list."""
    bd = Beads(cfg)
    if a.action == "list": return 0, [intent_line(i) for i in intents(cfg)] or ["(no open intents)"]
    if a.action == "done":
        if not a.args: raise Err("janus intent done <id> [<reason>]")
        bd.run("close", a.args[0], "--reason", " ".join(a.args[1:]) or "done"); return 0, [f"intent {a.args[0]} closed"]
    what = " ".join(a.args).strip(); old = {}
    if a.id:
        old = next((i for i in intents(cfg) if i["id"] == a.id), None)
        if old is None: raise Err(f"no open intent {a.id} (janus intent list)")
    elif not what: raise Err('janus intent add "<what>" [--why W] [--done-means D] [--instruction <id>] [--unit U ...] [--id <intent to update>]')
    rec = {"what": what or old.get("what", ""), "why": a.why or old.get("why", ""), "done": a.done_means or old.get("done", ""),
           "instruction": a.instruction or old.get("instruction", ""), "units": list(dict.fromkeys([*old.get("units", []), *(a.unit or [])])), "t": now()}
    i = a.id or bd.create("intent: " + rec["what"][:80], LABEL, rec["why"])
    bd.run("update", i, "--notes", json.dumps(rec))
    return 0, [("updated " if a.id else "added ") + intent_line({**rec, "id": i})]

__all__ = ["intents", "intent_stale", "intent_line", "intent_lines", "intent_findings", "cmd_intent"]
