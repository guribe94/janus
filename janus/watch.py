"""janus watch: the watcher's one pass (launchd/systemd every few minutes): board sync and alerts, check as a writer, resume
parked units whose jobs finished, thrash and tidy, cancel orphans, fill, and wake or restart the orchestrator. Each step is
guarded (watch_step): one that fails is logged and counted, the others still run, and the next pass tries it again. The
orchestrator is woken when the set of urgent observations CHANGES, never repeatedly for the same ones."""
import argparse, os, threading, traceback
from janus.actions.backlog import cmd_fill
from janus.actions.jobs import cancel_job, orphan_jobs, tend_thrash
from janus.actions.launch import new_tab, wait_shell
from janus.actions.lifecycle import tend_parked
from janus.actions.tidy import tend_tidy
from janus.board import board, board_alerts, board_dir, board_files
from janus.check import read_remote, run_check
from janus.reset import reset_now
from janus.context import CLAIM_WINDOW, Err, JANUS_MARK, LockLost, writer_lock, atomic_write, herdr, now, pending_age, session_alive, sh, ts, wake, wake_clear
from janus.setup import heartbeat_beat

def cmd_watch(cfg, a):
    log = lambda m: cfg.f("watch.log").open("a").write(f"{ts()} {m}\n")
    if getattr(a, "alert", False): board_alerts(cfg, log); return   # board.sh .notify: a new question or block, at once
    if getattr(a, "reset", False): return 0 if reset_now(cfg) else 1   # the Stop hook's safe-boundary context reset (janus/reset.py)
    watch_step(cfg, log, "sync", board_sync, cfg)   # first, before the writer lock: the hosts' posts are merged (their questions alert at once through .notify) before the check
    stop = sync_ticker(cfg, log)
    try:
        with writer_lock(cfg.f("watch.lock"), wait=0):   # one pass at a time (a second one is skipped): the project writer lock below is let go for host I/O, and two passes must not both resume or re-queue meanwhile
            cfg.run.ro = False; pre = read_remote(cfg)   # the probes and the remote outputs, before the lock: a slow host must not block an ack (check.read_remote)
            with cfg.writer(wait=120):   # short state work only: every step below lets go of it for its host I/O (cfg.unlocked)
                t0 = now(); STEP_S.clear()
                try: return watch_pass(cfg, log, pre)
                finally:   # skill-fix57: how long the pass held the writer lock and on what (check: watcher:lock)
                    slow = dict(sorted(((n, round(s)) for n, s in STEP_S.items() if s >= 5), key=lambda x: -x[1])[:4])
                    if not cfg.run.dry: cfg.jput("watch_lock.json", {"t": int(now()), "held": round(now() - t0), "slow": slow})
                    if now() - t0 >= 60: log(f"pass held the writer lock {now() - t0:.0f} s; slowest: " + (", ".join(f"{n} {s} s" for n, s in slow.items()) or "none over 5 s"))
    except Err as e: log(f"pass skipped: {e}")
    except Exception as e:   # never a silent death (budget-follow item 8): the cause is in watch.log, and the next pass runs
        log(f"pass failed: {type(e).__name__}: {e} | " + " | ".join(traceback.format_exc().strip().splitlines()[-3:-1])[:400])
    finally: stop.set()

def board_sync(cfg):
    """Merge the hosts' outboxes into the board and push its copy to them. The shipped board only, when there are other hosts
    (or posts this machine made before it was the board). Needs no writer lock: the board script has its own locks."""
    if cfg.paths.get("board") or cfg.run.dry: return
    board_files(cfg)
    if (board_dir(cfg) / ".hosts").read_text().strip() or (board_dir(cfg).parent / "board-outbox" / "outbox.log").is_file(): board(cfg, "sync", timeout=180)

SYNC_EVERY = 60   # seconds between board syncs while a pass waits for the writer lock or runs

def sync_ticker(cfg, log, every=SYNC_EVERY):
    """janus owns the board sync (issue 266): a pass can wait two minutes for the writer lock and then run for minutes (a check
    over slow hosts), and the board was merged only at its two ends, so a remote agent's question sat in its outbox that long.
    cmd_watch syncs first; this thread syncs again every minute until the pass is over (set the returned event); the installed
    watcher starts a pass every two minutes, so a post waits about two minutes at most. A failure is logged; the next tick
    tries again (the pass's own sync steps are the ones counted for watcher:step:sync)."""
    stop = threading.Event()
    def loop():
        while not stop.wait(every):
            try: board_sync(cfg)
            except Exception as e: log(f"board sync failed: {type(e).__name__}: {e}"[:300])
    threading.Thread(target=loop, daemon=True).start()
    return stop

