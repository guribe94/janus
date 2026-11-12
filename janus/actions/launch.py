"""janus launch and janus api-run: model routing, placement, the brief header and prompt, the three ways an agent starts
(API run as a pueue task, herdr pane, remote tmux), and briefs whose paths must reach a remote agent."""
import argparse, json, os, re, shlex, subprocess, sys, time
from pathlib import Path
from janus.actions.jobs import agent_res, ensure_lanes, in_container, job_script, runs_dir, task_rc, task_status, write_cap
from janus.beads import Beads, UNIT_V
from janus.board import board, board_dir, board_script, board_shipped
from janus.budgets import budget_for, transcript_cost, waste_paused
from janus.cap import build_room, cap_nums, class_mb, host_cap, record_history, tree_rss_mb
from janus.context import EXCL, Err, JANUS_SELF, SSH, X, atomic_write, clean_env, findkey, hrun, now, pq, qp, say, sh, ts
from janus.ledger import ledger_done, model_ok, read_secret, reserve_budget, run_cost
from janus.memory import MEM_SECTIONS, mem_line, memory_read, mset
from janus.platform import herdr
from janus.probe import probe_all, pueue, room
from janus.quality import qset
from janus.templates import CLEAN_PUEUE
from janus.transcripts import predecessor_name, related_pointers, transcript_of
from janus.units import herdr_agents, main_checkout, report_path, unit_state, ws_id

def pick_model(cfg, tier, kind, of_vendor, host=None):
    lst = cfg.route.get("review" if kind == "review" else tier, [])
    why = []
    for m in lst:
        c = cfg.models.get(m, {})
        if host and host not in c.get("hosts", list(cfg.hosts)): why.append(f"{m}: not allowed on {host}"); continue
        if kind == "review" and of_vendor and c.get("vendor") == of_vendor: why.append(f"{m}: same vendor as implementer"); continue
        ok, w = model_ok(cfg, m)
        if ok: return m
        why.append(f"{m}: {w}")
    raise Err("no model available for " + tier + "/" + kind + " (" + "; ".join(why) + ")")

def agent_prompt(cfg, unit, brief, hn, report=None):
    """The first prompt of a unit. It names the report path the unit must keep (row 26: a resumed unit continues its original
    report, which janus reads; the agent must not guess it from the brief)."""
    rc, o, e = board(cfg, "rule", unit, *([] if cfg.hosts[hn].get("local") else [hn]))
    rp = f"your report {report}" if report else "REPORT.md (path in the brief)"
    return (f"Read {brief} and carry out every instruction exactly. Keep {rp} current after every step (a run "
            f"stopped by its budget must leave its state written): line 1 'VERDICT: PENDING' "
            f"until done, then end with 'VERDICT: DONE' (or BLOCKED) and the real exit codes. Also write report.json "
            f"beside it (verdict, files_changed, jobs with their exit codes, tests_added with the failure each catches, "
            f"left); janus reads it before the prose. {o.strip()}")

def wait_shell(pane, unit):
    for _ in range(30):
        herdr("pane", "run", pane, f"echo ready-'{unit}'")
        rc, *_ = sh(["herdr", "pane", "wait-output", pane, "--match", f"ready-{unit}", "--timeout", "2000"], timeout=10)
        if rc == 0: return

def new_tab(cfg, label, cwd):
    ws = ws_id(cfg, create=True) or ""
    r = herdr("tab", "create", *(["--workspace", ws] if ws else []), "--label", label, "--cwd", str(cwd))
    if not r: raise Err("herdr tab create failed")
    return r["root_pane"]["pane_id"], r["tab"].get("tab_id")

