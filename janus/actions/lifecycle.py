"""A unit's life after launch: janus stop, collect, close, park, resume, board-close; saving its output, task log and
transcript; the watcher's tend_parked (resume a parked unit when its jobs finished, wrap up a capped run)."""
import os, re, shlex, sys, time
from pathlib import Path
from janus.actions.jobs import collect_job, job_of, job_state, runs_dir, save_task_log, task_rc, task_status, unit_jobs
from janus.actions.launch import api_remote, confirm_started, cwd_taken
from janus.autopilot import forget, record
from janus.beads import Beads
from janus.board import board, board_dir, board_script
from janus.budgets import reload_usd, session_usage, bset
from janus.context import (DIFF_X, Err, JANUS_MARK, JANUS_SELF, SSH, X, atomic_write, herdr, hrun, note_event, now, pq, ps_table, qp, say,
    sh, ts)
from janus.ledger import keep_run_json, ledger_done, reserve_budget, run_cost, settle_run
from janus.project import project_id, tag, unit_tmux
from janus.probe import probe, probe_all, pueue
from janus.templates import FINGERPRINT, UNIT_PROCS
from janus.transcripts import card_fields, read_transcript, session_of
from janus.units import (ENDED, api_out, get_unit, herdr_agents, own_tree, report_path, unit_runs, unit_spent, unit_state,
    verdict, waiting_line_jobs)

# Agents never pay to wait. An agent submits its builds and tests detached (`janus run --detach`, or its lane runner's
# `--detach`), which prints each job id (<host>:<task>) at once, writes `WAITING <job ids>` in its report and ends its turn.
# The unit is then parked on those jobs (or `janus park <unit>` parks a live agent: its turn is stopped, its jobs keep running).
# When every job has finished, the watcher resumes the same session with each job's exit code and log path: API units get a
# new run (`claude -p --resume <session id>`, or the provider's resume_cmd, e.g. `codex exec resume`) with its own ledger
# rows and the rest of the unit's budget; interactive agents get a prompt (herdr) or the text typed in (tmux). A run that
# ends at its budget cap without a final verdict is resumed once with [budget] wrapup_usd (default 0.5) to write its report.

def cmd_board_close(cfg, a):
    """Archive a board card that is not an open unit (cards from other launchers); an open unit is closed with janus close."""
    try: open_units = {u["name"] for u in Beads(cfg).units()}
    except Err as e: raise Err(f"beads unavailable, so it cannot tell whether {a.agent} is an open unit: {e}")
    if a.agent in open_units: raise Err(f"{a.agent} is an open unit: use janus close {a.agent}")
    if cfg.run.dry: say(f"+ board.sh close {a.agent}"); return
    rc, o, e = board(cfg, "close", a.agent)
    if rc: raise Err(f"board.sh close {a.agent}: {(e or o).strip()[:120]}")
    say(f"board card {a.agent} archived")

# Every process of a unit: the tree under its task or pane, every process group in it, and any process anywhere whose
# environment has JANUS_UNIT=<unit> (detached children: nohup, setsid, & disown). Printed as "pid pgid" lines (M12).
def mac_env_pids(unit):
    """pids whose environment has JANUS_UNIT=<unit>. macOS ps -E appends the environment to the command line, so the command
    (from plain ps) is cut off first: a command line that merely mentions the variable never matches."""
    tab = lambda *f: {int(l.split(None, 1)[0]): (l.split(None, 1) + [""])[1] for l in sh(["ps", "-Aww", *f, "-o", "pid=,command="], timeout=10)[1].splitlines() if l.strip()}
    P, E = tab(), tab("-E")
    return [p for p, c in E.items() if p in P and c.startswith(P[p]) and f"JANUS_UNIT={unit}" in c[len(P[p]):].split()]

def unit_procs(h, unit, roots=""):
    if h.get("local"): roots = " ".join([roots, *map(str, mac_env_pids(unit))])
    rc, o, e = hrun(h, UNIT_PROCS % {"unit": shlex.quote(unit), "roots": roots}, timeout=30)
    return [tuple(map(int, l.split())) for l in o.splitlines() if re.fullmatch(r"\d+ \d+", l.strip())]

def kill_unit(h, unit, roots=""):
    """TERM every process group of the unit, then KILL what is left after 5 s. Never the caller's or pueued's group."""
    ps = unit_procs(h, unit, roots)
    if not ps: return 0
    t = ps_table() if h.get("local") else {}
    safe = {os.getpgid(0)} | {g for p, (pp, g, s, c) in t.items() if "pueued" in c}
    groups = sorted({g for p, g in ps if g > 1 and g not in safe}); pids = sorted({p for p, g in ps})
    for sig in ("TERM", "KILL"):
        hrun(h, f"kill -{sig} {' '.join('-' + str(g) for g in groups)} {' '.join(map(str, pids))} 2>/dev/null; sleep {5 if sig == 'TERM' else 1}; true", timeout=30)
        ps = unit_procs(h, unit, roots)
        if not ps: return 0
        pids = sorted({p for p, g in ps}); groups = sorted({g for p, g in ps if g > 1 and g not in safe})
    return len(ps)

