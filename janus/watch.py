"""janus watch: the watcher's one pass (launchd/systemd every few minutes): board sync and alerts, check as a writer, tend
parked units, thrash and tidy, cancel orphans, fill, and wake or restart the orchestrator."""
import argparse, os
from janus.actions.backlog import cmd_fill
from janus.actions.jobs import cancel_job, job_state, orphan_jobs, task_rc, tend_thrash, wrap_token, wrapped_jobs
from janus.actions.launch import new_tab, wait_shell
from janus.actions.lifecycle import tend_parked
from janus.actions.tidy import tend_tidy
from janus.board import board, board_alerts, board_dir, board_files
from janus.autopilot import record, tend_autopilot
from janus.check import run_check
from janus.reset import orch_tokens, reset_now
from janus.context import Err, JANUS_MARK, WAKE, WAKE_CLEAR, atomic_write, handoff_stale, herdr, now, session_alive, sh, ts
from janus.setup import heartbeat_beat

def cmd_watch(cfg, a):
    log = lambda m: cfg.f("watch.log").open("a").write(f"{ts()} {m}\n")
    if getattr(a, "alert", False): board_alerts(cfg, log); return   # board.sh .notify: a new question or block, at once
    if getattr(a, "reset", False): reset_now(cfg); return   # the Stop hook's safe-boundary context reset (janus/reset.py)
    try:
        with cfg.writer(wait=120): return watch_pass(cfg, log)   # the whole pass is one writer (panel problem 2)
    except Err as e: log(f"pass skipped: {e}")