def deliver_key(h, fifo, key):
    """Send the key into the task's fifo from a detached ssh with no time limit, so a full agents group queues the task instead
    of failing the launch (M13). The key goes over the pipe only: never a file or argv."""
    p = subprocess.Popen([*SSH, h["ssh"], f"cat > {qp(fifo)}"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=clean_env())
    p.stdin.write(key.encode() + b"\n"); p.stdin.close()

API_CMD = ["claude", "--bare", "-p", "--model", "{api_id}", "--max-budget-usd", "{budget}", "--output-format", "json",
           "--dangerously-skip-permissions", "{prompt}"]   # [providers.<p>] cmd overrides; --bare: the key only, never a login

def api_argv(pv, mc, budget, prompt, session=None):
    """The agent's command line ([providers.<p>] cmd, default API_CMD). session: continue that session ([providers.<p>]
    resume_cmd, e.g. ["codex", "exec", "resume", "{session}", "{prompt}"]; default: API_CMD with --resume {session})."""
    t = pv.get("cmd", API_CMD)
    if session:
        if pv.get("resume_cmd"): t = pv["resume_cmd"]
        elif "cmd" in pv: raise Err("the provider has its own cmd and no resume_cmd: set [providers.<p>] resume_cmd to resume a session")
        else: t = [*API_CMD[:3], "--resume", "{session}", *API_CMD[3:]]
    return [x.format(api_id=mc.get("api_id", mc.get("model", "")), model=mc.get("model", ""), budget=budget, prompt=prompt, session=session or "") for x in t]

def confirm_started(cfg, h, tid, secs=8):
    """Watch a just-queued agent task for a few seconds: (state, detail). "running" or "queued" (waiting for an agents slot) is
    fine, and "finished" a run that already ended with exit 0; "died" is a run that failed at once (a broken command line, a
    missing tool): its exit code and the end of its log."""
    t0 = now(); t = None
    while now() - t0 < secs:
        t = task_status(h, tid)
        if t is not None and task_rc(t) == 0: return "finished", "exit 0"
        if t is not None and task_rc(t) is not None:
            log = hrun(h, f"{pueue(h)} log {int(tid)} --lines 12", timeout=30)[1].strip()
            return "died", f"exit {task_rc(t)}: " + " | ".join(l.strip() for l in log.splitlines()[-8:] if l.strip())[-400:]
        time.sleep(1)
    k = (t or {}).get("status"); k = k if isinstance(k, str) else next(iter(k or {}), "unknown")
    return {"Running": "running", "Queued": "queued"}.get(k, str(k).lower()), ""

def put_script(cfg, h, path, text, mode="755"):
    """Write a script on host h by write-then-rename: a script that is running keeps reading its old file (janus-fixes7: a shell
    rereads its script at the old byte offset when the file is edited in place)."""
    if h.get("local"): atomic_write(Path(path), text, int(mode, 8)); return 0, "", ""
    f, d, m = qp(path), qp(os.path.dirname(str(path)) or "."), "%o" % int(mode, 8)
    return X(cfg, h, f"mkdir -p {d} && cat > {f}.tmp.$$ && chmod {m} {f}.tmp.$$ && mv -f {f}.tmp.$$ {f}", inp=text)

def api_remote(cfg, h, name, mc, rbrief, rcwd, budget, prompt, env, session=None, run=None):
    """Headless agent as a pueue task on host h. The key goes over ssh stdin into a fifo that only that task reads: never a file,
    argv or pueue's environment."""
    pv = cfg.prov[mc["provider"]]; var, key = read_secret(pv)
    q = shlex.quote; R = h["root"]; root = qp(R); kf = f"{R}/run/{name}.key"; fifo = qp(kf); od = qp(f"{R}/out/{name}"); sp = f"{R}/run/{name}.sh"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var): raise Err(f"provider key variable {var!r} is not a variable name")
    mm = h.get("memory_max"); sd = f"systemd-run --user --scope -q -p {q(f'MemoryMax={mm}')} -- " if mm else ""
    ex = "".join(f"export {k}={q(v)}\n" for k, v in env.items())
    rd = qp(h["_rd"]); amb = int(cap_nums(cfg, h)["agmb"])
    # the agent's reservation (runs/agent-<unit>.res, its pid: exec keeps it) and a sampler that records the agent class's peak
    res = (f"RD={rd}; if [ -f $RD/cap.sh ]; then . $RD/cap.sh; echo \"$$ 0 $(cap_mb agent {amb} 0) $(date +%s) agent\" > $RD/agent-{name}.res\n"
           f"  ( t0=$(date +%s); pk=0; while kill -0 $$ 2>/dev/null; do v=$(tree_mb $$); [ \"${{v:-0}}\" -gt $pk ] && pk=$v; sleep 10; done\n"
           f"    cap_record agent $pk $(( $(date +%s) - t0 )) 0; rm -f $RD/agent-{name}.res ) </dev/null >/dev/null 2>&1 &\nfi\n")
    script = (f"{h.get('agent_env', 'export PATH=$HOME/.local/bin:$PATH')}\nexport PATH={root}/run/bin:$PATH\n{ex}read -r K < {fifo}; rm -f {fifo}; export {var}=$K; unset K\n"
              f"cd {qp(rcwd)} || exit 1\n{res}{sd}{shlex.join(api_argv(pv, mc, budget, prompt, session))} < /dev/null > {od}/api.json 2> {od}/api.err; rc=$?\n"
              + (f"cp {od}/api.json {od}/{q(f'api-{run}.json')} 2>/dev/null\n" if run else "") + "exit $rc\n")
    if cfg.run.dry: return X(cfg, h, f"mkfifo {fifo}; write {qp(sp)}; {pueue(h)} add -g agents -l {name} -w {qp(rcwd)} --print-task-id -- {pq('sh ' + qp(sp))}; key | ssh cat > {fifo}")
    rc, o, e = hrun(h, f"mkdir -p {root}/run/bin {od} && rm -f {fifo} && mkfifo -m 600 {fifo}")
    if rc: return rc, o, e
    rc, o, e = put_script(cfg, h, sp, script)
    if rc: return rc, o, e
    rc, o, e = put_script(cfg, h, f"{R}/run/bin/pueue", clean_pueue_script(h))
    if rc: return rc, o, e
    rc, o, e = hrun(h, f"{pueue(h)} add -g agents -l {name} -w {qp(rcwd)} --print-task-id -- {pq('sh ' + qp(sp))}")
    if rc: return rc, o, e
    deliver_key(h, kf, key)
    return 0, o, ""

def cmd_api_run(cfg, a):
    """One API agent run on this machine, as a pueue task that `janus launch` queued (janus-fixes7: janus runs API agents itself,
    no project launcher). The key is read here from the secret store and given only to the agent's process: never to pueue,
    a file or argv. Ledger: the run's running row (reserved at launch; written here when missing) and exactly one done row,
    both idempotent; a malformed ledger or a finished run id refuses the run."""
    mc = cfg.models.get(a.model) or {}
    if mc.get("kind") != "api": raise Err(f"model {a.model} is not an api model")
    pv = cfg.prov[mc["provider"]]; name = a.unit; od = cfg.p("out", "out") / name; od.mkdir(parents=True, exist_ok=True)
    run = reserve_budget(cfg, mc, name, a.budget, a.run)
    hn = next((n for n, h in cfg.hosts.items() if h.get("local")), "local")
    try: var, key = read_secret(pv)
    except Err as e: ledger_done(cfg, pv, run, name, 0, "no-key"); raise
    shim = cfg.f("bin"); put_script(cfg, {"local": True}, str(shim / "pueue"), clean_pueue_script({"local": True}))
    env = clean_env({"JANUS_ROLE": "agent", "JANUS_UNIT": name, "JANUS_ROOT": str(cfg.root), "JANUS_REPORT": a.report or str(od / "REPORT.md"), "BOARD_DIR": str(board_dir(cfg)), var: key})
    env["PATH"] = f"{shim}:{env['PATH']}"   # the agent's own `pueue add` never stores the key in pueue's state file
    prompt = Path(a.prompt_file).read_text() if a.prompt_file else agent_prompt(cfg, name, a.brief, hn, a.report or str(od / "REPORT.md"))
    argv = api_argv(pv, mc, a.budget, prompt, a.resume_session)
    say(f"janus api-run {name}: run {run}, model {mc.get('api_id', a.model)}, budget ${a.budget}, cwd {a.cwd}" + (f", resuming session {a.resume_session}" if a.resume_session else ""))
    import signal
    with open(od / "api.json", "w") as fo, open(od / "api.err", "w") as fe:
        p = subprocess.Popen(argv, cwd=a.cwd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe)
    del key, env
    fwd = lambda s, _f: p.send_signal(s)   # pueue kill / janus stop: the agent stops too, and its done row is still written
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(s, fwd)
    # the agent's reservation on this host (runs/agent-<unit>.res) while it runs; its peak memory goes to the history (class agent)
    h = cfg.hosts.get(hn, {"_rd": str(cfg.f("runs"))}); rd = Path(h["_rd"]); rd.mkdir(parents=True, exist_ok=True); t0 = now(); peak = 0
    try: hist = (rd / "history.tsv").read_text().splitlines()
    except OSError: hist = []
    n = cap_nums(cfg, h); resf = rd / f"agent-{name}.res"
    atomic_write(resf, f"{p.pid} 0 {class_mb(hist, 'agent', n['agmb'], 0, n['hn'], n['margin'])} {int(t0)} agent\n")
    while True:
        try: rc = p.wait(timeout=10); break
        except subprocess.TimeoutExpired: peak = max(peak, tree_rss_mb(p.pid))
        except InterruptedError: continue
    resf.unlink(missing_ok=True)
    if peak: record_history(rd, "agent", peak, int(now() - t0), rc)
    cost, reported = run_cost(od / "api.json", a.budget); how = ""
    if reported:
        try: atomic_write(od / f"api-{run}.json", (od / "api.json").read_text())   # every run's own json (janus report, resumes)
        except OSError: pass
    else:   # stopped before it wrote its cost (janus park, stop): its transcript since this run started, + 25 %, at most the budget
        f = transcript_of(cfg, {"rcwd": a.cwd, "started": t0 - 5, "brief": a.brief}) if not a.resume_session else \
            next(iter(sorted((Path.home() / ".claude/projects").glob(f"*/{a.resume_session}.jsonl"))), None)
        est = transcript_cost(cfg, f, since=t0 - 5)[0] if f else None
        if est is not None: cost, how = min(float(a.budget), round(est * 1.25, 4)), " (no cost reported: transcript estimate + 25 %)"
        else: how = " (no cost reported: charged the whole budget)"
    ledger_done(cfg, pv, run, name, cost, rc)
    say(f"janus api-run {name}: exit {rc}, cost ${cost:.4f}{how}")
    return rc