def task_pid_roots(h, u):
    """Root processes of a unit started before JANUS_UNIT was set: its pueue task's process (children of pueued whose command
    names the unit), or its tmux/herdr pane process."""
    if u.get("via") in ("herdr", "tmux") and u.get("pid"): return str(u["pid"])
    rc, o, e = hrun(h, "ps -A -o pid=,ppid=,command= | awk -v u=" + shlex.quote(u["name"]) + " '$3 ~ /pueued$/ {d[$1]} {p[$1] = $2; for (i = 3; i <= NF; i++) if ($i == u) m[$1]} "
                       "END {for (x in m) if (p[x] in d) print x}'")
    return " ".join(o.split())

def cmd_stop(cfg, a):
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]; via = u.get("via"); p = pueue(h)
    why = None if a.force or cfg.run.dry else uncollected(cfg, u)
    if why: raise Err(f"refused: {u['name']}: {why}")
    say(f"stop {u['name']} ({via} on {u['host']})")
    roots = task_pid_roots(h, u) if not cfg.run.dry else ""
    if cfg.run.dry: say(f"+ pueue kill / tmux kill-session; TERM then KILL every process group of {u['name']} (roots {roots or '-'}, env JANUS_UNIT)"); return
    if via == "pueue": hrun(h, f"{p} kill {int(u['task'])}")
    elif via == "tmux": hrun(h, f"tmux kill-session -t {shlex.quote(unit_tmux(u))}")
    # its lane jobs (label unit:<name>) belong to it too
    hrun(h, f"{p} status --json | python3 -c \"import json,sys; [print(i) for i, t in json.load(sys.stdin)['tasks'].items() if t.get('label') == '{tag(cfg, 'unit:' + u['name'])}' and ('Running' in str(t['status']) or 'Queued' in str(t['status']))]\" | xargs -r {p} kill 2>/dev/null; true")
    left = kill_unit(h, u["name"], roots)
    if left: raise Err(f"{u['name']}: {left} process(es) still alive after TERM and KILL")
    for _ in range(10):
        ps = probe(u["host"], h)
        if ps["ok"] and unit_state(u, {u["host"]: ps}, herdr_agents(cfg)) in ("gone", "done", "failed", "idle", "unknown"): break
        time.sleep(1)
    else: raise Err(f"{u['name']} is still alive after stop")
    if u.get("pane") and via != "herdr": herdr("pane", "close", u["pane"])
    if h.get("local"): settle_run(cfg, u, "stopped")   # remote: collect brings back the cost first, close settles the rest
    bd.comment(u["id"], f"{ts()} stopped (every process group of the unit ended)"); say("stopped: no process of the unit is left")

# Item 29: `diff -ruN | sed` wrote a new file as `--- a/<f>` with an epoch date and no `new file mode`, so `git apply` refused
# the whole patch ("does not exist in index"); a host without `patch` (linux1) could not take it either, and a new EMPTY file
# never appeared in the diff at all. This builds a patch git apply takes, from the two trees, in the host's own python3: a
# `diff --git` header per file, `new file mode` / `deleted file mode`, `/dev/null` on the missing side, and no dependency on
# the host's diff. Excludes are the same names as DIFF_X.
PATCH_PY = '''import os, sys, difflib
n, base, wt = sys.argv[1], sys.argv[2], sys.argv[3]
SKIP = {"target", ".venv", "node_modules", "__pycache__", ".git"}
def rels(root):
    s = set()
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP]
        for f in files: s.add(os.path.relpath(os.path.join(d, f), root))
    return s
A, B = rels(base), rels(wt)
out = []
for r in sorted(A | B):
    anew, bnew = r not in A, r not in B
    a = open(os.path.join(base, r), "rb").read() if not anew else b""
    b = open(os.path.join(wt, r), "rb").read() if not bnew else b""
    if a == b and not (anew or bnew): continue
    if b"\\0" in a or b"\\0" in b: continue
    L = list(difflib.unified_diff(a.decode(errors="replace").splitlines(True), b.decode(errors="replace").splitlines(True),
                                  fromfile="/dev/null" if anew else "a/" + r, tofile="/dev/null" if bnew else "b/" + r, n=3))
    if not L and not (anew or bnew): continue
    out.append("diff --git a/%s b/%s\\n" % (r, r))
    if anew: out.append("new file mode 100644\\n")
    elif bnew: out.append("deleted file mode 100644\\n")
    out.extend(L)
sys.stdout.write("".join(out))
'''

def remote_patch(cfg, h, n, fname):
    """Write out/<unit>/<fname> on host h: the agent's changes as a patch against the tree it was sent (base/<unit>), in a
    form `git apply` accepts (new and deleted files included; see PATCH_PY for why the old `diff -ruN | sed` did not)."""
    return X(cfg, h, f"cd {qp(h['root'])} && [ -d base/{n} ] && {{ python3 -c {shlex.quote(PATCH_PY)} {shlex.quote(n)} base/{n} worktrees/{n} > out/{n}/{fname}; }}", timeout=300)