STEP_S = {}   # seconds each step of this pass took (cmd_watch reports the slow ones)

def watch_step(cfg, log, name, fn, *a):
    """Run one step of the pass. Any failure is logged and counted in .janus/watch_steps.json (check: watcher:step:<name> after
    three passes in a row), and the pass goes on: one bad unit record or host cannot stop the resumes, the relaunches, the fill
    or the orchestrator's wake (budget-follow item 8)."""
    t0 = now()
    try: r = fn(*a)
    except LockLost: raise   # the writer lock could not be retaken after host I/O: the pass ends here, no later step runs without it
    except Exception as e:
        STEP_S[name] = STEP_S.get(name, 0) + now() - t0
        err = f"{type(e).__name__}: {e}"[:300]
        if not cfg.run.dry:
            rec = cfg.jget("watch_steps.json"); rec[name] = {"n": (rec.get(name) or {}).get("n", 0) + 1, "err": err, "t": int(now())}; cfg.jput("watch_steps.json", rec)
        log(f"step {name} failed, the pass goes on: {err}"); return None
    STEP_S[name] = STEP_S.get(name, 0) + now() - t0
    rec = cfg.jget("watch_steps.json")
    if name in rec and not cfg.run.dry: rec.pop(name); cfg.jput("watch_steps.json", rec); log(f"step {name} works again")
    return r

def outside(cfg, fn, *a):
    """fn with the project writer lock let go (host or herdr I/O): the pass retakes it for the next short state step."""
    with cfg.unlocked(): return fn(*a)