# A pueue that never stores the caller's environment: an agent holds ANTHROPIC_API_KEY in its own environment (claude passes
# it to every tool command), and pueue stores a task's whole environment in its state file. The lane runner uses it, and
# remote API agents get it first on PATH as `pueue` (janus-fixes6: found when a remote agent's lane job stored the key).
def clean_pueue_script(h):
    if h.get("local"):
        import shutil
        real = shutil.which("pueue", path=clean_env()["PATH"]) or "pueue"
    else: real = f"{h['bin']}/pueue" if h.get("bin") else "$HOME/.local/bin/pueue"
    return CLEAN_PUEUE % {"real": real}

def lane_runner(cfg, hn, h, name, rcwd):
    """<root>/run/<unit>.lane on host h: `lane <command>` runs the command in the agent's tree through this host's pueue jobs lane,
    with janus's admission (runs/cap.sh: reservations) and MemoryMax, waits, prints the log and exits with its code (M9; fixes5 item 2).
    `lane --detach <command>` queues it, prints <host>:<task> and returns at once (janus-fixes9: agents never wait)."""
    a = argparse.Namespace(lane="jobs", subdir=None, usb=False, wt=rcwd)
    rd = runs_dir(cfg, h); q = shlex.quote
    crd = in_container(h, rd)   # the runs dir as the job sees it
    js = job_script(cfg, hn, h, a, name, f"bash {qp(crd + '/__JID__.cmd')}", "__JID__", wd=rcwd, clsrc='"$(cat "$R.cmd")"')
    return ("#!/bin/bash\n# janus lane runner for unit " + name + f" on {hn}: lane <command>\nRD={qp(rd)}; mkdir -p \"$RD\"\n"
            f"det=; [ \"$1\" = --detach ] && {{ det=1; shift; }}\n"
            f"jid={name}-$(date +%m%d-%H%M%S)-$$; R=\"$RD/$jid\"\nprintf '%s\\n' \"$*\" > \"$R.cmd\"\n"
            f"cat > \"$R.sh\" <<'JANUS_JOB_EOF'\n{js}JANUS_JOB_EOF\nsed -i \"s/__JID__/$jid/g\" \"$R.sh\"\n"
            f"{pueue(h)} group add -p 1 jobs >/dev/null 2>&1\n"
            f"tid=$(env -i HOME=\"$HOME\" USER=\"$USER\" PATH=\"$PATH\" ${{XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR=\"$XDG_RUNTIME_DIR\"}} LANG=\"${{LANG:-C.UTF-8}}\" {pueue(h)} add -g jobs -l unit:{name} -w \"$RD\" --print-task-id -- \"bash $(printf %q \"$R.sh\")\") || exit 125\n"
            f"echo \"lane: queued {hn}:$tid ($jid)\" >&2\n[ -n \"$det\" ] && {{ echo \"{hn}:$tid\"; exit 0; }}\n"
            f"{pueue(h)} wait -q $tid; cat \"$R.log\"; exit $(cat \"$R.rc\" 2>/dev/null || echo 255)\n")

PARK_RULE = ("**Never wait for a build or test (janus rule).** Submit it with `{submit}`: it prints the job id (`<host>:<task>`) at once. "
             "Then write a line `WAITING <job ids>` in your report ({report}) and end your turn. Do not poll, sleep or wait for a job: "
             "janus resumes this same session with each job's exit code and log path when all of them have finished. A turn spent "
             "waiting costs money and can end the run at its budget cap with nothing written.\n\n")

BOARD_RULE = ("**Board:** post only state changes (status when you start, `waiting <job ids>` when you park, `blocked` or `question` when "
              "stuck, `learned <fact, with its source>` when you found something every agent should know, `done <VERDICT line>` at the end); "
              "your steps go in your report, not on the board.")