# A remote copy's fingerprint: the checksum of the agent's changes and of its output folder (janus's own files left out), the
# number of output files and the report's lines other than PENDING. collect records it; stop and close compare (item 4).
def fingerprint(cfg, u):
    h = cfg.hosts[u["host"]]
    rc, o, e = hrun(h, FINGERPRINT % {"root": qp(h["root"]), "n": u["name"], "ex": DIFF_X}, timeout=120)
    if rc: return None
    return dict(l.split("=", 1) for l in o.splitlines() if "=" in l)

def uncollected(cfg, u):
    """Why removing or stopping a remote unit now would lose work (None when nothing would): its copy has changes or output
    that differ from what janus collect last brought here, or its host cannot be asked."""
    h = cfg.hosts.get(u.get("host"), {})
    if h.get("local") or u.get("via") in ("failed", "starting"): return None
    kv = fingerprint(cfg, u)
    if kv is None: return f"cannot read its copy on {u['host']} (unreachable?), so cannot tell whether work would be lost"
    if kv.get("fp") == u.get("collected"): return None
    d = kv.get("fp", "").split("|")[0].split()
    if len(d) > 1 and d[1] == "0" and int(kv.get("n", "0").strip() or 0) <= 1 and int(kv.get("p", "0").strip() or 0) == 0: return None   # nothing written yet
    return (f"its copy on {u['host']} has changes or output not collected " + (f"since {ts(u['collected_at'])}" if u.get("collected_at") else "yet")
            + f": janus collect {u['name']} first, or --force")

def cmd_collect(cfg, a):
    if ":" in a.unit: return collect_job(cfg, a.unit)
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]
    if h.get("local"): say("local unit: nothing to collect"); return
    root = h["root"]; n = u["name"]; od = cfg.p("out", "out") / n
    # The agent's changes come back as a patch against the tree it was sent (base/<unit>), never as an rsync over the Mac
    # checkout, which may have moved on or be shared (H9). Apply it with git apply --3way in the unit's worktree.
    remote_patch(cfg, h, n, "changes.patch")
    if not save_output(cfg, u): raise Err(f"collect from {u['host']} failed (unreachable?)")   # out/<unit>/, the task log
    if not cfg.run.dry and not save_transcript(cfg, u): say(f"note: no transcript of {n} saved (none found on {u['host']})")   # out/<unit>/transcript.md
    st = task_status(h, u["task"]) if u.get("via") == "pueue" and u.get("task") is not None and not cfg.run.dry else None
    if not (st and task_rc(st) is None): X(cfg, h, f"rm -f {qp(f'{root}/run/{n}.key')}")   # the key fifo, once no task can still read it (row 52)
    mc = cfg.models.get(u.get("model"), {})
    if not cfg.run.dry and u.get("via") == "pueue" and mc.get("kind") == "api" and u.get("run"):
        cost, reported = run_cost(od / "api.json", None, u["run"])
        if reported: keep_run_json(cfg, od, u["run"]); ledger_done(cfg, cfg.prov[mc["provider"]], u["run"], n, cost, "collected")   # idempotent: once per run id
    if not cfg.run.dry and u.get("report"): say(f"report -> {u['report']}")
    kv = fingerprint(cfg, u) if not cfg.run.dry else None
    if kv: u["collected"] = kv.get("fp"); u["collected_at"] = int(now()); bd.save(u)   # stop and close compare against it
    if not cfg.run.dry: bd.comment(u["id"], f"{ts()} collected from {u['host']}")
    p = od / "changes.patch"
    say(f"collected: {p} ({p.stat().st_size if p.exists() else 0} bytes; apply in {u['cwd']} with git apply --3way)")