def watch_pass(cfg, log, pre=None):
    step = lambda name, fn, *a: watch_step(cfg, log, name, fn, *a)
    H = lambda *a: outside(cfg, herdr, *a)   # herdr's own socket and its waits: never under the writer lock
    t0 = now(); F, live, hs, units = run_check(cfg, cycle=False, writer=True, pre=pre); STEP_S["check"] = now() - t0
    F = [f for f in F if f.family != "machine"]   # machine state (no project id) never wakes or starts an orchestrator
    step("heartbeat_beat", outside, cfg, heartbeat_beat, cfg)   # the watchdog host sees this pass (inert until heartbeat/ ships and [heartbeat] watchdog is set)
    step("board_alerts", board_alerts, cfg, log)
    step("tend_parked", tend_parked, cfg, hs, units, log, pre[1] if pre else None)   # park, and resume when the jobs finished
    step("tend_thrash", tend_thrash, cfg, hs, units, log)   # shared hosts that thrash: stop and re-queue this project's newest job
    step("tend_tidy", tend_tidy, cfg, hs, units, log)     # what tidy can reclaim, for check (tidy:<n>, disk:<host>)
    step("sync", outside, cfg, board_sync, cfg)   # push what this pass wrote to every host
    for r, why in step("orphan_jobs", orphan_jobs, cfg, hs, units) or []:   # nothing whose owner is gone keeps running or waiting
        try:
            with cfg.unlocked(): d = "; ".join(cancel_job(cfg, r))   # the host's kill and file copy: not under the writer lock
            log(f"orphan {r} ({why}): {d}")
        except Err as e: log(f"orphan {r} ({why}): not cancelled: {e}")
    # [fill] watcher = true (the owner's authorization): launch ready backlog units with briefs up to capacity, at most every
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
    pane = cfg.f("pane").read_text().strip() if cfg.f("pane").exists() else ""
    pl = H("pane", "list")
    if pl is None: log("herdr did not answer (error or timeout): nothing started or woken"); return
    present = pane in {p.get("pane_id") for p in pl.get("panes", [])}
    ag = H("agent", "get", pane) if pane and present else None
    if present and ag is None: log(f"herdr agent get {pane} failed: nothing started"); return
    sess = cfg.f("session").read_text().strip() if cfg.f("session").exists() else ""
    age = pending_age(sess); fresh = age is not None and age < CLAIM_WINDOW
    alive = session_alive(cfg)
    tr = cfg.f("transcript").read_text().strip() if cfg.f("transcript").exists() else ""
    quiet = now() - (os.path.getmtime(tr) if tr and os.path.exists(tr) else 0) > 20 * 60
    w = cfg.jget("orch_wait.json")
    if present and (ag or {}).get("agent"): cfg.f("orch_wait.json").unlink(missing_ok=True)
    elif present and not fresh and not alive and quiet and now() - (w.get("since") or now()) >= 20 * 60:
        # self-healing (re-check N1): the recorded pane has shown no agent for 20 min, nothing can claim a registration, the
        # registered process ended and its transcript is quiet: the agent exited and left its shell. Close the pane, then the
        # normal restart below.
        log(f"pane {pane} has had no agent for {int((now() - w['since']) / 60)} min, process ended, transcript quiet: closing it")
        if H("pane", "close", pane) is None: log(f"herdr pane close {pane} failed: nothing started"); return
        cfg.f("orch_wait.json").unlink(missing_ok=True); present = False
    elif present or fresh:
        # never a second orchestrator next to one that may be starting (panel-eng F2): its pane still exists, or the session it
        # was started for can still claim the registration (CLAIM_WINDOW). Logged each pass; check raises orchestrator:unregistered
        # after ORCH_WAIT passes.
        why = f"pane {pane} exists but herdr shows no agent in it" if present else "the started orchestrator has not registered (session pending)"
        cfg.jput("orch_wait.json", {"since": w.get("since") or int(now()), "passes": w.get("passes", 0) + 1, "why": why})
        log(f"orchestrator not seen ({why}): nothing started, pass {w.get('passes', 0) + 1}"); return
    if not present or not (ag or {}).get("agent"):
        # A new orchestrator only when every sign says the registered one is gone (H4, fixes5 item 1): its pane is absent from
        # a successful pane list, its process (pid and start time recorded at register) is not running, and its transcript has
        # not changed for 20 min.
        if alive or not quiet:
            log(f"pane {pane or '-'} not found, but the registered session is {'still running' if alive else 'writing its transcript'}: nothing started"); return
        if not (F or running): return
        if not any(p.get("enabled") for p in cfg.prov.values()): log("orchestrator gone; no provider enabled"); return
        if now() - cfg.mtime("last_wake") < 600: return
        kind = (cfg.f("session_kind").read_text().strip() if cfg.f("session_kind").exists() else "") or cfg.c.get("orchestrator", {}).get("kind", "claude")
        oc = cfg.c.get("orchestrator", {}); args = oc.get("args", []) if oc.get("kind", "claude") == kind else []
        cfg.touch("last_wake"); log(f"orchestrator gone (pane absent, process ended, transcript quiet): starting a new {kind} one")
        with cfg.unlocked():   # the new tab, the shell's wait (seconds) and the agent's start (up to 100 s): the claim (last_wake) is already recorded
            pane, _ = new_tab(cfg, "orchestrator", cfg.root); wait_shell(pane, "orchestrator")
            atomic_write(cfg.f("session"), f"pending:{int(now())}:{pane}")   # only a hook in this pane claims it (panel-eng F5)
            sh(["herdr", "agent", "start", "orchestrator", "--kind", kind, "--pane", pane, "--timeout", "90000", "--", *args], timeout=100)
            atomic_write(cfg.f("pane"), pane); herdr("agent", "prompt", "orchestrator", JANUS_MARK + "You are the orchestrator. Run janus register, then " + wake_clear(cfg, "a new session"))
        return
    if ag["agent"].get("agent_status") == "working": return
    wake_on_change(cfg, log, pane, F)

def wake_on_change(cfg, log, pane, F):
    """Prompt the idle orchestrator when the set of urgent observations changed (a new one appeared), once per change; the same
    set again is not a reason, and a set that cleared resets the memory so a recurrence is new. At least 10 min apart."""
    keys = sorted(f.key for f in F if f.urgent); seen = set(cfg.jget("woken.json").get("keys", []))
    new = [k for k in keys if k not in seen]; due = bool(new) and now() - cfg.mtime("last_wake") > 600
    if not cfg.run.dry: cfg.jput("woken.json", {"keys": keys if due or not new else sorted(seen & set(keys))})   # a held-back wake stays pending
    if due:
        cfg.touch("last_wake"); log(f"wake: new {new[:3]}")
        with cfg.unlocked(): herdr("agent", "prompt", pane, JANUS_MARK + wake(cfg, "changed: " + ", ".join(new[:3])))
