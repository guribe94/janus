"""orch watch: the watcher's one pass (launchd/systemd every few minutes): board sync and alerts, check as a writer, tend
parked units, thrash and tidy, cancel orphans, fill, and wake or restart the orchestrator."""
import argparse, os
from orchkit.actions.backlog import cmd_fill
from orchkit.actions.jobs import cancel_job, job_state, orphan_jobs, task_rc, tend_thrash, wrap_token, wrapped_jobs
from orchkit.actions.launch import new_tab, wait_shell
from orchkit.actions.lifecycle import tend_parked
from orchkit.actions.tidy import tend_tidy
from orchkit.board import board, board_alerts, board_dir, board_files
from orchkit.check import run_check
from orchkit.context import Err, ORCH_MARK, WAKE, handoff_stale, herdr, now, session_alive, sh, ts

def cmd_watch(cfg, a):
    log = lambda m: cfg.f("watch.log").open("a").write(f"{ts()} {m}\n")
    if getattr(a, "alert", False): board_alerts(cfg, log); return   # board.sh .notify: a new question or block, at once
    def sync():   # the shipped board only, when there are other hosts (or posts this machine made before it was the board)
        if cfg.paths.get("board") or cfg.run.dry: return
        board_files(cfg)
        if (board_dir(cfg) / ".hosts").read_text().strip() or (board_dir(cfg).parent / "board-outbox" / "outbox.log").is_file(): board(cfg, "sync", timeout=180)
    sync()   # the shipped board: merge the hosts' outboxes (their questions alert at once through .notify) before the check
    F, live, hs, units = run_check(cfg, cycle=False, writer=True)
    board_alerts(cfg, log)
    tend_parked(cfg, hs, units, log)   # park, resume when the jobs finished, wrap up capped runs (orch-fixes9)
    tend_thrash(cfg, hs, units, log)   # shared hosts that thrash: stop and re-queue this project's newest job (fixes10 item 3)
    tend_tidy(cfg, hs, units, log)     # what tidy can reclaim, for check (tidy:<n>, disk:<host>)
    sync()   # push what this pass wrote (peers.md, directions) to every host
    for r, why in orphan_jobs(cfg, hs, units):   # nothing whose owner is gone keeps running or waiting (item 3)
        try: log(f"orphan {r} ({why}): " + "; ".join(cancel_job(cfg, r)))
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
        cfg.f("session").write_text(f"pending:{int(now())}")   # its first hook event claims the registration
        pane, _ = new_tab(cfg, "orchestrator", cfg.root); wait_shell(pane, "orchestrator")
        sh(["herdr", "agent", "start", "orchestrator", "--kind", kind, "--pane", pane, "--timeout", "90000", "--", *args], timeout=100)
        cfg.f("pane").write_text(pane); herdr("agent", "prompt", "orchestrator", ORCH_MARK + "You are the orchestrator. Run orch register, then " + WAKE); return
    st = ag["agent"].get("agent_status")
    if st == "working": return
    tr = cfg.f("transcript").read_text().strip() if cfg.f("transcript").exists() else ""
    big = tr and os.path.exists(tr) and os.path.getsize(tr) / 4 > cfg.reset_tokens
    urgent = [f.key for f in F if f.urgent]   # the registry says which (findings/__init__.py); acked findings are already out of F; informational ones do not hold a reset (L4)
    if big and not urgent and not handoff_stale(cfg, False):
        if cfg.f("reset_asked").exists():
            cfg.f("reset_asked").unlink(); log("context reset: /clear"); herdr("agent", "prompt", pane, "/clear"); return
        cfg.touch("reset_asked"); log("context large: asked for final notes")
        herdr("agent", "prompt", pane, ORCH_MARK + "Context is large: finish the Orchestrator notes in .orch/HANDOFF.md, then stop."); return
    if (overdue or late) and now() - cfg.mtime("last_wake") > 600:
        cfg.touch("last_wake"); log(f"wake: overdue={bool(overdue)} late={late[:3]}"); herdr("agent", "prompt", pane, ORCH_MARK + WAKE)