def unit_dirty(cfg, cwd):
    """(uncommitted paths the unit owns, whether its tree lies in the project's own repo). Only the unit's cwd subtree counts,
    never janus's state dir or the board's folder (paths.board_dir, default board/): a unit whose cwd is inside the project's
    repo could otherwise never close while other agents' board posts keep that repo dirty. The project's repo is committed and
    pushed by janus commit-state, so its unpushed commits are not the unit's either."""
    rc, top, _ = sh(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    if rc: return [], False
    top = Path(top.strip()).resolve()
    rc, o, e = sh(["git", "status", "--porcelain", "-uall", "--", "."], cwd=cwd)
    if rc: return [], False
    skip = [cfg.state.resolve(), (cfg.root / cfg.paths.get("board_dir", "board")).resolve()]
    out = []
    for l in o.splitlines():
        f = l[3:].split(" -> ")[-1].strip().strip('"'); p = (top / f).resolve()
        if not any(p.is_relative_to(s_) for s_ in skip): out.append(f)
    rc2, ptop, _ = sh(["git", "-C", str(cfg.root), "rev-parse", "--show-toplevel"])
    return out, rc2 == 0 and Path(ptop.strip()).resolve() == top and Path(cwd).resolve() != top

def cmd_close(cfg, a):
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]; units = bd.units()
    handover = bool(getattr(a, "handover", False))
    mine = own_tree(cfg, u, units) or handover   # item 13: a successor took its tree, so --force is right
    if a.force and not mine: raise Err(f"--force refused: {u['cwd']} is shared (project root or another open unit's cwd)")
    if mine:
        dirty, state_repo = unit_dirty(cfg, u["cwd"])
        if dirty and not a.force: raise Err(f"{u['cwd']} has uncommitted changes ({', '.join(dirty[:3])}); commit and push, or --force")
        rc, o, e = sh(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=u["cwd"])
        if rc == 0 and o.strip() != "0" and not a.force and not state_repo: raise Err("unpushed commits; push first, or --force")
    elif not verdict(cfg, u) and u.get("via") != "failed":   # no tree of its own (H10): the git state is not the unit's; its REPORT is
        raise Err(f"{u['name']} has no final VERDICT line in {report_path(cfg, u)}")
    # A handover is never a reason to skip the uncollected guard (finding 3): a remote predecessor's dirty tree is its copy on
    # the host, so removing it without collect loses work. Only a plain --force skips the guard.
    why = None if (a.force and not handover) or cfg.run.dry else uncollected(cfg, u)
    if why: raise Err(f"refused: {u['name']}: {why}")
    if handover and not cfg.run.dry:   # never close a predecessor whose agent is still running (finding 3; finding 5: unknown too)
        st = unit_state(u, probe_all(cfg), herdr_agents(cfg))
        if st in ("working", "queued", "blocked", "unknown"):
            what = f"still {st}" if st != "unknown" else "in an unknown state (its host could not say whether it runs)"
            raise Err(f"{u['name']}: cannot hand over its tree: its agent is {what}; stop it first (janus stop {u['name']}), then hand its tree over")
    say(f"close {u['name']}" + ("" if mine else " (no worktree of its own: git checks skipped)"))
    if u.get("tab") or u.get("pane"):
        if cfg.run.dry: say(f"+ herdr tab close {u.get('tab') or u.get('pane')}")
        elif u.get("tab"): herdr("tab", "close", u["tab"])
        else: herdr("pane", "close", u["pane"])
    if not h.get("local"):
        root = h["root"]; n = u["name"]
        others = [x["name"] for x in units if x["id"] != u["id"] and x.get("host") == u["host"] and x.get("rcwd") == u.get("rcwd")]
        if others: say(f"keeping {u.get('rcwd')} on {u['host']}: open unit(s) {', '.join(others)} use it")
        else: X(cfg, h, f"cd {qp(root)} && rm -rf worktrees/{n} base/{n} briefs/{n}.md out/{n} run/{n}.* && rmdir worktrees base briefs out run 2>/dev/null; true")
    if u.get("via") == "pueue":   # the task's log is kept under out/<unit>/ before the task is removed
        if not cfg.run.dry: save_task_log(cfg, h, u, u["task"])
        X(cfg, h, f"{pueue(h)} remove {int(u['task'])} 2>/dev/null; true")
    X(cfg, {"local": True}, f"BOARD_DIR={shlex.quote(str(board_dir(cfg)))} {shlex.quote(str(board_script(cfg)))} close {u['name']} 2>/dev/null; true")
    if cfg.run.dry: return
    if u.get("run") and unit_state(u, {u["host"]: probe(u["host"], h)}, {}) not in ("working", "queued", "unknown"): settle_run(cfg, u, "closed")
    bd.comment(u["id"], f"{ts()} closed"); bd.run("close", u["id"], "--reason", a.reason or "done"); note_event(cfg, "close")
    say("closed")

WRAPUP = ("Your budget is spent; this short run is only for your report. Do not run, submit or wait for anything. Write {report} "
          "now: what is done, its state, what is left, the real exit codes of everything you ran (say \"not run\" where none), then "
          "the last line 'VERDICT: DONE' only if the brief's done-criteria are met, else 'VERDICT: BLOCKED'. Update report.json "
          "beside it to match. Then stop.")

def job_result(cfg, hs, ref, agent_host):
    """(exit code or None, log path the agent can read) of a finished job. A remote job's files are copied here first when
    the agent runs here (janus collect <host>:<task> does the same)."""
    hn, _, tid = ref.partition(":"); h = cfg.hosts[hn]; p = hs.get(hn) or {}
    t = ((p.get("pueue") or {}).get("tasks") or {}).get(tid); jid = job_of(t); rc = task_rc(t); rd = runs_dir(cfg, h)
    here = cfg.hosts.get(agent_host, {}).get("local")
    if not jid:   # a task without a job script (a gate): its pueue log is the log
        loc = cfg.f("runs") / f"task-{hn}-{tid}.log"; loc.parent.mkdir(exist_ok=True)
        r = hrun(h, f"{pueue(h)} log {int(tid)} --full", timeout=60)
        if r[0] == 0 and not cfg.run.dry: loc.write_text(r[1])
        return rc, str(loc) if here or h.get("local") else f"{hn}: {pueue(h)} log {tid}"
    if rc is None or t is None:
        o = hrun(h, f"cat {qp(f'{rd}/{jid}.rc')} 2>/dev/null")[1].strip()
        rc = int(o) if o.lstrip("-").isdigit() else rc
    if hn == agent_host or (h.get("local") and here): return rc, f"{rd}/{jid}.log"
    if here:
        loc = cfg.f("runs"); loc.mkdir(exist_ok=True)
        sh(["rsync", "-a", "-e", " ".join(SSH), f"{h['ssh']}:{rd}/{jid}.*", f"{loc}/"], timeout=300)
        return rc, str(loc / f"{jid}.log")
    return rc, f"{hn}:{rd}/{jid}.log"

