"""janus gate (and its gate-exec, gate-judge and gate-scope helpers), janus land and janus commit-state."""
import argparse, fnmatch, json, os, re, shlex, signal, subprocess, sys, time, unicodedata
from pathlib import Path
from janus.actions.jobs import cmd_run, ensure_lanes, gate_group, task_output, task_rc, tree_guard
from janus.actions.launch import cmd_launch
from janus.actions.swarm import write_index
from janus.beads import Beads
from janus.context import Err, JANUS_SELF, X, atomic_write, hrun, note_event, now, pq, qp, say, sh, ts
from janus.gates import GATE_DEFAULT, GATE_LINE, baseline_failures, gate_baseline, gate_ids, judge_gate, landed, unapplied_patch
from janus.landhost import HostGit, identity, land_clone, land_host, land_tree, lane_script, origin_url, ssh_url
from janus.platform import group_cpu_s, tree_groups_sh
from janus.project import own_label, tag
from janus.probe import pueue
from janus.quality import land_review
from janus.units import all_units, get_unit, own_tree

def cmd_gate_judge(cfg, a):
    r, new, known = judge_gate(cfg, a.name, a.rc)
    b = gate_baseline(cfg, a.name)
    base = (f" BASE={b}({len(baseline_failures(cfg, a.name))})" if b else "")
    extra = (f" KNOWN={len(known)}" if known else "") + base + (f" NEW={','.join(new[:20])[:400]}" if new and new != [f"exit {a.rc}"] else "")
    return 0 if r == "PASS" else 1, [f"{time.strftime('%Y-%m-%d %H:%M:%S')} {a.name} EXIT_tests={a.rc}{extra} RESULT: {r}"]

def gate_times(cfg): return cfg.jget("gate-times.json")

# The lines of a gate's own output that look like the first error (the soak-40 need: `janus queue --log <gate task>` printed only
# host headers and gate-judge only the RESULT line, so nothing showed why a gate failed).
GERR_RX = re.compile(r"(?i)\b(error|(?-i:\w+(Error|Exception))|failed|failure|assert(ion)?|panic(ked)?|traceback|not found|no such file|expected|"
                     r"exit(ed)? [1-9]|exit code [1-9])\b")

# A step's own log (out/<gate>/steps/<step>.log, where a gate script sends each step's output): the lines naming a failed test
# (cargo test, nextest, pytest, unittest) and the lines that only look like an error (a passing test whose name holds "failed").
STEP_FAIL_RX = re.compile(r"^(?:test (.+?) \.\.\. FAILED|(?:FAILED|ERROR) (\S+::\S+)(?: - .*)?|(\S+::\S+) (?:FAILED|ERROR)\b.*|"
                          r"\s*FAIL \[[^\]]*\]\s+(\S.*?)|(?:FAIL|ERROR): (\S.*?))\s*$")
STEP_OK_RX = re.compile(r"\.\.\. ok\s*$|^PASSED\b|\bPASSED\b[^:]*$|^test result:|^\s*(PASS|ok)\b|^janus: ")   # janus: its own job notices

def step_failures(text):
    """(failing test ids, first error line or None, the CACHED PASS line or None) of one step's log."""
    ids, err, cached = [], None, None
    for l in text.splitlines():
        m = STEP_FAIL_RX.match(l)
        if m: ids += [i for i in (next(g for g in m.groups() if g),) if i not in ids]
        elif l.startswith("CACHED PASS "): cached = l.strip()
        elif err is None and GERR_RX.search(l) and not STEP_OK_RX.search(l): err = l.strip()
    return ids, err, cached

def step_lines(sd, steps):
    """The lines gate-log prints from the step logs: per failing step (every step log when the gate's output named none) its
    failing test ids and first error line; per step answered by the result cache, its CACHED PASS line."""
    L = []; rcs = dict(steps)
    for f in sorted(sd.glob("*.log")) if sd.is_dir() else []:
        n = f.stem; ids, err, cached = step_failures(f.read_text(errors="replace"))
        if cached: L.append(f"step {n}: {cached}")
        if rcs.get(n, "1" if not rcs and (ids or err) else "0") == "0": continue
        L.append(f"step {n}: failed test(s) ({len(ids)}): " + ", ".join(ids[:20]) + ("..." if len(ids) > 20 else "") if ids else f"step {n}: no failing test id in {f}")
        if err: L.append(f"  first error: {err[:220]}")
    return L

def cmd_gate_log(cfg, a):
    """janus gate-log <name> [--task <host>:<id>]: a failed gate's per-step summary and first error line. The RESULT line and
    failing/passing test ids come from the gate log and out/<name>/{failed,passed}.txt; the per-step EXIT_<step>=<rc> lines and
    the first error come from the gate's own output (the pueue task it was queued as, recorded in out/<name>/task.txt, or
    --task, or a saved out/<name>/log). Read-only."""
    name = a.name; out = cfg.p("out", "out"); L = []
    try: ls = [l for l in cfg.p("gate_log").read_text().splitlines() if (m := GATE_LINE.match(l)) and m.group(1) == name]
    except OSError: ls = []
    last = ls[-1] if ls else None
    if last: L.append("result: " + last)
    else: L.append(f"gate {name}: no RESULT line in {cfg.p('gate_log')}")
    f_ids = gate_ids(cfg, name, "failed")
    if f_ids: L.append(f"failed test(s) ({len(f_ids)}): " + ", ".join(f_ids[:20]) + ("..." if len(f_ids) > 20 else ""))
    p_ids = gate_ids(cfg, name, "passed")
    if p_ids is not None: L.append(f"passed test(s): {len(p_ids)}")
    log = None; ref = a.task or ((out / name / "task.txt").read_text().strip() if (out / name / "task.txt").is_file() else "")
    if ref:
        hn, _, tid = ref.partition(":"); h = cfg.hosts.get(hn)
        if h and tid.strip().isdigit():
            rc, o = task_output(cfg, hn, tid.strip())
            if rc == 0: log = o
    if log is None:
        for f in (out / name / "log", out / name / "gate.log"):
            if f.is_file(): log = f.read_text(errors="replace"); break
    steps = re.findall(r"EXIT_(\w+)=(\d+)", log or "") or re.findall(r"EXIT_(\w+)=(\d+)", last or "")   # the RESULT line names them too
    SL = step_lines(out / name / "steps", steps)   # row 228: a gate whose steps write their own logs has its test ids and errors there
    if log is None:
        L.append("no gate output found (recorded task out/<name>/task.txt, --task <host>:<id>, or out/<name>/log)")
    else:
        if steps: L.append("steps: " + ", ".join(f"{s}=rc{r}" for s, r in steps))
        errs = [l.strip() for l in log.splitlines() if GERR_RX.search(l)]
        if errs or not SL:
            L.append("first error line(s):" if errs else "no error-looking line; last lines:")
            for l in (errs[:5] if errs else log.splitlines()[-8:]): L.append("  " + l[:220])
    L += SL
    return (0 if last and last.endswith("RESULT: PASS") else 1), L