def swarm_header(cfg, hn, h, name, rcwd, local, brief_text=None, pred=None):
    """What every agent gets at start besides its brief (janus-swarm item 2): the session memory, then pointers (never content)
    to the project's agent instructions, the reports index, the peers file and how to read another agent's transcript; then the
    RELATED: names and the predecessor's report path and transcript command (imp-awareness)."""
    es = memory_read(cfg); L = []
    if es:
        L.append("## Session memory (curated by the orchestrator; your brief wins on conflict)")
        for sec in MEM_SECTIONS.values():
            sel = [mem_line(e) for e in es if e[0] == sec]
            if sel: L += [f"{sec}:", *sel]
        L.append("")
    bdir = str(board_dir(cfg)) if local else f"{h['root']}/board"
    docs = mset(cfg, "agent_docs"); docs = [docs] if isinstance(docs, str) else docs
    L += ["## Other agents and shared knowledge (janus)",
          f"- Project instructions: read the root {' / '.join(docs)} of your tree ({rcwd}) and the one in each folder you touch, where present.",
          f"- Reports index: {bdir}/index.md (one line per unit: verdict, result, report path).",
          f"- Decisions: {bdir}/decisions.md (one line per decision record; read the record before you decide anything it touches).",
          f"- Peers: {bdir}/peers.md (each running unit: host, model, branch, files it changed, step, jobs it waits on, report, transcript).",
          ("- Another agent's transcript: `janus transcript <unit> --grep '<regex>'` or `janus transcript <unit> --tail 20` (condensed)." if local else
           "- Another agent's transcript: peers.md names where it is; when it is on this host, grep or tail it (never read it whole); "
           "otherwise ask with a board question and the orchestrator reads it for you."),
          "- Rule: read peers.md and the index when your task touches another unit's area; read a transcript only to solve a problem "
          "another unit met, by grep or tail, never whole.",
          "- " + BOARD_RULE, "", ""]
    rel = related_pointers(cfg, name, brief_text, pred)
    if rel: L += [rel, ""]
    return "\n".join(L)

def quality_block(cfg, report=None):
    """The quality rules every brief header carries: the project's rules file by pointer ([quality] rules_file), the fail-first
    test rule, "run only the tests your change can affect", and the report.json contract."""
    rf = qset(cfg, "rules_file")
    L = ["**Quality (janus):** every new test names the failure it catches, shown failing first; run only the tests your change can affect."]
    if rf: L.append(f"- Project quality rules: read `{rf}` in your tree where present, before you start.")
    L.append(f"- Structured report: also write `report.json` beside {report or 'REPORT.md'} (verdict, files_changed, jobs with their exit codes, "
             f"tests_added with the failure each catches, left); janus reads it before the prose.")
    return "\n".join(L) + "\n"

def brief_header(cfg, hn, h, name, rcwd, local, build=None, report=None, brief_text=None, pred=None):
    """The header every unit's brief gets: where it runs, how it submits builds and tests (detached, then WAITING), its report and
    the quality rules, then the session memory and the swarm pointers (swarm_header; imp-awareness adds the RELATED: names and the
    predecessor there)."""
    if local:
        b = build or hn
        return (f"<!-- janus: unit {name} runs on {hn} -->\n" + PARK_RULE.format(
            submit=f"janus run --detach --host {shlex.quote(b)} --wt {shlex.quote(str(rcwd))} -- '<command>'", report=report or "REPORT.md")
            + quality_block(cfg, report) + swarm_header(cfg, hn, h, name, rcwd, True, brief_text, pred))
    lane = qp(f"{h['root']}/run/{name}.lane")
    return (f"<!-- janus: unit {name} runs on {hn} -->\n**Where you run (janus):** you are on host `{hn}`, in `{rcwd}` (a copy of the worktree; `janus collect` "
            f"brings back a patch of your changes, your REPORT and logs). Run every build and test through this host's pueue lane: "
            f"`{lane} --detach '<command>'` (for example `{lane} --detach 'cargo test -p <crate>'`); it waits for admission and runs there. "
            f"Do not run builds, tests or other heavy commands directly.\n" + PARK_RULE.format(submit=f"{lane} --detach '<command>'", report=report or "REPORT.md")
            + quality_block(cfg, report) + swarm_header(cfg, hn, h, name, rcwd, False, brief_text, pred))

def place(cfg, a, brief, mc, units, hs):
    """Host for the agent: --host; else where its builds and tests run (--test-host, a 'TEST-HOST: <h>' line in the brief, the
    first matching [placement] rule of janus.toml (regex over unit name and brief text = host), or the worktree's sticky host in
    out/linux-host/<unit>), when the model may run there; else the allowed host with the most measured room."""
    allowed = [n for n in cfg.hosts if n in mc.get("hosts", list(cfg.hosts))]
    if a.host: return [a.host], "--host", (a.test_host if a.test_host in cfg.hosts else None)   # (hosts, why, build host): review-fixes8 #1
    txt = brief.read_text() if brief.is_file() else ""
    want = a.test_host
    if not want:
        m = re.search(r"^TEST-HOST:\s*(\S+)", txt, re.M); want = m.group(1) if m else None
    if not want:
        for rx, hn in cfg.c.get("placement", {}).items():
            if re.search(rx, a.unit + "\n" + txt): want = hn; break
    if not want:
        st = cfg.p("out", "out") / "linux-host" / a.unit
        want = st.read_text().strip() if st.is_file() else None
    if want and want in allowed: return [want], f"tests run on {want}", want
    if want and want not in cfg.hosts: want = None
    return allowed, ("tests run on " + want + ", but " + a.unit + "'s model is not allowed there; " if want else "") + "most room", want

def choose(cfg, a, brief, mc, units, hs):
    """(agent host, build host, why) for a new unit, or Err saying why not and when to expect room. The agent host has room for
    one more agent by reservations (room); the build host (where its builds and tests run: placement, else the agent host) has
    a free build reservation or a bounded queue position (build_room, janus-fixes8 item 3)."""
    if a.host and a.host not in cfg.hosts: raise Err(f"unknown host {a.host}")
    hosts, why_place, want = place(cfg, a, brief, mc, units, hs)
    best, bn, notes = None, 0, []
    for n in hosts:
        r, why = room(cfg, n, hs[n], units, a.kind == "cleanup")
        if r < 1: notes.append(f"{n}: {why}"); continue
        b = want or n
        if not hs.get(b, {}).get("ok"): notes.append(f"{n}: build host {b} unreachable"); continue
        ok, bw = build_room(cfg, b, host_cap(cfg, b, hs[b], units), a.kind)
        if not ok: notes.append(bw); continue
        if r > bn: best, bn = (n, b, f"{why_place}; {bw}"), r
    if not best: raise Err("refused, no room (" + "; ".join(dict.fromkeys(notes)) + ")")
    return best