def resume_message(cfg, hs, u, jobs):
    s, b = unit_spent(cfg, u), u.get("budget") or 0
    report = str(report_path(cfg, u)) if cfg.hosts[u["host"]].get("local") else f"{cfg.hosts[u['host']]['root']}/out/{u['name']}/REPORT.md"
    L = [f"Your jobs finished (spent ${s:.2f} of ${b:g}):"]
    for j in jobs:
        rc, log = job_result(cfg, hs, j, u.get("host"))
        note = ""
        if rc == 121:   # finding 10: nothing read exit 121; the unit is told what it means (the command never ran)
            note = " (never admitted: its host's admission never freed within the job's wait limit, so the command never ran; the log says why)"
        elif rc == 124: note = " (the command hit its own timeout)"
        L.append(f"- {j}: exit {rc if rc is not None else 'unknown (the task is gone and left no exit code)'}{note}; log {log}")
    L.append(f"Read only the logs you need, then continue the brief from where {report} stands. Same rules: submit builds and "
             f"tests detached, write 'WAITING <job ids>' in the report and end your turn; finish with the VERDICT line and the "
             f"real exit codes.")
    return "\n".join(L)

def resume_unit(cfg, u, msg, wrapup=False, budget=None):
    """Continue the unit's own agent session with msg (see above). budget: a new budget for this run (`janus resume --budget`,
    same tree), instead of the unit's remaining budget (item 24: a capped unit with an unpushed commit can be continued). The
    caller saves u. Returns a line saying what was done."""
    name = u["name"]; h = cfg.hosts[u["host"]]; mc = cfg.models.get(u.get("model"), {}); via = u.get("via"); text = JANUS_MARK + msg
    if via == "herdr":
        if not cfg.run.dry: herdr("agent", "prompt", name, text)
        return f"{name}: " + ("prompted in its herdr pane" if not cfg.run.dry else "would prompt in its herdr pane")
    if via == "tmux":
        tm = shlex.quote(unit_tmux(u)); X(cfg, h, f"tmux send-keys -t {tm} -l {shlex.quote(text)} && tmux send-keys -t {tm} Enter")
        return f"{name}: " + (f"resumed in tmux {unit_tmux(u)} on {u['host']}" if not cfg.run.dry else f"would resume in tmux {unit_tmux(u)} on {u['host']} (dry run)")
    if via != "pueue" or mc.get("kind") != "api": raise Err(f"{name}: a unit run via {via} cannot be resumed")
    pv = cfg.prov[mc["provider"]]; od = cfg.p("out", "out") / name
    sid = session_of(cfg, u)
    if not sid: raise Err(f"{name}: no session id (api.json or transcript) to resume")
    if u.get("run"):   # the run before is closed first (idempotent): its reported cost, else its budget
        cost, rep = run_cost(od / "api.json", (u.get("budget") or pv.get("budget_per_unit", 0)) if u.get("run") != u.get("wrapup") else bset(cfg, "wrapup_usd"), u["run"])
        if rep: keep_run_json(cfg, od, u["run"])   # the next run of this session is charged against it
        ledger_done(cfg, pv, u["run"], name, cost, "resumed")
    spent = unit_spent(cfg, u); explicit = budget is not None
    # The agent CLI stops a resumed run when THIS run's spend reaches its cap, and the first request of a resume loads the whole
    # context again: a cap under that cost stops at turn 1 with nothing done. So the wrap-up gets its budget on top of the
    # reload, and any other resume is refused when what is left cannot pay for the reload plus [budget] resume_min_usd.
    use = session_usage(cfg, u, sid=sid); reload = reload_usd(cfg, use); floor = round(reload + bset(cfg, "resume_min_usd"), 2)
    if wrapup: budget = round(bset(cfg, "wrapup_usd") + reload, 2)
    elif budget is None: budget = round((u.get("budget") or pv.get("budget_per_unit", 0)) - spent, 2)
    if budget < floor:
        need = f", and a resume needs ${floor:.2f} (${reload:.2f} to load its {use['ctx']}-token context again)" if reload else " (under [budget] resume_min_usd)"
        raise Err(f"{name}: ${budget:.2f} budget {'given' if explicit else 'left'}{need}: resume it with janus resume {name} --budget <usd> (same tree), or janus launch --resume-of and a new budget")
    k = len(unit_runs(u)) + 1; run = f"{name}.r{k}.{int(now())}"
    run = reserve_budget(cfg, mc, name, budget, run)
    local = h.get("local", False); old = u.get("task")
    report = str(report_path(cfg, u)) if local else f"{h['root']}/out/{name}/REPORT.md"
    env = {"JANUS_ROLE": "agent", "JANUS_REPORT": report, "JANUS_UNIT": name, "JANUS_PROJECT": project_id(cfg), "JANUS_ROOT": str(cfg.root)}
    if local:
        pf = od / f"resume-{k}.md"
        if not cfg.run.dry: atomic_write(pf, text)
        cmd = shlex.join([sys.executable, str(JANUS_SELF), "api-run", name, "--run", run, "--model", u["model"], "--budget", str(budget),
                          "--brief", u.get("rbrief") or u["brief"], "--cwd", u["rcwd"], "--report", report, "--resume-session", sid, "--prompt-file", str(pf)])
        rc, o, e = X(cfg, h, f"{pueue(h)} add -g agents -l {tag(cfg, name)} -w {shlex.quote(u['rcwd'])} --print-task-id -- {pq(cmd)}", env=env)
    else:
        rc, o, e = api_remote(cfg, h, name, mc, u.get("rbrief") or f"{h['root']}/briefs/{name}.md", u["rcwd"], budget, text, env, session=sid, run=run)
    if rc:
        if not cfg.run.dry: ledger_done(cfg, pv, run, name, 0, "resume-failed")
        raise Err(f"{name}: resume failed: {(e or o).strip()[:120]}")
    if cfg.run.dry: return f"{name}: would resume session {sid} as run {run} (${budget:g})"
    st, why = confirm_started(cfg, h, int(o.strip() or 0))
    if st == "died":
        ledger_done(cfg, pv, run, name, 0, "died-at-start")
        raise Err(f"{name}: the resumed run died at once ({why})")
    if old is not None: save_task_log(cfg, h, u, old, remove=True)   # the finished task's log is kept, then the task removed
    u.update(task=int(o.strip() or 0), run=run, runs=[*unit_runs(u), run], resumes=u.get("resumes", 0) + 1, resumed_at=int(now()),
             **({"budget": round(spent + budget, 2)} if explicit else {}))   # item 24: the unit's budget grows by the new run's
    if wrapup: u["wrapup"] = run
    chk = f"herdr {name}" if via == "herdr" else f"pueue {u['host']}:{u['task']}" if via == "pueue" else f"tmux {u['host']}:{unit_tmux(u)}"
    board(cfg, "post", name, "waiting", f"resumed as task {u['task']}", env={"BOARD_CHECK": chk})   # item 9: a queued run is not GONE
    return f"{name}: resumed session {sid[:8]} as run {run} (${budget:g}{', wrap-up' if wrapup else ''}), task {u['task']}"