def gate_estimate(cfg, name):
    """Minutes a gate is expected to take: the median of its own last runs, else of the gates of the same family (the name with
    its unit part replaced: land-<unit>-x -> land-*-x), else None."""
    t = gate_times(cfg)
    def fam(n): return re.sub(r"^(land-)?[^-]+", r"\1*", n)
    xs = t.get(name) or [v for k, vs in t.items() if fam(k) == fam(name) for v in vs]
    if not xs: return None
    xs = sorted(xs[-10:]); return xs[len(xs) // 2]

def cmd_gate_exec(cfg, a):
    """The gate wrapper (pueue runs it): clears the run's failed/passed lists, runs the gate's command in its own process group,
    stops the group at [landing] gate_timeout_min (then writes a RESULT: FAIL line with EXIT_timeout=124), records the duration."""
    od = cfg.p("out", "out") / a.name
    for w in ("failed", "passed"): (od / f"{w}.txt").unlink(missing_ok=True)
    t0 = now(); lim = (a.timeout_min or 0) * 60 or None
    c = a.command[1:] if a.command[:1] == ["--"] else a.command
    p = subprocess.Popen(["sh", "-c", "--", c[0] if len(c) == 1 else shlex.join(c)], start_new_session=True)
    def stop_group():   # row 237: every group of the command's process tree (a runner that started its own group outlived the cancel)
        gs = {p.pid} | {int(x) for x in sh(["sh", "-c", tree_groups_sh(p.pid)])[1].split() if x.isdigit()}
        for sg, grace in ((signal.SIGTERM, 20), (signal.SIGKILL, 5)):
            for g in gs:
                try: os.killpg(g, sg)
                except (ProcessLookupError, PermissionError): pass
            try: p.wait(timeout=grace)
            except subprocess.TimeoutExpired: pass
            if sg == signal.SIGTERM and len(gs) > 1: time.sleep(1)   # the other groups get a moment to end on TERM
    # Row 219: the command runs in its own process group, so a cancel (pueue kill) that stops only this wrapper left the gate's
    # runner and tests.sh running: they queued later steps and wrote a RESULT line after the cancel. A signal here stops the whole
    # group and writes no RESULT; the group id is also kept in runs/<token>.pgid, so janus cancel stops the group even when this
    # wrapper is killed outright. Row 214: runs/<token>.live is the gate's progress mark (its group used CPU), like a job's.
    tok = os.environ.get("JANUS_WRAP", ""); rd = Path(next(h for h in cfg.hosts.values() if h.get("local"))["_rd"])
    marks = [rd / f"{tok}.pgid", rd / f"{tok}.live"] if re.fullmatch(r"[\w.-]+", tok) else []
    if marks: rd.mkdir(parents=True, exist_ok=True); marks[0].write_text(f"{p.pid}\n"); marks[1].write_text(f"{int(now())}\n")
    class Cancelled(Exception): pass
    def on_signal(sig, frame): raise Cancelled(sig)
    for sg in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(sg, on_signal)
    rc = None; last = None
    try:
        while rc is None:
            left = lim - (now() - t0) if lim else 60
            if left <= 0: break
            try: rc = p.wait(timeout=min(60, left))
            except subprocess.TimeoutExpired:
                cpu = group_cpu_s(p.pid)
                if marks and cpu is not None and (last is None or abs(cpu - last) >= 2): marks[1].write_text(f"{int(now())}\n")
                last = cpu if cpu is not None else last
    except Cancelled as e:
        for sg in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(sg, signal.SIG_IGN)
        stop_group()
        for m in marks: m.unlink(missing_ok=True)
        say(f"gate {a.name} cancelled (signal {e.args[0]}): its process group is stopped and no RESULT line is written")
        return 128 + int(e.args[0])
    for m in marks: m.unlink(missing_ok=True)
    if rc is None:
        stop_group(); rc = 124
        with open(cfg.p("gate_log", "out/gates.log"), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {a.name} EXIT_timeout=124 RESULT: FAIL\n")
        say(f"gate {a.name} stopped at its {a.timeout_min} min limit")
    t = gate_times(cfg); t[a.name] = [*t.get(a.name, []), round((now() - t0) / 60, 1)][-10:]
    if rc != 124: cfg.jput("gate-times.json", t)   # a stopped run says nothing about the gate's length
    return rc

STEP_RX = re.compile(r"^(?:@([A-Za-z0-9._-]+)\s+)?([A-Za-z0-9._-]+):\s*(.+)$")

def scope_steps(cfg, unit, tree):
    """[(host or None, step, command)] the project's affected-scope hook ([landing] scope_cmd, called with {unit} {tree}) prints
    for a tree: one step per line, '[@host ]<step>: <command>'. A step with no host runs on this machine (portable steps)."""
    sc = cfg.c.get("landing", {}).get("scope_cmd")
    if not sc: raise Err("no [landing] scope_cmd in janus.toml (the project's hook that prints a tree's gate steps)")
    cmd = sc.replace("{unit}", shlex.quote(unit)).replace("{tree}", shlex.quote(str(tree)))
    rc, o, e = sh(["sh", "-c", cmd], cwd=cfg.root, env={"JANUS_UNIT": unit, "JANUS_TREE": str(tree)}, timeout=300)
    if rc: raise Err(f"scope_cmd failed ({rc}): {(e or o).strip()[:200]}")
    st = []
    for l in o.splitlines():
        if not l.strip() or l.lstrip().startswith("#"): continue
        m = STEP_RX.match(l.strip())
        if not m: raise Err(f"scope_cmd printed a line that is not '[@host ]<step>: <command>': {l[:120]}")
        h = m.group(1)
        if h and h not in cfg.hosts: raise Err(f"scope_cmd names unknown host {h}")
        st.append((None if h and cfg.hosts[h].get("local") else h, m.group(2), m.group(3)))
    return st

def scope_script(cfg, unit, tree, steps):
    """tests.sh for the steps: each runs (here in the tree, or janus run on its host), prints EXIT_<step>=<rc>; exit 1 on any."""
    L = ["#!/bin/bash", f"# gate for {unit} generated by janus gate-scope from [landing] scope_cmd; tree {tree}", "fail=0"]
    hosts = {h for h, _, _ in steps}
    if len(hosts) == 1 and None not in hosts: L.insert(2, f"# janus-gate-host: {hosts.pop()}")   # row 245: janus gate queues it in that host's group
    me = shlex.join([sys.executable, str(JANUS_SELF)])
    for h, n, c in steps:
        run = f"(cd {shlex.quote(str(tree))} && {c})" if not h else f"{me} run --host {shlex.quote(h)} --lane jobs --unit {shlex.quote(unit)} --wt {shlex.quote(str(tree))} -- {shlex.quote(c)}"
        L.append(f"{run}; rc=$?; echo EXIT_{n}=$rc; [ $rc -eq 0 ] || fail=1")
    L.append("exit $fail"); return "\n".join(L) + "\n"

def cmd_gate_scope(cfg, a):
    bd, u = get_unit(cfg, a.unit); name = a.name or f"land-{a.unit}"
    steps = scope_steps(cfg, a.unit, u["cwd"]); txt = scope_script(cfg, a.unit, u["cwd"], steps)
    if cfg.run.dry: say(txt.rstrip()); say(f"(dry run: would write out/{name}/tests.sh; then janus gate {name} --unit {a.unit})"); return
    t = cfg.p("out", "out") / name / "tests.sh"; t.parent.mkdir(parents=True, exist_ok=True); atomic_write(t, txt); t.chmod(0o755)
    est = gate_estimate(cfg, name)
    say(f"wrote {t}: {len(steps)} step(s) ({', '.join(n + ('@' + h if h else '') for h, n, _ in steps)})"
        + (f"; expected {est:g} min" if est is not None else "") + f"; queue it: janus gate {name} --unit {a.unit}")

def gate_unit(cfg, name):
    """The open unit a landing gate's name carries when no --unit is given (land-<unit> or land-<unit>-<suffix>, and the
    [landing] gate_names patterns that start with land-; the longest unit name wins), or None."""
    pats = [x for x in cfg.c.get("landing", {}).get("gate_names", []) if x.startswith("land-")] or ["land-{unit}", "land-{unit}-*"]
    try: names = [u["name"] for u in Beads(cfg).units()]
    except Err: return None
    return max((n for n in names if any(fnmatch.fnmatch(name, x.replace("{unit}", n)) for x in pats)), key=len, default=None)

GATE_HOST_RX = re.compile(r"#\s*janus-gate-host:\s*([A-Za-z0-9._-]+)\s*$")

def cmd_gate(cfg, a):
    """Run out/<name>/tests.sh as a gates-lane task on this machine (in the group gates-<host> when its steps run on another host). paths.gate_runner (a project script called with <name>)
    runs it when set; otherwise janus runs tests.sh itself and appends '<date> <time> <name> EXIT_tests=<rc> RESULT: PASS|FAIL'
    to paths.gate_log, the line janus check reads. --host passes the test host in paths.gate_host_env (default JANUS_TEST_HOST).
    --unit (default $JANUS_UNIT) records the gate on that unit, for the landing check."""
    out = cfg.p("out", "out"); t = out / a.name / "tests.sh"
    if not t.is_file(): raise Err(f"{t} not found")
    ln, h = next((n, x) for n, x in cfg.hosts.items() if x.get("local"))
    # Row 217: a gate that is already queued or running is not queued again (the duplicate failed at once and task.txt then named it)
    try: tasks = (json.loads(hrun(h, f"{pueue(h)} status --json", timeout=30)[1]).get("tasks") or {})
    except ValueError: tasks = {}
    dup = next((i for i, x in tasks.items() if own_label(cfg, x) == f"gate:{a.name}" and task_rc(x) is None), None)
    if dup is not None:
        raise Err(f"refused: gate {a.name} is already queued or running as {ln}:{dup}: wait for it (janus gate-log {a.name}), or janus cancel {ln}:{dup} first")
    # Baseline (decision 0002): --baseline <name> records a run of the landing branch on the same host whose failures are main's,
    # not the unit's; --baseline none clears it. The gate script's gate-judge reads out/<name>/baseline.txt.
    bl = getattr(a, "baseline", None)
    if bl == "none": (out / a.name / "baseline.txt").unlink(missing_ok=True)
    elif bl:
        if not (out / bl / "failed.txt").is_file() and not (out / bl / "tests.sh").is_file():
            raise Err(f"--baseline {bl}: no recorded gate out/{bl} (a run of the landing branch on the same host); run it first")
        (out / a.name).mkdir(parents=True, exist_ok=True); atomic_write(out / a.name / "baseline.txt", bl + "\n")
        say(f"gate {a.name}: baseline {bl} (its failures are treated as the branch's, not the unit's)")
    unit = a.unit or os.environ.get("JANUS_UNIT") or gate_unit(cfg, a.name)
    # Row 216: the gate's task carries its unit, so the steps it queues (janus run reads JANUS_UNIT) are recorded on that unit
    env = {"JANUS_LANE": "gates", "JANUS_ROOT": str(cfg.root)} | ({cfg.paths.get("gate_host_env", "JANUS_TEST_HOST"): a.host} if a.host else {}) | ({"JANUS_UNIT": unit} if unit else {})
    pre = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    rh = cfg.paths.get("gate_runner", "")
    run = (shlex.quote(str(cfg.root / rh)) + " " + shlex.quote(a.name)) if rh else \
          shlex.join(["sh", "-c", GATE_DEFAULT, "gate", str(t), a.name, str(cfg.p("gate_log", "out/gates.log")), sys.executable, str(JANUS_SELF)])
    ld = cfg.c.get("landing", {}); lim = a.timeout_min if a.timeout_min is not None else ld.get("gate_timeout_min", 0)
    run = shlex.join([sys.executable, str(JANUS_SELF), "gate-exec", a.name, "--timeout-min", str(lim or 0), "--", run])
    est = gate_estimate(cfg, a.name); qm = ld.get("quick_gate_min")
    group = "gates-quick" if qm and (a.quick or (est is not None and est < qm)) else "gates"   # short gates never wait behind long ones
    # Row 245 (every host is independent): a gate whose steps run on another host (--host, or a `# janus-gate-host: <host>` line in
    # the head of its script) only waits for them here, so it takes that host's own group and never a local gates slot.
    gh = a.host or next((m.group(1) for l in t.read_text(errors="replace").splitlines()[:10] if (m := GATE_HOST_RX.match(l))), None)
    if gh in cfg.hosts and not cfg.hosts[gh].get("local"): group = gate_group(gh)
    if unit:   # a gate refuses its unit's tree mid-merge, with conflict markers, or too far behind the landing branch (item 41)
        try:       # row 220: a landing gate (land-*) judges the tree that lands, so it must hold the landing branch's head
            bd0, u0 = get_unit(cfg, unit)
            why = tree_guard(cfg, u0["cwd"], head=a.name.startswith("land-")) if own_tree(cfg, u0, bd0.units()) else None
            if why and not a.allow_unmerged: raise Err(f"refused: {unit}'s tree {u0['cwd']} {why} (--allow-unmerged to gate anyway)")
        except Err as e:
            if str(e).startswith("refused"): raise
    if unit and not cfg.run.dry:
        try:
            bd, u = get_unit(cfg, unit)
            if a.name not in u.get("gates", []): u["gates"] = [*u.get("gates", []), a.name]; bd.save(u)
        except Err as e: say(f"note: gate not recorded on {unit}: {e}")
    X(cfg, h, ensure_lanes(h, cfg))
    tok = f"gate-{re.sub(r'[^A-Za-z0-9_-]', '_', a.name)[:40]}-{time.strftime('%m%d-%H%M%S')}-{os.getpid()}"   # cancel follows the jobs it queues
    rc, o, e = X(cfg, h, f"env {pre} {pueue(h)} add -g {shlex.quote(group)} -l {shlex.quote(tag(cfg, 'gate:' + a.name))} -w {shlex.quote(str(cfg.root))} --print-task-id -o {int(a.priority)} -- {pq(f'env JANUS_WRAP={tok} {run}')}")
    if rc: raise Err("pueue add failed: " + (e or o).strip()[:100])
    if cfg.run.dry: return
    tid = o.strip(); say(f"queued gate {a.name} as {ln}:{tid} in {group}" + (f" for unit {unit}" if unit else "")
                         + (f" (expected {est:g} min)" if est is not None else "") + (f", limit {lim} min" if lim else ""))
    atomic_write(out / a.name / "task.txt", f"{ln}:{tid}\n")   # janus gate-log reads its output
    if not a.wait: return
    subprocess.run(["sh", "-c", f"{pueue(h)} wait -q {tid}"])
    lines = [l for l in cfg.p("gate_log").read_text().splitlines() if (m := GATE_LINE.match(l)) and m.group(1) == a.name]
    say(lines[-1] if lines else f"no RESULT line for {a.name}")
    return 0 if lines and lines[-1].endswith("RESULT: PASS") else 1

MERGE_BRIEF = """# Brief: {m}: merge {br} into {unit}'s branch

TIER: {tier}
ITEM: {item}

`janus land {unit}` stopped: merging the current `{br}` into the branch `{branch}` of `{wt}` conflicts in:
{files}

In `{wt}`: run `git merge {br}`, resolve every conflict keeping both sides' intent (read each side's commits), build and test
through the lanes, commit the merge. Do not land: when it is committed, the orchestrator runs `janus land {unit}` again, which
gates the merged tree and squash-lands it. Write REPORT.md with what each conflict was and how it was resolved.
"""

def git(wt, *a, timeout=120): return sh(["git", "-C", str(wt), *a], timeout=timeout)

def _dq_escape(v):
    """v made literal inside a shell double-quoted string: escape the characters double quotes still expand (\\, ", $, `)."""
    return re.sub(r'([\\"$`])', r'\\\1', str(v))

def final_gate_cmd(ld, unit, wt, branch):
    """The [landing] final_gate command with {unit}, {wt} and {branch} substituted (item 12), each shell-quoted for the quote
    context it ACTUALLY sits in (finding 7, finding 3, finding D): an agent names its own branch and the command runs on the
    final-gate host, so a hostile branch name or a path with a space must not split the command, and a `$(...)` in the value must
    never run in the quoting form it sits in. The template is scanned with a STACK of quote contexts: a `"` inside single quotes
    is literal, a `'` inside double quotes is literal, and -- the part finding D fixes -- a `$(` or a backtick inside double
    quotes opens a NEW command context (its own quoting is fresh, and it closes on its `)` or backtick), so a placeholder there is
    quoted for THAT context, not escaped for the surrounding double quotes. Consequences: `sh -c 'cd "{wt}" && ./check'` (a
    placeholder inside double quotes inside single quotes) works; `./gate "$(echo {branch})"` passes the value through
    `shlex.quote` inside the substitution (the value's `$(...)` is literal there); a placeholder alone between matching quotes
    drops them and is quoted once (shlex.quote); one inside single quotes is escaped for single quotes; one bare is shlex.quote'd.
    A `${...}` in the config is left intact (only a bare {name} is substituted). NOTE (finding D part 2): a SECOND shell level
    (`sh -c '..."{branch}"...'`, `ssh h "...'{branch}'..."`) re-parses the command in a fresh shell, which no scan of one level
    can make safe; `final_gate_cmd_checked` (what cmd_land uses) refuses a branch janus cannot quote there."""
    s = ld.get("final_gate") or ""
    vals = {"unit": str(unit), "wt": str(wt), "branch": str(branch)}
    out, i, n = [], 0, len(s)
    stack = [None]   # the quote context of each open level: None (bare command), "'", '"', ")" (inside $() or ( ), "`" (backticks)
    def ph(j):
        if j and s[j - 1] == "$": return None   # `${name}` in the config is not a placeholder
        m = re.match(r"\{(\w+)\}", s[j:])
        return (m.group(1), j + m.end()) if m and m.group(1) in vals else None
    while i < n:
        c = s[i]; q = stack[-1]
        if q is None:
            if c in "\"'":
                m = re.match(r"([\"'])\{(\w+)\}\1", s[i:])
                if m and m.group(2) in vals:   # the placeholder alone between matching quotes: drop the pair, quote once
                    out.append(shlex.quote(vals[m.group(2)])); i += m.end(); continue
                out.append(c); stack.append(c); i += 1; continue
            if c == "\\":
                out.append(c); i += 1
                if i < n: out.append(s[i]); i += 1
                continue
            if c == "$" and i + 1 < n and s[i + 1] == "(":   # a command substitution: a fresh command context, closes on )
                out.append("$("); stack.append(")"); i += 2; continue
            if c == "`": out.append(c); stack.append("`"); i += 1; continue
            if c == "(": out.append(c); stack.append(")"); i += 1; continue
            if c == ")" and len(stack) > 1: stack.pop(); out.append(c); i += 1; continue
            g = ph(i)
            if g: out.append(shlex.quote(vals[g[0]])); i = g[1]; continue
        elif q == "'":
            if c == "'": stack.pop(); out.append(c); i += 1; continue
            g = ph(i)
            if g: out.append(vals[g[0]].replace("'", "'\\''")); i = g[1]; continue
        elif q == '"':
            if c == '"': stack.pop(); out.append(c); i += 1; continue
            if c == "\\":
                out.append(c); i += 1
                if i < n: out.append(s[i]); i += 1
                continue
            if c == "$" and i + 1 < n and s[i + 1] == "(":
                out.append("$("); stack.append(")"); i += 2; continue
            if c == "`": out.append(c); stack.append("`"); i += 1; continue
            g = ph(i)
            if g: out.append(_dq_escape(vals[g[0]])); i = g[1]; continue
        else:   # a command context inside $() or ( ) (closes on ")") or backticks (closes on "`")
            if c == q: stack.pop(); out.append(c); i += 1; continue
            if c in "\"'": stack.append(c); out.append(c); i += 1; continue
            if c == "\\":
                out.append(c); i += 1
                if i < n: out.append(s[i]); i += 1
                continue
            if c == "$" and i + 1 < n and s[i + 1] == "(":
                out.append("$("); stack.append(")"); i += 2; continue
            if c == "`": out.append(c); stack.append("`"); i += 1; continue
            if c == "(": out.append(c); stack.append(")"); i += 1; continue
            g = ph(i)
            if g: out.append(shlex.quote(vals[g[0]])); i = g[1]; continue
        out.append(c); i += 1
    return "".join(out)

GATE_BRANCH_ASCII = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/+@=,%#-")

def gate_branch_refusal(branch):
    """Why the final gate cannot take this branch name, or None. A second shell level (`sh -c '..."{branch}"...'`,
    `ssh h "...'{branch}'..."`) re-parses the command in a fresh shell, so no quoting of one template can make an arbitrary value
    safe there (finding D part 2); only a name whose every character is literal in every quoting context at every level is let
    through: ASCII letters, digits and `._/+@=,%#-`, and letters, marks and digits beyond ASCII (`wk/fix#12`, `issue-12=retry`
    and a name with an umlaut are legal branch names and pass). A leading `-` would arrive as an option of the gate's command and
    a leading `#` as a comment, so neither may start the name."""
    b = str(branch)
    bad = sorted({c for c in b if not (c in GATE_BRANCH_ASCII or (ord(c) > 127 and unicodedata.category(c)[0] in "LMN"))})
    if bad: return "it holds " + " ".join(repr(c) for c in bad[:8]) + ", which a second shell level cannot be made to take literally"
    if not b or b[0] in "-#": return f"it starts with {b[:1]!r}, which the gate's command would read as an option or a comment" if b else "it is empty"
    return None

def final_gate_cmd_checked(ld, unit, wt, branch):
    """final_gate_cmd, refusing an agent-named branch the gate's shell text cannot carry (gate_branch_refusal). The branch is
    checked only when the command takes it (the template holds `{branch}`): a gate that never sees the branch has no reason to
    refuse a legal name. cmd_land builds its gate command only through here, before it merges or dispatches anything."""
    why = gate_branch_refusal(branch) if "{branch}" in (ld.get("final_gate") or "") else None
    if why:
        raise Err(f"branch {branch!r} cannot be put into the final gate's command ([landing] final_gate holds {{branch}}): {why}. Nothing was "
                  f"merged or landed. Rename it (git branch -m) and land again, or land without the gate (--no-gate)")
    return final_gate_cmd(ld, unit, wt, branch)

def gate_gen(cfg, ld, unit, wt):
    """[landing] gate_gen (row 252; unset: nothing happens): the project's command that writes the final gate's script
    (out/land-<unit>/tests.sh), run in the project root with {unit} and {worktree} substituted when that script is missing or
    older than the head commit of the tree to gate (the merge land has just made, or the unit's last commit)."""
    gg = ld.get("gate_gen")
    if not gg: return
    t = cfg.p("out", "out") / f"land-{unit}" / "tests.sh"; cmd = gg.replace("{unit}", shlex.quote(unit)).replace("{worktree}", shlex.quote(str(wt)))
    if t.is_file() and t.stat().st_mtime >= int(git(wt, "log", "-1", "--format=%ct")[1].strip() or 0): return
    if cfg.run.dry: say(f"+ {cmd}  ([landing] gate_gen: {t} is missing or older than the tree)"); return
    rc, o, e = sh(["sh", "-c", cmd], cwd=cfg.root, env={"JANUS_UNIT": unit, "JANUS_TREE": str(wt)}, timeout=900)
    if rc: raise Err(f"[landing] gate_gen failed ({rc}): {(e or o).strip()[-200:]}; not landed, the merge stays committed in {wt}")
    say(f"gate script written by [landing] gate_gen: {cmd}")

def conflict_stop(cfg, a, u, wt, branch, br, files):
    """A land whose merge conflicts stops (the tree is left as it was) and dispatches the merge unit merge-<unit>; returns 3."""
    files = files[:20] or ["(see git merge)"]
    say(f"STOP: merging {br} conflicts in {len(files)} file(s): {', '.join(files[:6])}")
    m = f"merge-{a.unit}"[:32]
    if a.no_dispatch or cfg.run.dry: say(f"{'would dispatch' if cfg.run.dry else 'dispatch with'}: janus launch {m} --kind implement --cwd {wt} (brief {cfg.p('briefs', 'briefs') / (m + '.md')})"); return 3
    bp = cfg.p("briefs", "briefs") / f"{m}.md"
    if not bp.exists(): atomic_write(bp, MERGE_BRIEF.format(m=m, br=br, unit=a.unit, branch=branch, wt=wt, tier=(u.get("tier") or "normal").upper(),
                                                          item=u.get("item") or a.unit, files="\n".join(f"- `{f}`" for f in files)))
    cmd_launch(cfg, argparse.Namespace(unit=m, kind="implement", tier=u.get("tier") or "normal", host=None, model=None, brief=str(bp), cwd=str(wt),
                                       budget=None, test_host=u.get("build"), allow_main=False, of=None, report=None, resume_of=None, item=u.get("item"),
                                       dry_run=False, share_with=a.unit))
    return 3

def gate_place(cfg, gh, gc):
    """(host, command) the final gate really runs as. A final gate that is a janus command (`janus gate ...`) is a dispatcher: it
    needs the project (janus.toml, the lanes' records) and sends its steps to the hosts itself, so it runs on the coordinator by
    this janus's own entrypoint, whatever host was named (row 64: on a remote host it was `janus: command not found`, exit 127,
    and with a launcher there a janus without a project). Any other command runs on gh as given."""
    m = re.match(r"\s*((?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)janus\s+(.*)", gc, re.S)
    if not m: return gh, gc
    return next(n for n, h in cfg.hosts.items() if h.get("local")), f"{m.group(1)}{shlex.quote(sys.executable)} {shlex.quote(str(JANUS_SELF))} {m.group(2)}"

def run_final_gate(cfg, a, ra, where):
    mark = cfg.f("runs") / f"land-{a.unit}.pid"; mark.parent.mkdir(parents=True, exist_ok=True); mark.write_text(str(os.getpid()))
    try: grc = cmd_run(cfg, ra)   # row 241: while the mark's process lives, this gate is land's own, not an orphan (landing_now)
    finally: mark.unlink(missing_ok=True)
    if grc: raise Err(f"final gate on {ra.host} failed (exit {grc}): not landed; the merge stays committed in {where}")
    say(f"final gate on {ra.host}: PASS")

def move_local(wt, br, new, base=None):
    """Move the landing branch here to the landing commit: git merge --ff-only in its checkout when it is checked out (that
    checkout's files follow), else update-ref (from base when given). The error text, or None."""
    wl = git(wt, "worktree", "list", "--porcelain")[1]
    co = next((blk.split("\n")[0][9:] for blk in wl.split("\n\n") if f"branch refs/heads/{br}" in blk), None)
    if co:
        rc, o, e = git(co, "merge", "--ff-only", new, timeout=300)
        return f"{co} (where {br} is checked out) did not take the landing commit {new[:9]} fast-forward: {(e or o).strip()[-160:]}" if rc else None
    rc, o, e = git(wt, "update-ref", f"refs/heads/{br}", new, *([base] if base else []))
    return f"update-ref {br} failed: {e.strip()[:120]}" if rc else None

def land_remote(cfg, a, u, wt, branch, br, ld, hn, why):
    """The land's git work on another host (skill-fix55: the coordinator is no longer the only place a land can merge, gate and
    push). In the host's landing clone (janus/landhost.py): the unit's commits arrive by a push over ssh from its worktree here;
    the host fetches the landing branch from the remote itself (without a key there it is sent from here as last fetched, and
    land says so); merge-tree, the merge, the squash commit and the push to the remote run there. The final gate: a plain
    command runs in the host's own landing tree through its gates lane (nothing is synced); a janus command is the
    coordinator's dispatcher (gate_place) and gates the unit's worktree here, which first takes the host's merge fast-forward.
    --dry-run runs the read-only half there (the delivery to refs/janus/land/<unit>/ of the clone, the fetch, merge-tree,
    git push --dry-run) and prints the rest; no branch, tree or remote changes."""
    h = cfg.hosts[hn]; unit = a.unit; dry = cfg.run.dry; rem = cfg.c.get("git", {}).get("remote") or "origin"
    url = origin_url(cfg, wt)
    if not url: raise Err(f"{wt} has no remote {rem} and [landing] origin is not set: a land on {hn} needs the project's remote")
    clone, tree = land_clone(cfg, h), land_tree(h, unit); ident = identity(wt); G, T = HostGit(h, clone, ident), HostGit(h, tree, ident)
    rc, o, e = hrun(h, lane_script(cfg, h, url), timeout=60)
    if rc: raise Err(f"{hn} has no landing lane ({(e or o).strip()[-120:]}): janus install-host {hn}")
    ref, su, rb = f"refs/janus/land/{unit}", ssh_url(h, clone), f"refs/remotes/origin/{br}"
    keyed = G("fetch", "-q", "origin", f"+refs/heads/{br}:{rb}", timeout=900)[0] == 0   # first: the unit's push below then sends only what the remote lacks
    rc, o, e = git(wt, "push", "-q", "--force", su, f"HEAD:{ref}/head", timeout=900)
    if rc: raise Err(f"sending {branch} to {hn}:{clone} failed: {(e or o).strip()[-160:]}")
    if not keyed:
        git(wt, "fetch", "-q", rem, timeout=120); lrb = f"refs/remotes/{rem}/{br}"
        rc, o, e = git(wt, "push", "-q", "--force", su, f"{lrb if git(wt, 'rev-parse', '-q', '--verify', lrb)[0] == 0 else 'refs/heads/' + br}:{rb}", timeout=900)
        if rc: raise Err(f"sending {br} to {hn}:{clone} failed: {(e or o).strip()[-160:]}")
        say(f"note: {hn} cannot fetch {url} itself (no key the remote accepts there: an owner item); {br} was sent from here as last fetched, and the last push will be made from here")
    base, head = G("rev-parse", rb)[1].strip(), G("rev-parse", f"{ref}/head")[1].strip()
    if not base or not head: raise Err(f"{hn}:{clone} holds no {br} or no {ref}/head after the delivery")
    ahead = int(G("rev-list", "--count", f"{base}..{head}")[1].strip() or 0); behind = int(G("rev-list", "--count", f"{head}..{base}")[1].strip() or 0)
    if not ahead: raise Err(f"{branch} has no commits over {br} ({base[:9]}): nothing to land")
    say(f"land {unit} on {hn} ({why}): {branch} {head[:9]} in {clone}, {ahead} commit(s) ahead of {br} ({base[:9]}, {'fetched there' if keyed else 'sent from here'}), {behind} behind")
    rc, o, e = G("merge-tree", "--write-tree", "--name-only", head, base)
    if rc not in (0, 1): raise Err(f"git merge-tree on {hn} failed (the landing lane needs git 2.38 or newer): {(e or o).strip()[:120]}")
    if rc == 1:
        if not dry: git(wt, "fetch", "-q", rem, timeout=120)   # the merge unit works here: it merges the remote's tip
        return conflict_stop(cfg, a, u, wt, branch, f"{rem}/{br}", [l for l in o.split("\n\n")[0].splitlines()[1:] if l.strip()])
    gc = None if a.no_gate else final_gate_cmd_checked(ld, unit, wt, branch)
    if gc is not None and not gc: raise Err("set [landing] final_gate (the command that gates a merged tree) in janus.toml, or --no-gate")
    gh, gcmd = gate_place(cfg, hn, gc) if gc else (hn, None)
    on_host = gc is not None and gh == hn   # a plain command: in the landing tree there
    if on_host: gcmd = final_gate_cmd_checked(ld, unit, tree, branch)
    if dry:
        prc, po, pe = G("push", "--dry-run", "origin", f"{head}:refs/heads/janus-land-probe", timeout=120) if keyed else (1, "", "it cannot fetch either")
        say(f"+ [{hn}] git merge --no-edit {base[:9]} in {tree}  (merge-tree there: clean)")
        say("+ gate skipped (--no-gate)" if gc is None else f"+ janus run --host {gh} --lane gates --unit {unit} " + (f"(in {tree} there, nothing synced)" if on_host else f"--wt {wt} (the dispatcher; {wt} first takes the merge fast-forward)") + f" -- {gcmd}")
        say(f"+ [{hn}] git commit-tree <merged tree> -p {base[:9]}; git push origin <commit>:refs/heads/{br}  (push from {hn}: "
            + ("ok, git push --dry-run there passed)" if prc == 0 else f"NOT possible ({(pe or po).strip()[-100:]}): the push would be made from here; an owner item)"))
        return 0
    rc, o, e = hrun(h, f"{G.env}if [ -e {qp(tree)}/.git ]; then git -C {qp(tree)} checkout -q --detach {head} && git -C {qp(tree)} reset -q --hard {head}; "
                       f"else git -C {qp(clone)} worktree prune; git -C {qp(clone)} worktree add -q --detach {qp(tree)} {head}; fi", timeout=900)
    if rc: raise Err(f"the landing tree {hn}:{tree} could not be set to {head[:9]}: {(e or o).strip()[-160:]}")
    if behind:
        rc, o, e = T("merge", "--no-edit", base, timeout=600)
        if rc: T("merge", "--abort"); raise Err(f"git merge {br} on {hn} failed although merge-tree was clean: {(e or o).strip()[-160:]}; aborted")
        say(f"merged {br} into {branch} on {hn}")
    merged = T("rev-parse", "HEAD")[1].strip(); G("update-ref", f"{ref}/merged", merged)
    if merged != head:   # the unit's worktree here follows: what it holds is what is gated and landed
        rc, o, e = git(wt, "fetch", "-q", su, f"{ref}/merged", timeout=900)
        if not rc: rc, o, e = git(wt, "merge", "--ff-only", "FETCH_HEAD", timeout=300)
        if rc: raise Err(f"{wt} did not take the merge made on {hn} fast-forward: {(e or o).strip()[-160:]}")
    if gc is None: say("gate skipped (--no-gate)")
    else:
        gate_gen(cfg, ld, unit, wt)
        run_final_gate(cfg, a, argparse.Namespace(host=gh, lane="gates", unit=unit, wt=str(wt), subdir=None, usb=False, priority=10, no_wait=False,
                                                  allow_unmerged=False, command=[gcmd], host_tree=tree if on_host else None), f"{hn}:{tree}")
    if keyed: G("fetch", "-q", "origin", f"+refs/heads/{br}:{rb}", timeout=300)
    if G("rev-parse", rb)[1].strip() != base: raise Err(f"{br} moved while {unit} was gated: run janus land {unit} again (it merges the new tip)")
    subj = T("log", "--format=%s", f"{base}..HEAD", "--no-merges")[1].splitlines()
    msg = f"{unit}: land {branch} ({len(subj)} commit(s), squashed)\n\n" + "\n".join(f"- {s}" for s in subj[:40]) + "\n"
    rc, new, e = T("commit-tree", merged + "^{tree}", "-p", base, "-F", "-", inp=msg); new = new.strip()
    if rc or not new: raise Err(f"git commit-tree on {hn} failed: {e.strip()[:120]}")
    G("update-ref", f"{ref}/landed", new); pushed = False
    if keyed:
        rc, o, e = G("push", "-q", "origin", f"{new}:refs/heads/{br}", timeout=300); pushed = rc == 0
        if not pushed: say(f"note: git push from {hn} was refused ({(e or o).strip()[-120:]}): {br} moves here instead, push it from here")
    rc, o, e = git(wt, "fetch", "-q", su, f"{ref}/landed", timeout=900)   # the landing branch here follows
    err = f"fetching the landing commit from {hn} failed: {(e or o).strip()[-120:]}" if rc else None
    if not err and (pushed or git(wt, "rev-parse", br)[1].strip() != base) and git(wt, "merge-base", "--is-ancestor", br, new)[0]: err = f"{br} here is not an ancestor of the landing commit"
    err = err or move_local(wt, br, new)
    if pushed: git(wt, "fetch", "-q", rem, timeout=120)
    if err and not pushed: raise Err(f"{unit} is merged, gated and committed on {hn} as {new[:9]} ({ref}/landed in {clone}) but is not on {br} anywhere yet: {err}")
    say(f"landed {unit} on {hn}: {br} {base[:9]} -> {new[:9]} ({len(subj)} commit(s) squashed); " + (f"pushed to {url} from {hn}" if pushed else "not pushed: push from here")
        + (f"; note: {br} here did not follow ({err}): git fetch, then fast-forward it" if err else "") + f"; close is separate (janus close {unit})")
    if u.get("id"): Beads(cfg).comment(u["id"], f"{ts()} landed on {br} as {new[:9]} (on {hn}{', pushed from there' if pushed else ''})")
    note_event(cfg, "landing")
    return 0

def cmd_land(cfg, a):
    """janus land <unit>: in the unit's worktree, merge the current landing branch ([landing] branch, default main); on a conflict
    stop (the tree is left as it was) and dispatch a merge unit merge-<unit> (--no-dispatch: only say so); gate the merged tree
    on [landing] final_gate_host with [landing] final_gate (a command run through janus run, gates lane; [landing] gate_gen, when
    set, first writes the gate's script if it is missing or older than the merged tree); then squash-land: one
    commit whose tree is the gated tree and whose parent is the landing branch tip that was merged. When the landing branch is
    checked out (the main checkout), it is moved there with git merge --ff-only, so that checkout's files follow; a landing
    branch that moved meanwhile, or a dirty main checkout, stops it. --dry-run checks the merge with git merge-tree and prints
    each step, changing nothing."""
    ld = cfg.c.get("landing", {}); br = ld.get("branch") or "main"
    u = next((x for x in Beads(cfg).units() if x["name"] == a.unit), None) or all_units(cfg).get(a.unit)
    if not u: raise Err(f"no unit {a.unit}")
    wt = Path(u.get("cwd", "/"))
    if not own_tree(cfg, u, [x for x in Beads(cfg).units()]) or not wt.is_dir(): raise Err(f"{a.unit} has no worktree of its own ({wt}): nothing to land")
    rc, branch, e = git(wt, "symbolic-ref", "--short", "HEAD"); branch = branch.strip()
    if rc: raise Err(f"{wt} is not on a branch")
    if branch == br: raise Err(f"{wt} is on {br} itself")
    # the gate's command is built (and the branch name judged) before anything is merged or dispatched, and only when a gate
    # will run: a refusal after the merge left a merge commit behind for a land that could never go on (review finding F2)
    gh, gc = ld.get("final_gate_host"), None if a.no_gate else final_gate_cmd_checked(ld, a.unit, wt, branch)
    up = unapplied_patch(cfg, u)   # row 239
    if up: raise Err(f"{a.unit}'s collected patch {up} is not applied in {wt} (the unit ran on a host without the repository): "
                     f"git -C {wt} apply --3way {up}, commit, then janus land {a.unit} again; do not close it before (its work exists only in that patch)")
    if git(wt, "status", "--porcelain", "--untracked-files=no")[1].strip(): raise Err(f"{wt} has uncommitted changes: commit them first")
    if git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0: raise Err(f"{wt} has a merge in progress (MERGE_HEAD): finish or abort it")
    hn, hwhy = land_host(cfg, getattr(a, "host", None))   # skill-fix55: any host with a landing lane lands; the coordinator is one of them
    if not cfg.hosts[hn].get("local"):
        qr = land_review(cfg, u)
        if qr: raise Err(qr)
        return land_remote(cfg, a, u, wt, branch, br, ld, hn, hwhy)
    base = git(wt, "rev-parse", br)[1].strip()
    if not base: raise Err(f"no branch {br}")
    # Row 240: the landing branch here may be behind its upstream (a commit already pushed that this checkout lacks). "Ahead of
    # <branch>" then counted that pushed commit as the unit's, and the squash rewrote it on the old base. Fetch, compare, refuse.
    rcu, ups, _ = git(wt, "rev-parse", "--abbrev-ref", f"{br}@{{upstream}}"); ups = ups.strip()
    if rcu == 0 and "/" in ups:
        if not cfg.run.dry and git(wt, "fetch", "-q", ups.split("/", 1)[0], timeout=120)[0]: say(f"note: git fetch {ups.split('/', 1)[0]} failed; comparing with {ups} as last fetched")
        lag = int(git(wt, "rev-list", "--count", f"{br}..{ups}")[1].strip() or 0)
        if lag: raise Err(f"{br} here is {lag} commit(s) behind {ups} (already pushed): landing on it would rewrite them. Fast-forward {br} first "
                          f"(in its checkout: git merge --ff-only {ups}), then janus land {a.unit} again")
    ahead = int(git(wt, "rev-list", "--count", f"{br}..HEAD")[1].strip() or 0); behind = int(git(wt, "rev-list", "--count", f"HEAD..{br}")[1].strip() or 0)
    if not ahead: raise Err(f"{branch} has no commits over {br}: nothing to land")
    if landed(cfg, u): raise Err(f"{a.unit} is already landed: {br} holds its tree ({branch} has no change since): janus close {a.unit}")
    qr = land_review(cfg, u)   # [quality] review_model: its review must pass or be overridden before it lands (item 2)
    if qr: raise Err(qr)
    say(f"land {a.unit}: {branch} in {wt}, {ahead} commit(s) ahead of {br} ({base[:9]}), {behind} behind")
    # 1. merge the current landing branch (dry run: merge-tree only)
    rc, o, e = git(wt, "merge-tree", "--write-tree", "--name-only", "HEAD", br)
    if rc not in (0, 1) and not cfg.run.dry:   # git before 2.38 has no merge-tree --write-tree: a trial merge, always aborted (tree clean)
        rc2, o2, e2 = git(wt, "merge", "--no-commit", "--no-ff", br, timeout=600)
        cf_ = git(wt, "diff", "--name-only", "--diff-filter=U")[1]; git(wt, "merge", "--abort")
        if rc2 and not cf_.strip(): raise Err(f"git merge-tree and a trial merge failed: {(e or o).strip()[:80]}; {(e2 or o2).strip()[:80]}")
        rc, o = (1, "-\n" + cf_) if cf_.strip() else (0, "")
    elif rc not in (0, 1): raise Err(f"git merge-tree failed: {(e or o).strip()[:120]}")
    if rc == 1:
        return conflict_stop(cfg, a, u, wt, branch, br, [l for l in o.split("\n\n")[0].splitlines()[1:] if l.strip()])   # tree id, then the conflicted paths
    if behind:
        if cfg.run.dry: say(f"+ git -C {wt} merge --no-edit {br}  (merge-tree: clean)")
        else:
            rc, o, e = git(wt, "merge", "--no-edit", br, timeout=600)
            if rc: git(wt, "merge", "--abort"); raise Err(f"git merge {br} failed although merge-tree was clean: {(e or o).strip()[-160:]}; aborted")
            say(f"merged {br} into {branch}")
    # 2. gate the merged tree on the final-gate host
    if a.no_gate: say("gate skipped (--no-gate)")
    elif not gh or not gc: raise Err("set [landing] final_gate_host and final_gate (the command that gates a merged tree) in janus.toml, or --no-gate")
    else:
        gate_gen(cfg, ld, a.unit, wt)
        gh2, gc2 = gate_place(cfg, gh, gc)   # row 64: a janus command gates from the coordinator by this janus's entrypoint
        ra = argparse.Namespace(host=gh2, lane="gates", unit=a.unit, wt=str(wt), subdir=None, usb=False, priority=10, no_wait=False, allow_unmerged=False, command=[gc2])
        if cfg.run.dry: say(f"+ janus run --host {gh2} --lane gates --unit {a.unit} --wt {wt} -- {gc}")
        else: run_final_gate(cfg, a, ra, wt)
    # 3. squash-land: tree of the gated HEAD, parent = the landing tip that was merged
    if cfg.run.dry: say(f"+ git commit-tree HEAD^{{tree}} -p {base[:9]}; move {br} (ff-only in its checkout); {ahead} commit(s) squashed"); return 0
    if git(wt, "rev-parse", br)[1].strip() != base: raise Err(f"{br} moved while {a.unit} was gated: run janus land {a.unit} again (it merges the new tip)")
    tree = git(wt, "rev-parse", "HEAD^{tree}")[1].strip()
    subj = git(wt, "log", "--format=%s", f"{base}..HEAD", "--no-merges")[1].splitlines()
    msg = f"{a.unit}: land {branch} ({len(subj)} commit(s), squashed)\n\n" + "\n".join(f"- {s}" for s in subj[:40]) + "\n"
    rc, new, e = sh(["git", "-C", str(wt), "commit-tree", tree, "-p", base, "-F", "-"], inp=msg)
    if rc: raise Err(f"git commit-tree failed: {e.strip()[:120]}")
    new = new.strip(); err = move_local(wt, br, new, base)
    if err: raise Err(err)
    say(f"landed {a.unit}: {br} {base[:9]} -> {new[:9]} ({len(subj)} commit(s) squashed); push and close are separate (janus close {a.unit})")
    if u.get("id"): Beads(cfg).comment(u["id"], f"{ts()} landed on {br} as {new[:9]}")
    note_event(cfg, "landing")
    return 0

SECRETS = [re.compile(x.encode()) for x in ("sk" + "-ant-[A-Za-z0-9_-]{20,}", "sk" + "-or-[A-Za-z0-9_-]{20,}", "sk" + "-proj-[A-Za-z0-9_-]{20,}",
    "\\bsk" + "-[a-f0-9]{32,}\\b", "AK" + "IA[0-9A-Z]{16}", "gh" + "p_[A-Za-z0-9_-]{20,}", "xo" + "x[A-Za-z0-9_-]{10,}",
    # a real PEM block only: the dashed BEGIN line, then a base64 line (a mention of the words in prose is not a key)
    "-----" + "BEGIN( [A-Z0-9]+)* PRIV" + "ATE KEY-----\\r?\\n[A-Za-z0-9+/=]{16,}\\r?$")]

SECRETS[-1] = re.compile(SECRETS[-1].pattern, re.M)

def cmd_commit_state(cfg, a):
    """Stage everything, scan exactly what is staged (L9), commit and push; unstage again if a secret is found."""
    if cfg.run.dry: say("+ janus index; git add -A; scan staged files; git commit; git push"); return
    write_index(cfg)   # out/INDEX.md is committed with the state (janus-swarm item 3)
    rc, o, e = sh(["git", "add", "-A"], cwd=cfg.root, timeout=120)
    for _ in range(3):   # item 7: a file vanished between listing and add (a monitor deleted a temp file); retry, then carry on
        if not rc: break
        msg = (e or o).strip()
        if not re.search(r"unable to stat|No such file|did not match|does not exist", msg):
            raise Err(f"git add failed: {msg[-200:]}")   # finding 11: a file that fails to add every time is an error, not a note
        rc, o, e = sh(["git", "add", "-A", "--ignore-errors"], cwd=cfg.root, timeout=120)
    if rc:   # finding 10: a SECOND vanished file is not an error; only a real add failure is
        msg = (e or o).strip()
        if not re.search(r"unable to stat|No such file|did not match|does not exist", msg): raise Err(f"git add failed: {msg[-200:]}")
    rc, o, e = sh(["git", "diff", "--cached", "--name-only", "-z"], cwd=cfg.root)
    paths = [x for x in o.split("\0") if x]
    hit = []
    for x in paths:
        try: b = (cfg.root / x).read_bytes()
        except OSError: continue
        if any(r.search(b) for r in SECRETS): hit.append(x)
    if hit: sh(["git", "reset", "-q"], cwd=cfg.root); raise Err("refused (unstaged again), possible secrets in: " + ", ".join(hit))
    if not paths: say("nothing to commit"); return
    for cmd in (["git", "commit", "-m", a.message], push_cmd(cfg.root, cfg)):
        rc, o, e = sh(cmd, cwd=cfg.root, timeout=120)
        if rc: raise Err(f"{shlex.join(cmd[:2])} failed: {(e or o).strip()[-200:]}")
    say(f"committed {len(paths)} path(s) and pushed")

def push_cmd(repo, cfg=None):
    """git push; on a branch without an upstream, push --set-upstream to [git] remote (default origin, else the only remote),
    so the next check can count unpushed commits (janus-fixes7)."""
    if sh(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "@{u}"])[0] == 0: return ["git", "push"]
    rem = ((cfg.c.get("git", {}) if cfg else {}).get("remote")) or ""
    if not rem:
        rs = sh(["git", "-C", str(repo), "remote"])[1].split()
        rem = "origin" if "origin" in rs or not rs else rs[0]
    return ["git", "push", "--set-upstream", rem, "HEAD"]