def resume_report(cfg, a, brief):
    """The report a resumed unit continues: --report, else out/<--resume-of>/REPORT.md. None for a new unit (it writes
    out/<unit>/REPORT.md). Never parsed from the brief's prose (a path cut out of prose broke a launch, janus-fixes9); the path
    must be a REPORT file whose folder exists."""
    p = a.report or (str(cfg.p("out", "out") / a.resume_of / "REPORT.md") if a.resume_of else None)
    if not p: return None
    p = Path(os.path.expanduser(p)); p = (p if p.is_absolute() else cfg.root / p).resolve()
    if a.resume_of and not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", a.resume_of): raise Err(f"--resume-of {a.resume_of!r} is not a unit name")
    if not p.parent.is_dir(): raise Err(f"report {p}: its folder does not exist")
    return None if p == (cfg.p("out", "out") / a.unit / "REPORT.md").resolve() else p

DIALOGS = [   # startup dialogs janus gets past (fixes5 item 10): (text in the pane, keys to send)
    (r"trust the (files|contents) (in|of) this (folder|directory)|Do you trust", ["Enter"]),
    (r"Update available|new version of Codex|Skip until next version", ["Down", "Enter"]),
    (r"Press Enter to continue", ["Enter"])]

def past_dialogs(name, pane):
    for _ in range(6):
        r = herdr("pane", "read", pane, "--source", "visible", "--lines", "40") or {}
        txt = json.dumps(r)
        hit = next((k for rx, k in DIALOGS if re.search(rx, txt, re.I)), None)
        st = ((herdr("agent", "get", name) or {}).get("agent") or {}).get("agent_status")
        if not hit: return None if st in ("idle", "working", "done", None) else f"agent {st} at start: {txt[-200:]}"
        herdr("pane", "send-keys", pane, *hit); time.sleep(2)
    return "startup dialog still shown after 6 tries"

def unit_tab(cfg, u, mname, hn, h):
    """One labelled tab per running unit (<unit> · <model> · <host>) that follows its pueue log (fixes5 item 3)."""
    if u.get("via") != "pueue" or cfg.run.dry: return
    try: pane, tab = new_tab(cfg, f"{u['name']} · {mname} · {hn}", cfg.root)
    except Err: return
    f = f"{pueue(h)} follow {int(u['task'])}"
    herdr("pane", "run", pane, f if h.get("local") else f"ssh -t {shlex.quote(h['ssh'])} {shlex.quote(f)}"); u.update(pane=pane, tab=tab)