def cmd_park(cfg, a):
    """Stop a live agent's turn while its jobs wait or run (the remedy for waiting:<unit>); the watcher resumes it, same session,
    when they finish. API units: the run is ended (SIGTERM: its cost is still recorded); herdr and tmux agents: Escape."""
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]; n = u["name"]
    hs = probe_all(cfg); jobs = unit_jobs(cfg, hs, n)
    if not jobs: raise Err(f"{n} has no queued or running job of its own: nothing to wait for (janus stop {n} to stop it)")
    say(f"park {n}: its jobs {', '.join(jobs)} keep running; it resumes when they finish")
    if cfg.run.dry: return
    via = u.get("via")
    if via == "pueue":
        hrun(h, f"{pueue(h)} kill -s SIGTERM {int(u['task'])} 2>/dev/null; true")
        for _ in range(30):
            if task_rc(task_status(h, u["task"])) is not None: break
            time.sleep(2)
        else: hrun(h, f"{pueue(h)} kill {int(u['task'])} 2>/dev/null; true")
        if h.get("local"): settle_run(cfg, u, "parked")
    elif via == "herdr": herdr("pane", "send-keys", u["pane"], "Escape")
    elif via == "tmux": X(cfg, h, f"tmux send-keys -t {shlex.quote(unit_tmux(u))} Escape")
    u["parked"] = {"jobs": jobs, "since": int(now()), "by": "janus park"}; bd.save(u)
    bd.comment(u["id"], f"{ts()} parked on {', '.join(jobs)}"); say(f"parked {n}")