def watch_pass(cfg, log):
    def sync():   # the shipped board only, when there are other hosts (or posts this machine made before it was the board)
        if cfg.paths.get("board") or cfg.run.dry: return
        board_files(cfg)
        if (board_dir(cfg) / ".hosts").read_text().strip() or (board_dir(cfg).parent / "board-outbox" / "outbox.log").is_file(): board(cfg, "sync", timeout=180)
    sync()   # the shipped board: merge the hosts' outboxes (their questions alert at once through .notify) before the check
    F, live, hs, units = run_check(cfg, cycle=False, writer=True)
    F = [f for f in F if f.family != "machine"]   # machine state (no project id) never wakes or starts an orchestrator
    heartbeat_beat(cfg)   # the watchdog host sees this pass (inert until heartbeat/ ships and [heartbeat] watchdog is set)
    board_alerts(cfg, log)
    tend_parked(cfg, hs, units, log)   # park, resume when the jobs finished, wrap up capped runs (janus-fixes9)
    tend_autopilot(cfg, hs, units, F, log)   # [autopilot] enabled: the ending policy's fixed-rule actions (janus/autopilot.py)
    tend_thrash(cfg, hs, units, log)   # shared hosts that thrash: stop and re-queue this project's newest job (fixes10 item 3)
    tend_tidy(cfg, hs, units, log)     # what tidy can reclaim, for check (tidy:<n>, disk:<host>)
    sync()   # push what this pass wrote (peers.md, directions) to every host
    for r, why in orphan_jobs(cfg, hs, units):   # nothing whose owner is gone keeps running or waiting (item 3)
        try: d = "; ".join(cancel_job(cfg, r)); log(f"orphan {r} ({why}): {d}"); record(cfg, f"orphan:job:{r}", "cancel", f"{why}: {d}")
        except Err as e: log(f"orphan {r} ({why}): not cancelled: {e}")
    live = {wrap_token(t) for p in hs.values() for t in ((p.get("pueue") or {}).get("tasks") or {}).values() if task_rc(t) is None}
    for f in (cfg.f("runs") / "wrap").glob("*") if (cfg.f("runs") / "wrap").is_dir() else []:   # finished wrappers' records, after a day
        if f.name not in live and now() - f.stat().st_mtime > 86400 and all(job_state(hs, r)[0] in ("done", "gone") for r in wrapped_jobs(cfg, f.name)): f.unlink()
    # [fill] watcher = true (owner autonomy: "launch within capacity"): launch ready backlog units up to capacity, at most every
    # [fill] every_min (default 10) minutes, only when check found something that fits (idle-with-backlog)
    fc = cfg.c.get("fill", {})
    if fc.get("watcher") and any(k == "idle-with-backlog" for k, _ in F) and now() - cfg.mtime("last_fill") > fc.get("every_min", 10) * 60:
        cfg.touch("last_fill")
        try:
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf): cmd_fill(cfg, argparse.Namespace(max=fc.get("max_per_pass")))
            log("fill: " + " | ".join(l.strip() for l in buf.getvalue().splitlines())[:600])
        except Exception as e: log(f"fill failed: {e}")
    running = any(u.get("state") in ("working", "queued") for u in units)
    op = cfg.jget("open.json"); late = [k for k, t in op.items() if now() - t > cfg.deadline]
    overdue = (running or F) and now() - cfg.mtime("last_cycle") > cfg.cycle
    pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    pl = herdr("pane", "list")
    if pl is None: log("herdr did not answer (error or timeout): nothing started or woken"); return
    present = pane in {p.get("pane_id") for p in pl.get("panes", [])}
    ag = herdr("agent", "get", pane) if pane and present else None
    if present and ag is None: log(f"herdr agent get {pane} failed: nothing started"); return
    sess = cfg.f("session").read_text().strip() if cfg.f("session").exists() else ""
    if present and (ag or {}).get("agent"): cfg.f("orch_wait.json").unlink(missing_ok=True)
    elif present or sess.startswith("pending:"):
        # never a second orchestrator next to one that may be starting (panel-eng F2): its pane still exists, or the session it
        # was started for has not registered yet. Logged each pass; check raises orchestrator:unregistered after ORCH_WAIT passes.
        w = cfg.jget("orch_wait.json"); why = f"pane {pane} exists but herdr shows no agent in it" if present else "the started orchestrator has not registered (session pending)"
        cfg.jput("orch_wait.json", {"since": w.get("since") or int(now()), "passes": w.get("passes", 0) + 1, "why": why})
        log(f"orchestrator not seen ({why}): nothing started, pass {w.get('passes', 0) + 1}"); return
    if not present or not (ag or {}).get("agent"):
        # A new orchestrator only when every sign says the registered one is gone (H4, fixes5 item 1): its pane is absent from
        # a successful pane list, its process (pid and start time recorded at register) is not running, and its transcript has
        # not changed for 20 min.
        alive = session_alive(cfg)
        tr = cfg.f("transcript").read_text().strip() if cfg.f("transcript").exists() else ""
        quiet = now() - (os.path.getmtime(tr) if tr and os.path.exists(tr) else 0) > 20 * 60
        if alive or not quiet:
            log(f"pane {pane or '-'} not found, but the registered session is {'still running' if alive else 'writing its transcript'}: nothing started"); return
        if not (F or running): return
        if not any(p.get("enabled") for p in cfg.prov.values()): log("orchestrator gone; no provider enabled"); return
        if now() - cfg.mtime("last_wake") < 600: return
        kind = (cfg.f("session_kind").read_text().strip() if cfg.f("session_kind").exists() else "") or cfg.c.get("orchestrator", {}).get("kind", "claude")
        oc = cfg.c.get("orchestrator", {}); args = oc.get("args", []) if oc.get("kind", "claude") == kind else []
        cfg.touch("last_wake"); log(f"orchestrator gone (pane absent, process ended, transcript quiet): starting a new {kind} one")
        pane, _ = new_tab(cfg, "orchestrator", cfg.root); wait_shell(pane, "orchestrator")
        atomic_write(cfg.f("session"), f"pending:{int(now())}:{pane}")   # only a hook in this pane claims it (panel-eng F5)
        sh(["herdr", "agent", "start", "orchestrator", "--kind", kind, "--pane", pane, "--timeout", "90000", "--", *args], timeout=100)
        atomic_write(cfg.f("pane"), pane); herdr("agent", "prompt", "orchestrator", JANUS_MARK + "You are the orchestrator. Run janus register, then " + WAKE_CLEAR.format(reason="a new session")); return
    st = ag["agent"].get("agent_status")
    if st == "working": return
    tok = orch_tokens(cfg)
    urgent = [f.key for f in F if f.urgent]   # the registry says which (findings/__init__.py); acked findings are already out of F; informational ones do not hold a reset (L4)
    if tok > cfg.reset_tokens:   # the reset itself happens at the next Stop that passes (reset.stop_reset); here: ask for it, or say why not
        why = f"urgent findings {', '.join(urgent[:3])}" if urgent else "notes stale" if handoff_stale(cfg) else "notes asked under 10 min ago" if now() - cfg.mtime("reset_prompted") < 600 else None
        if why: log(f"context reset skipped ({tok} tokens over {cfg.reset_tokens}): {why}")
        else:
            cfg.touch("reset_prompted"); log(f"context large ({tok} tokens): asked for final notes")
            herdr("agent", "prompt", pane, JANUS_MARK + "Context is large: finish the Orchestrator notes in .janus/HANDOFF.md, then end the turn (janus resets the context then)."); return
    if (overdue or late) and now() - cfg.mtime("last_wake") > 600:
        cfg.touch("last_wake"); log(f"wake: overdue={bool(overdue)} late={late[:3]}")
        herdr("agent", "prompt", pane, JANUS_MARK + WAKE.format(reason="cycle overdue" if overdue else "late: " + ", ".join(late[:3])))