def cmd_launch(cfg, a):
    name = a.unit
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name): raise Err("unit must match [a-z][a-z0-9_-]{0,31}")
    bd = Beads(cfg); units = bd.units(); prev = next((u for u in units if u["name"] == name), None)
    if prev and a.kind != "review": raise Err(f"{name} already launched on {prev.get('host')}")
    wp = waste_paused(cfg)
    if wp: raise Err(f"refused (waste pause): {wp}")
    brief = Path(a.brief) if a.brief else cfg.p("briefs", "briefs") / f"{name}.md"
    if not brief.is_file(): raise Err(f"brief {brief} not found")
    of = next((u for u in units if u["name"] == (a.of or name)), None)
    if a.model:   # an explicit model passes the same enabled and budget checks as a routed one (M1)
        if a.model not in cfg.models: raise Err(f"unknown model {a.model}")
        ok, w = model_ok(cfg, a.model)
        if not ok: raise Err(f"model {a.model}: {w}")
    mname = a.model or pick_model(cfg, a.tier, a.kind, of.get("vendor") if of else None, a.host)
    mc = cfg.models[mname]
    hs = getattr(a, "probes", None) or probe_all(cfg)
    ag = herdr_agents(cfg) if not getattr(a, "probes", None) else {}
    for x in units: x.setdefault("state", unit_state(x, hs, ag))   # finished units hold no build position
    hn, build, why_place = choose(cfg, a, brief, mc, units, hs)
    h = cfg.hosts[hn]; local = h.get("local", False)
    cwd = cfg.p("worktrees", "worktrees") / name
    cwd = (Path(a.cwd) if a.cwd else cwd if cwd.is_dir() else cfg.root).resolve()
    shared = [u["name"] for u in units if u.get("cwd") and Path(u["cwd"]).resolve() == cwd and u["name"] != name and cwd != cfg.root.resolve()]
    if shared and getattr(a, "share_with", None): shared = [x for x in shared if x != a.share_with]   # janus land's merge unit
    if shared and not (local and a.kind in ("review", "audit")):
        raise Err(f"refused: {cwd} is the cwd of open unit(s) {', '.join(shared)}; give the unit its own worktree (H9)")
    if main_checkout(cwd) and not a.allow_main: raise Err(f"refused: {cwd} is a main checkout with linked worktrees; use a worktree (or --allow-main)")
    if qset(cfg, "brief_lint"):   # [quality] brief_lint: a brief that fails is refused with every reason (opt-in, item 5)
        probs = lint_brief(cfg, brief, cwd=cwd, reachable=local)
        if probs: raise Err(f"brief {brief} fails the lint ([quality] brief_lint = true): " + "; ".join(probs))
    outd = cfg.p("out", "out") / name; outd.mkdir(parents=True, exist_ok=True)
    rbrief, rcwd = str(brief), str(cwd)
    budget = None
    if mc["kind"] == "api":
        budget, bwhy = (a.budget, "--budget") if a.budget is not None else budget_for(cfg, mc, a.kind, a.tier)
        why_place += f"; budget ${budget:g} ({bwhy})"
    say(f"launch {name}: model={mname} host={hn} builds on {build} ({why_place}) cwd={cwd}")
    btext = brief.read_text()
    if not local:   # every path the brief names must reach the agent there (refused before anything is recorded)
        btext, bcopy, bnotes = brief_plan(cfg, h, name, cwd, btext)
        for n_ in bnotes: say(f"note: {n_}")
        if bcopy or btext != brief.read_text(): say(f"brief for {hn}: {len(bcopy)} file(s) copied to {h['root']}/ctx/{name}, paths rewritten")
    if mc["kind"] == "api":
        read_secret(cfg.prov[mc["provider"]])   # the secret store answers before anything is reserved or recorded
        ok, w = model_ok(cfg, mname)
        if not ok and "ledger" in w: raise Err(f"refused: {w}")
    # A resumed unit continues its original report (--report or --resume-of only, never a path from the brief's prose); the
    # unit records it, so check and the Stop hook read that report.
    report = resume_report(cfg, a, brief)
    if report and report.exists():
        t = report.read_text().split("\n", 1)
        if t[0].startswith("VERDICT:") and "PENDING" not in t[0] and not cfg.run.dry: atomic_write(report, "VERDICT: PENDING\n" + (t[1] if len(t) > 1 else ""))
    # Record the unit before anything starts (fixes5 item 10), claimed in_progress so bd ready never lists it (H7).
    pred = predecessor_name(cfg, name, a.resume_of)   # RELATED: names and the predecessor reach the brief (imp-awareness)
    u = {"v": UNIT_V, "host": hn, "build": build, "model": mname, "vendor": mc.get("vendor"), "kind": a.kind, "tier": a.tier, "brief": str(brief),
         "cwd": str(cwd), "rcwd": rcwd, "started": int(now()), "via": "starting", "budget": budget}
    if report: u["report"] = str(report); u["resume"] = True
    item = a.item or (re.search(r"^ITEM:\s*(\S+)", brief.read_text(), re.M) or [None, None])[1]
    if item: u["item"] = item
    if cfg.run.dry: say(f"+ bd create unit:{name}; bd update --status in_progress")
    else:
        u["id"] = bd.create(f"unit:{name}", "unit", f"{a.kind} on {hn} with {mname}"); u["name"] = name
        bd.run("update", u["id"], "--status", "in_progress"); bd.save(u)
    env = {"JANUS_ROLE": "agent", "JANUS_REPORT": str(report or outd / "REPORT.md"), "JANUS_UNIT": name, "JANUS_ROOT": str(cfg.root),
           **({"BOARD_DIR": str(board_dir(cfg))} if local else {})}   # remote: <root>/bin/board.sh finds <root>/board itself
    try:
        if not local:
            root = h["root"]; rcwd = f"{root}/worktrees/{name}"; rbrief = f"{root}/briefs/{name}.md"
            env["JANUS_REPORT"] = f"{root}/out/{name}/REPORT.md"
            rc_, o_, _ = X(cfg, h, "mkdir -p " + " ".join(qp(x) for x in (rcwd, f"{root}/briefs", f"{root}/out/{name}", f"{root}/run", f"{root}/base"))
                           + f" && cd {qp(rcwd)} && pwd -P")
            if rc_ == 0 and o_.strip().startswith("/"): u["rcwd_abs"] = o_.strip().splitlines()[-1]   # the transcript folder's name (#4)
            cmd = ["rsync", "-a", "--delete", "-e", " ".join(SSH), *EXCL, f"{cwd}/", f"{h['ssh']}:{rcwd}/"]
            if cfg.run.dry: say("+ " + shlex.join(cmd))
            elif sh(cmd, timeout=900)[0]: raise Err("rsync to " + hn + " failed")
            # base/<unit>: the tree as sent, so collect can bring back only the agent's changes as a patch (H9)
            X(cfg, h, f"rm -rf {qp(f'{root}/base/{name}')} && cp -a {qp(rcwd)} {qp(f'{root}/base/{name}')}", timeout=300)
            put_script(cfg, h, f"{root}/run/{name}.lane", lane_runner(cfg, hn, h, name, rcwd))
            if not cfg.paths.get("board"):
                for n, t in board_shipped(cfg): put_script(cfg, h, f"{root}/bin/{n}", t)   # the shipped board there (outbox)
            if not cfg.run.dry: write_cap(cfg, hn, h)   # the lane runner's admission and the agent's reservation use it
            ship_context(cfg, h, name, bcopy)
            put_script(cfg, h, rbrief, brief_header(cfg, hn, h, name, rcwd, local, build, env["JANUS_REPORT"], brief_text=btext, pred=pred) + btext, "644")
            if report and report.exists():   # a resumed unit continues its original report there; collect copies it back
                put_script(cfg, h, f"{root}/out/{name}/REPORT.md", report.read_text(), "644")
            else: X(cfg, h, f"[ -s {qp(env['JANUS_REPORT'])} ] || echo 'VERDICT: PENDING' > {qp(env['JANUS_REPORT'])}")
        rep = outd / "REPORT.md"
        if not report and not rep.exists() and not cfg.run.dry: rep.write_text("VERDICT: PENDING\n")
        if local:   # the brief with janus's header (the park rule) as the agent reads it: out/<unit>/<brief name>
            rbrief = str(outd / (brief.name if (outd / brief.name).resolve() != brief.resolve() else "janus-" + brief.name))
            if not cfg.run.dry: atomic_write(Path(rbrief), brief_header(cfg, hn, h, name, rcwd, True, build, env["JANUS_REPORT"], brief_text=btext, pred=pred) + brief.read_text())
        u["rbrief"] = rbrief
        prompt = agent_prompt(cfg, name, rbrief, hn, env["JANUS_REPORT"])
        u["rcwd"] = rcwd
        if mc["kind"] == "api":
            X(cfg, h, ensure_lanes(h, cfg))
            if local and not cfg.run.dry: write_cap(cfg, hn, h)
            # the budget is reserved (cap check and the run's running row, under the ledger's lock) before anything is queued
            run = reserve_budget(cfg, mc, name, budget); u.update(run=run, runs=[run])
            if local:
                cmd = shlex.join([sys.executable, str(JANUS_SELF), "api-run", name, "--run", run, "--model", mname, "--budget", str(budget),
                                  "--brief", rbrief, "--cwd", rcwd, "--report", env["JANUS_REPORT"]])
                rc, o, e = X(cfg, h, f"{pueue(h)} add -g agents -l {name} -w {shlex.quote(rcwd)} --print-task-id -- {pq(cmd)}", env=env)
            else:
                rc, o, e = api_remote(cfg, h, name, mc, rbrief, rcwd, budget, prompt, env, run=run)
            if rc and not cfg.run.dry: ledger_done(cfg, cfg.prov[mc["provider"]], run, name, 0, "launch-failed")
            if rc: raise Err("pueue add failed: " + (e or o).strip()[:100])
            u.update(via="pueue", task=int(o.strip() or 0))
            if not cfg.run.dry:   # launched means started (or queued for a slot, or already done), never a run that died at once
                st, why = confirm_started(cfg, h, u["task"])
                if st == "died":
                    ledger_done(cfg, cfg.prov[mc["provider"]], run, name, 0, "died-at-start")
                    raise Err(f"the run died at once ({why}); nothing is running")
                u["start_state"] = st
                say(f"{name}: run {st}" + (" (waiting for an agents slot)" if st == "queued" else " (exit 0)" if st == "finished" else
                                            "" if st == "running" else f": not confirmed running (pueue {hn}:{u['task']} is {st})"))
            unit_tab(cfg, u, mname, hn, h)
        elif local:
            pane, tab = new_tab(cfg, f"{name} · {mname} · {hn}", cwd) if not cfg.run.dry else ("pane", "tab")
            u.update(pane=pane, tab=tab)
            if not cfg.run.dry:
                wait_shell(pane, name)
                envs = {**env, **({"OPENCODE_CONFIG_CONTENT": json.dumps({"model": mc["model"]})} if mc["kind"] == "opencode" else {})}
                # the pane's shell inherited herdr's environment: drop what must not reach an agent, then set the unit's own
                herdr("pane", "run", pane, "unset ANTHROPIC_API_KEY CLAUDE_CODE_MESSAGING_TOKEN HERDR_PANE_ID CLAUDE_CODE_SESSION_ID; "
                      + "; ".join(f"export {k}={shlex.quote(v)}" for k, v in envs.items()) + f"; echo env-'{name}'")
                sh(["herdr", "pane", "wait-output", pane, "--match", f"env-{name}", "--timeout", "10000"], timeout=15)
                r = sh(["herdr", "agent", "start", name, "--kind", mc["kind"], "--pane", pane, "--timeout", "90000", "--", *mc.get("args", [])], timeout=100)
                d = past_dialogs(name, pane)
                if r[0] and d: raise Err("herdr agent start failed: " + (r[2] or d)[:160])
                if d: raise Err(f"{name}: {d}")
                herdr("agent", "prompt", name, prompt)
                u.update(via="herdr", pid=findkey(herdr("pane", "process-info", "--pane", pane), "pid"))
                agent_res(cfg, hn, h, name, u.get("pid"))
            else: say(f"+ herdr tab create / agent start {name} --kind {mc['kind']} / agent prompt")
        else:
            cli = {"claude": "claude", "codex": "codex", "opencode": "opencode run"}[mc["kind"]]
            e = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            if mc["kind"] == "opencode": e += " OPENCODE_CONFIG_CONTENT=" + shlex.quote(json.dumps({"model": mc["model"]}))
            mm = h.get("memory_max"); sd = f"systemd-run --user --scope -q -p {shlex.quote(f'MemoryMax={mm}')} -- " if mm else ""   # fixes5 item 7
            inner = f"cd {qp(rcwd)} && exec env {e} {sd}{cli} {shlex.join(mc.get('args', []))} {shlex.quote(prompt)}"
            pre = f'PATH="$HOME/.local/bin:{h.get("bin", "")}:$PATH"; '
            rc, o, er = X(cfg, h, pre + f"tmux new -d -s janus-{name} {shlex.quote(inner)} && tmux display -p -t janus-{name} '#{{pane_pid}}'")
            if rc: raise Err("tmux start failed: " + er.strip()[:100])
            u.update(via="tmux", pid=int(o.strip() or 0))
            if not cfg.run.dry:   # launched means the agent's session is alive a moment later (a broken command line ends it at once)
                time.sleep(3)
                if hrun(h, pre + f"tmux has-session -t janus-{name}")[0] or not u["pid"]:
                    raise Err(f"the agent's tmux session janus-{name} ended at once; nothing is running")
                u["start_state"] = "running"
            if not cfg.run.dry: agent_res(cfg, hn, h, name, u["pid"])
            if not cfg.run.dry:
                pane, tab = new_tab(cfg, f"{name} · {mname} · {hn}", cwd)
                herdr("pane", "run", pane, f"ssh -t {shlex.quote(h['ssh'])} tmux attach -t janus-{name}"); u.update(pane=pane, tab=tab)
    except Err as ex:
        if not cfg.run.dry:
            u["via"] = "failed"; bd.save(u); bd.comment(u["id"], f"{ts()} launch failed: {ex}")
            bd.run("close", u["id"], "--reason", "launch failed")
            if u.get("tab"): herdr("tab", "close", u["tab"])
        raise
    if cfg.run.dry: return
    bd.save(u); bd.comment(u["id"], f"{ts()} launched {a.kind} on {hn} model {mname} via {u['via']}"); cfg.touch("changed")
    chk = f"herdr {name}" if u["via"] == "herdr" else f"pueue {hn}:{u['task']}" if u["via"] == "pueue" else f"tmux {hn}:janus-{name}"
    board(cfg, "post", name, "status", f"launched ({mname}, {hn})",
       env={"BOARD_MACHINE": hn, "BOARD_BRIEF": str(brief), "BOARD_CHECK": chk, "BOARD_REPORT": str(report_path(cfg, u))})   # row 26: a resumed unit's own report
    st = u.get("start_state", "running" if u["via"] == "herdr" else "unknown")
    say((f"launched {name} on {hn} via {u['via']}" if st in ("running", "queued", "finished") else f"queued {name} on {hn} via {u['via']}, NOT confirmed running ({st})")
        + (f" (task {u['task']}{', waiting for an agents slot' if st == 'queued' else ''})" if u.get("task") is not None else ""))