def cmd_resume(cfg, a):
    """Resume a unit's own session now: parked (its jobs' results, when they finished), or with --message.

    Finding 2: never a second agent beside a running one -- a pueue API run's resume queues a NEW task, so a still-running agent
    task is refused even with --force; tmux/herdr only type into the agent's own pane, so they are never refused on state (a
    blocked herdr unit can be answered), and a review/audit unit may resume in the tree it reviews (the launch H9 exception).
    A still-running job is never marked handled, so the next park line still names it; --force overrides only the 'not finished'
    refusal."""
    bd, u = get_unit(cfg, a.unit); hs = probe_all(cfg)
    jobs = (u.get("parked") or {}).get("jobs") or waiting_line_jobs(cfg, u)
    if a.message: msg = a.message
    elif jobs: msg = resume_message(cfg, hs, u, jobs)
    else: raise Err(f"{u['name']} is not parked: give --message")
    # finding 2: the tree is another open unit's cwd: never put a unit back into the tree its successor now works in (H9). A
    # review/audit unit may share a tree with the unit it reviews, exactly as launch allows (launch.py H9 exception); finding 6
    # (fix-install6): a review/audit unit in `shared` is by design (it reviews that tree), so it is ignored -- a unit may resume in
    # a tree whose only other open unit is its reviewer.
    cwd = Path(u["cwd"]).resolve() if u.get("cwd") else None
    shared = []
    if cwd:
        units = bd.units()
        shared, _ = cwd_taken(cfg, units, cwd, u["name"])
        shared = [n for n in shared if next((x for x in units if x["name"] == n), {}).get("kind") not in ("review", "audit")]
    local = cfg.hosts.get(u.get("host"), {}).get("local")
    if shared and not (local and u.get("kind") in ("review", "audit")):
        raise Err(f"refused: {cwd} is the cwd of open unit(s) {', '.join(shared)}: resume {u['name']} in a tree of its own (H9)")
    # finding 2: never a second agent beside a running one. A pueue API run's resume queues a NEW task, so a still-running agent
    # task is refused even with --force (the old guard sat inside `not --force`, and --force then started the second agent). A
    # tmux/herdr resume only types into the agent's own pane (it cannot double-start), and a herdr unit blocked on a question is
    # answerable, so those are never refused on their state. Finding 6 (fix-install6): refuse unless the agent task is KNOWN
    # ended -- Paused/Stashed/Locked (a live agent janus never pauses) and an unknown state (a failed probe) are refused too.
    if u.get("via") == "pueue":
        st = unit_state(u, hs, herdr_agents(cfg))
        if st not in ENDED:
            why = "its agent's state cannot be told (unknown)" if st == "unknown" else f"its agent is still {st}"
            raise Err(f"{u['name']}: {why}: a resume would start a second agent beside it (janus stop {u['name']} first)")
    if not a.force:
        left = [j for j in jobs if job_state(hs, j)[0] not in ("done", "gone")]
        if left: raise Err(f"{u['name']} waits on {', '.join(left)}, not finished (--force to resume now)")
    say(resume_unit(cfg, u, msg, budget=getattr(a, "budget", None)))
    if cfg.run.dry: return
    done = [j for j in jobs if job_state(hs, j)[0] in ("done", "gone")]   # finding 2: a running job is never marked handled
    if done: u["handled_jobs"] = [*u.get("handled_jobs", []), *done]
    u.pop("parked", None); bd.save(u)
    bd.comment(u["id"], f"{ts()} resumed")

def save_output(cfg, u):
    """Bring a remote unit's output folder here (out/<unit>/: REPORT.md, api.json and each run's api-<run>.json, logs; never the
    key fifo) and keep its pueue task's log; a resumed unit's report also goes back to its original path. True when done."""
    h = cfg.hosts[u["host"]]; n = u["name"]; od = cfg.p("out", "out") / n
    cmd = ["rsync", "-a", "--exclude", "*.key", "-e", " ".join(SSH), f"{h['ssh']}:{h['root']}/out/{n}/", str(od) + "/"]
    if cfg.run.dry: say("+ " + shlex.join(cmd)); return True
    od.mkdir(parents=True, exist_ok=True)
    if sh(cmd, timeout=600)[0]: return False
    if u.get("via") == "pueue" and u.get("task") is not None: save_task_log(cfg, h, u, u["task"])
    if u.get("report") and (od / "REPORT.md").is_file(): atomic_write(Path(u["report"]), (od / "REPORT.md").read_text())
    return True

def saved_marker(u):
    """What `saved` records when a remote unit's output was brought here: its pueue task, else 'tmux' (a tmux unit has no task,
    finding 5, so `saved == task` is never None == None)."""
    return u.get("task") if u.get("task") is not None else "tmux"

def refresh_remote(cfg, u, bd, log):
    """A remote unit's output is read here: saved once when its agent's turn ended (then its verdict, WAITING line and api.json
    are judged here), and every [collect] backup_min while it runs (item 4)."""
    if cfg.hosts[u["host"]].get("local") or u.get("via") in ("failed", "starting"): return
    fin = u.get("state") in ENDED
    if not ((fin and u.get("saved") != saved_marker(u)) or now() - u.get("saved_at", 0) > cfg.c.get("collect", {}).get("backup_min", 30) * 60): return
    remote_patch(cfg, cfg.hosts[u["host"]], u["name"], "backup.patch")   # the agent's changes so far, as a patch (out/<unit>/backup.patch)
    if not save_output(cfg, u): log(f"{u['name']}: saving its output from {u['host']} failed"); return
    u["saved_at"] = int(now())
    if fin: u["saved"] = saved_marker(u)
    bd.save(u)
    u["verdict"] = v = verdict(cfg, u)
    if not v and not u.get("_park") and (fin or u.get("via") == "tmux"):
        pk = waiting_line_jobs(cfg, u)
        if pk: u["_park"] = pk; u["state"] = "parked"