PATH_RX = re.compile(r"(?<![\w.:/~$>)\]}*-])(/[A-Za-z0-9._@+-][A-Za-z0-9._@+/-]*)")

def brief_paths(text):
    """Every absolute path the brief names (trailing dots stripped), in order, once."""
    return list(dict.fromkeys(m.group(1).rstrip(".") for m in PATH_RX.finditer(text) if len(m.group(1)) > 1))

def brief_plan(cfg, h, name, cwd, text):
    """(text for the remote agent, [(local path, path relative to the project root)] to copy, notes). A path in the unit's tree
    becomes the same path in its copy there; a path under the project's root (reports, logs, briefs) that exists here is copied
    to <root>/ctx/<unit>/ and rewritten to the copy (the root itself: that folder); a missing one under out/<unit> becomes the
    host's out/<unit>; any other path must exist on the host. Raises Err naming every path the agent could not reach."""
    root = h["root"]; ctx = f"{root}/ctx/{name}"; pr = cfg.root.resolve(); cw = Path(cwd).resolve()
    rep, copy, ask, notes = {}, [], [], []
    for p in brief_paths(text):
        q = Path(p)
        try: rq = q.resolve()
        except OSError: rq = q
        if p.startswith(str(root).rstrip("/") + "/") or p == root: continue   # already a path there
        if rq == cw or cw in rq.parents: rep[p] = f"{root}/worktrees/{name}" + ("/" + str(rq.relative_to(cw)) if rq != cw else ""); continue
        if rq == pr: rep[p] = ctx; notes.append(f"{p} is the project root: named as {ctx} (only the files the brief names are copied)"); continue
        if pr in rq.parents:
            rel = rq.relative_to(pr)
            if q.exists(): rep[p] = f"{ctx}/{rel}"; copy.append((str(rq), str(rel))); continue
            if rel.parts[:2] == ("out", name): rep[p] = f"{root}/{rel}"; continue
            ask.append(p); continue
        ask.append(p)
    if pr in [Path(p).resolve() for p in rep if rep[p] == ctx]:   # the root is named (B = <root>): B/out/x.md and out/x.md files too
        have = {r for _, r in copy}
        for m in re.finditer(r"(?<![\w./~$-])([A-Za-z0-9._-]+(?:/[A-Za-z0-9._@+-]+)+)", text):
            t = m.group(1).rstrip("."); parts = t.split("/")
            for rel in ("/".join(parts), "/".join(parts[1:])):
                f = pr / rel
                if rel and rel not in have and f.is_file() and pr in f.resolve().parents and not f.resolve().is_relative_to(cw):
                    have.add(rel); copy.append((str(f.resolve()), rel)); break
    if ask:   # must exist on the host as named (a system path, a path of that host)
        rc, o, e = hrun(h, "for p in " + " ".join(shlex.quote(x) for x in ask) + '; do [ -e "$p" ] || echo "$p"; done', timeout=30)
        missing = [x for x in o.splitlines() if x.strip()] if rc == 0 else ask
        if missing: raise Err(f"refused: the brief names path(s) the agent on that host cannot reach (not in its tree, not under the project root here, "
                              f"not on the host): {', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}")
    for p in sorted(rep, key=len, reverse=True): text = re.sub(r"(?<![\w.:/~$>)\]}*-])" + re.escape(p) + r"(?![A-Za-z0-9_@+/-]|\.[A-Za-z0-9_@+/-])", lambda m, r=rep[p]: r, text)
    return text, copy, notes