def sync_remote_outputs(cfg, units, bd):
    """Item 11 addendum (finding 5): before check judges a remote unit parked vs orphan, bring its report and the end-of-turn
    backup patch here once its agent ended (the local report has no WAITING line until janus collect, so its jobs looked
    orphaned). Remote tmux units (no pueue task) are synced too, and a failed sync fails closed: the unit is marked so its jobs
    are never cancelled on the stale local report."""
    if cfg.run.ro: return
    for u in units:
        if cfg.hosts.get(u.get("host"), {}).get("local") or u.get("via") in ("failed", "starting"): continue
        if u.get("state") not in ENDED or u.get("saved") == saved_marker(u): continue
        try:
            remote_patch(cfg, cfg.hosts[u["host"]], u["name"], "backup.patch")
            if not save_output(cfg, u):
                u["_sync_failed"] = True; continue
        except Err:
            u["_sync_failed"] = True; continue
        u["saved"] = saved_marker(u); u["saved_at"] = int(now())
        try: bd.save(u)
        except Err: pass

def tend_parked(cfg, hs, units, log):
    """The watcher's part: record parked units, resume those whose jobs all finished, wrap up capped runs (item 2)."""
    bd = Beads(cfg)
    for u in units:
        try:
            refresh_remote(cfg, u, bd, log)
            jobs = u.get("_park")
            if jobs:
                if (u.get("parked") or {}).get("jobs") != jobs:
                    u["parked"] = {"jobs": jobs, "since": int(now()), "by": "report"}; bd.save(u); log(f"park {u['name']} on {', '.join(jobs)}")
                    if card_fields(cfg, u["name"]).get("state") != "waiting": board(cfg, "post", u["name"], "waiting", " ".join(jobs))   # never SILENT/GONE
                st = [job_state(hs, j)[0] for j in jobs]
                if all(x in ("done", "gone") for x in st):
                    # once only (panel-eng F1): the handled fact is recorded here before the resume and guards it; the unit
                    # record (bd) is saved best-effort after, so a failed save cannot start a second run on the same session
                    key, cause = f"parked:{u['name']}", f"resume:{','.join(jobs)}"
                    if cfg.run.dry or record(cfg, key, cause, f"resumed: jobs {', '.join(jobs)} finished", u):
                        try: log(resume_unit(cfg, u, resume_message(cfg, hs, u, jobs)))
                        except Err as e:   # nothing started: recorded on the unit, check reports it; the next pass tries again
                            if not cfg.run.dry: forget(cfg, key, cause, str(e))
                            if (u.get("resume_error") or {}).get("why") != str(e): u["resume_error"] = {"why": str(e), "at": int(now())}; save_soft(bd, u, log)
                            raise
                        u.pop("resume_error", None)
                    elif set(jobs) <= set(u.get("handled_jobs", [])): continue
                    else: log(f"{u['name']}: already resumed for {', '.join(jobs)}; saving the unit record again")
                    if cfg.run.dry: continue
                    u["handled_jobs"] = [*u.get("handled_jobs", []), *jobs]; u.pop("parked", None); save_soft(bd, u, log)
                continue
            if wrap_due(cfg, u):
                if not cfg.run.dry and not record(cfg, f"unit:{u['name']}", "wrapup", "ended at its budget cap without a verdict: resumed once to write its report", u, ack=f"unit:{u['name']}:limit"):
                    continue   # already wrapped up once (the unit record's save failed): never a second wrap-up run
                try: log(resume_unit(cfg, u, WRAPUP.format(report=report_path(cfg, u) if cfg.hosts[u["host"]].get("local") else f"{cfg.hosts[u['host']]['root']}/out/{u['name']}/REPORT.md"), wrapup=True))
                except Err as e:
                    if not cfg.run.dry: forget(cfg, f"unit:{u['name']}", "wrapup", str(e), ack=f"unit:{u['name']}:limit")
                    raise
                save_soft(bd, u, log)
        except Err as e: log(f"{u['name']}: {e}")

def save_soft(bd, u, log):
    """Save the unit record best-effort: the once-only facts live in .janus/autopilot.json, so a busy bd only delays it."""
    try: bd.save(u)
    except Err as e: log(f"{u['name']}: saving the unit record failed ({e}); retried next pass")

def wrap_due(cfg, u):
    """A run that ended at its budget cap without a final verdict, not yet wrapped up (item 2)."""
    mc = cfg.models.get(u.get("model"), {})
    return (u.get("via") == "pueue" and mc.get("kind") == "api" and u.get("state") in ENDED and not u.get("verdict") and not u.get("wrapup")
            and api_out(cfg, u).get("subtype") == "error_max_budget_usd" and not u.get("_park"))

def save_transcript(cfg, u):
    """out/<unit>/transcript.md: the whole condensed transcript (collect; it outlives the remote copy)."""
    rc, txt = read_transcript(cfg, u, "", 0)
    if rc == 0 and txt and not cfg.run.dry: atomic_write(cfg.p("out", "out") / u["name"] / "transcript.md", txt + "\n")
    return rc == 0