def ship_context(cfg, h, name, copy):
    """Copy the brief's files under the project root to <root>/ctx/<unit>/ on the host (one rsync, relative paths kept)."""
    if not copy: return
    ctx = f"{h['root']}/ctx/{name}"
    if cfg.run.dry: say(f"+ copy to {h.get('ssh')}:{ctx}/: " + ", ".join(r for _, r in copy)); return
    X(cfg, h, f"mkdir -p {qp(ctx)}")
    lst = "".join(r + "\n" for _, r in copy)
    rc, o, e = sh(["rsync", "-a", "-r", "--files-from=-", "-e", " ".join(SSH), f"{cfg.root.resolve()}/", f"{h['ssh']}:{ctx}/"], inp=lst, timeout=600)
    if rc: raise Err(f"copying the brief's files to {h.get('ssh')} failed: {(e or o).strip()[-160:]}")

# A brief must say what the unit does and how it is judged before anything runs: the lint names every missing part. It is off
# unless [quality] brief_lint is set (the owner's projects carry briefs of every shape; a project turns it on once its briefs
# carry the markers). The five parts are the tier line, a scope, the report path, done-criteria, and reachable paths.
TIER_RX = re.compile(r"(?im)^\s*\**\s*TIER\s*[:=]\s*(\S+)")
SCOPE_RX = re.compile(r"(?im)(?:^#{1,6}\s*scope\b)|(?<![\w-])(?:scope|in[- ]scope|out[- ]of[- ]scope|modules|your modules)\s*:")
REPORT_RX = re.compile(r"(?i)report\.(?:md|json)")
DONE_RX = re.compile(r"(?im)^\s*(?:[-*>]\s*)?\**\s*(done[- ]when|done|proof|acceptance|done[- ]criteria|accepted|exit codes?)\b\s*[:=-]")

def lint_brief(cfg, brief, text=None, cwd=None, reachable=True):
    """Problems that make a brief unfit to launch ([] when it is good): no tier line, no scope, no report path, no
    done-criteria, or a path under the project root it names that is not there. `reachable=False` skips the path check (a remote
    launch's paths are enforced by brief_plan). Reads the brief unless text is given."""
    t = brief.read_text() if text is None else text
    P = []
    if not TIER_RX.search(t): P.append("no tier line: add 'TIER: NORMAL' or 'TIER: CRITICAL'")
    if not SCOPE_RX.search(t): P.append("no scope: add 'SCOPE:' (or 'Your modules:') naming what the unit may touch")
    if not REPORT_RX.search(t): P.append("no report path: name REPORT.md, where the unit writes its report")
    if not DONE_RX.search(t): P.append("no done-criteria: add 'DONE-WHEN:' or 'PROOF:' saying what proves the unit done")
    if reachable:
        root = cfg.root.resolve(); cw = Path(cwd).resolve() if cwd else None
        for p in brief_paths(t):
            q = Path(p)
            try: rq = q.resolve()
            except OSError: rq = q
            if cw and (rq == cw or cw in rq.parents): continue          # in the unit's own tree: it is what it edits
            if rq == root or root in rq.parents:
                rel = rq.relative_to(root) if rq != root else Path(".")
                if not rq.exists() and rel.parts[:1] != ("out",):       # out/<unit> is created at launch
                    P.append(f"path {p} is not reachable: under the project root but missing")
    return P
