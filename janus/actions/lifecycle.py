"""A unit's life after launch: janus stop, collect, close, park, resume, board-close; saving its output, task log and
transcript; the watcher's tend_parked (resume a parked unit when its jobs finished)."""
import argparse, ast, contextlib, os, re, shlex, sys, time
from pathlib import Path
from janus.actions.jobs import (agent_res, collect_job, job_of, job_state, patch_scope, patch_scope_desc, patch_scope_sh, recorded_jid, runs_dir, save_task_log,
    task_rc, task_status, unit_jobs)
from janus.actions.launch import (agent_scope, api_remote, close_tab, confirm_started, cwd_taken, per_turn, put_script, remote_env, start_tmux, start_turn, tmux_submit, tmux_target, tool_argv,
    turn_command, turn_running)
from janus.beads import Beads
from janus.board import board, board_dir, board_script
from janus.branch import published_contains
from janus.budgets import budget_note, finish_refusal, reload_usd, session_usage
from janus.context import (DIFF_X, Err, JANUS_MARK, LockLost, JANUS_SELF, SSH, X, atomic_write, herdr, hrun, now, pq, ps_table, qp, say,
    sh, ts)
from janus.memory import memory_text
from janus.ledger import keep_run_json, ledger_done, ledger_read, reserve_budget, route_ok, run_cost, settle_run
from janus.project import project_id, tag, unit_tmux
from janus.rigor import level_of
from janus.probe import host_liveness, probe, probe_all, pueue
from janus.templates import FINGERPRINT, UNIT_PROCS
from janus.transcripts import card_fields, read_transcript, session_of
from janus.units import (ENDED, agent_live, all_units, busy_dead, busy_mark, no_remote_copy, get_unit, herdr_agents, force_close_refusal, own_tree, reopened, report_path, unit_runs, unit_spent, unit_state,
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

def stop_orphan(cfg, ref):
    """janus stop --orphan HOST:SESSION: stop one tmux session of this project that no open unit records (janus stop <unit> said
    'no open unit' and the hook refuses tmux kill by hand: orphans held reservations and blocked fill for days)."""
    from janus.actions.tidy import orphan_session
    hn, _, s = ref.partition(":")
    if hn not in cfg.hosts or not s: raise Err("expected --orphan <host>:<tmux session>")
    h = cfg.hosts[hn]; units = Beads(cfg).units(); why = orphan_session(cfg, s, units, all_units(cfg))
    if not why: raise Err(f"refused: {s} is not this project's orphan session (an open unit records it, or it carries no project id): janus stop <unit> stops a unit")
    if cfg.run.dry: say(f"+ tmux kill-session -t ={s} on {hn} ({why})"); return
    rc, o, e = hrun(h, f'PATH="$HOME/.local/bin:{h.get("bin", "")}:$PATH"; tmux kill-session -t {tmux_target(s, False)}', timeout=60)
    if rc: raise Err(f"{hn}: tmux kill-session {s} failed: {(e or o).strip()[:120]}")
    say(f"stopped orphan tmux session {s} on {hn} ({why})")

def cmd_stop(cfg, a):
    if getattr(a, "orphan", False): return stop_orphan(cfg, a.unit)
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]; via = u.get("via"); p = pueue(h)
    why = None if a.force or cfg.run.dry else uncollected(cfg, u)
    if why: raise Err(f"refused: {u['name']}: {why}")
    say(f"stop {u['name']} ({via} on {u['host']})")
    roots = task_pid_roots(h, u) if not cfg.run.dry else ""
    if cfg.run.dry: say(f"+ pueue kill / tmux kill-session; TERM then KILL every process group of {u['name']} (roots {roots or '-'}, env JANUS_UNIT)"); return
    if via == "pueue": hrun(h, f"{p} kill {int(u['task'])}")
    elif via == "tmux": hrun(h, f'PATH="$HOME/.local/bin:{h.get("bin", "")}:$PATH"; tmux kill-session -t {tmux_target(unit_tmux(u), False)}')   # that name only, on the PATH the probe uses
    # its lane jobs (label unit:<name>) belong to it too
    hrun(h, f"{p} status --json | python3 -c \"import json,sys; [print(i) for i, t in json.load(sys.stdin)['tasks'].items() if t.get('label') == '{tag(cfg, 'unit:' + u['name'])}' and ('Running' in str(t['status']) or 'Queued' in str(t['status']))]\" | xargs -r {p} kill 2>/dev/null; true")
    left = kill_unit(h, u["name"], roots)
    if left: raise Err(f"{u['name']}: {left} process(es) still alive after TERM and KILL")
    for _ in range(10):
        with cfg.unlocked(): ps = probe(u["host"], h)
        st = unit_state(u, {u["host"]: ps}, herdr_agents(cfg)) if ps["ok"] else None
        if st and (st == "unknown" or not agent_live(u, st)): break   # an idle tmux agent's session still stands: not stopped
        time.sleep(1)
    else: raise Err(f"{u['name']} is still alive after stop")
    if close_tab(cfg, u): bd.save(u)   # every kind: a stopped herdr agent leaves only an empty shell in its tab (issue 267)
    if h.get("local"): settle_run(cfg, u, "stopped")   # remote: collect brings back the cost first, close settles the rest
    bd.comment(u["id"], f"{ts()} stopped (every process group of the unit ended)"); say("stopped: no process of the unit is left")

# A remote unit's changes as a patch (janus collect, the backup patch). The patch is written by git itself, never by text
# tools: `diff -ruN | sed` (item 29) could not say "new file", and a difflib builder lost or corrupted a file without a final
# newline, non-UTF-8 text, a form feed, binaries, modes, symlinks and quoted names (collect-fix2). The host's python3 only
# finds WHICH paths changed, byte for byte (content, executable bit, symlink target; the same excluded names as DIFF_X); both
# sides of those paths go into a scratch git object store as they are (no filters, no ignore rules, no user config), git
# writes the patch (`diff-tree -p --binary --full-index`, so `git apply --3way` has its blob ids), and the patch is proved
# before it is printed: applied by git to the base tree it must give the unit's tree id. Anything that keeps the patch from
# being complete (an unreadable path, no git, a patch that does not rebuild the tree) is an error on stderr and a non-zero
# exit; a fifo, socket or device, which git cannot carry, is named on stderr and left out. Tool caches (SKIP) and new files the
# tree's own .gitignore ignores are no part of the unit's changes (row 251); a file that was sent is carried whatever the rules say.
PATCH_PY = r"""import filecmp, os, shutil, signal, stat, subprocess, sys, tempfile
unit, base, wt = sys.argv[1], sys.argv[2], sys.argv[3]
sub = sys.argv[4] if len(sys.argv) > 4 else None   # janus collect --subdir: one subtree of the copy (scoped recovery)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))   # the deadline's TERM: the scratch store is removed (finally) and git killed
# every git it runs keeps the host's patch lock (fd 9, close_fds=False): the lock is free only when no process of the helper is left
SKIP = {"target", ".venv", "node_modules", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis", ".tox"}   # gates.CACHE_DIRS too
bad, notes = [], []
def die(msg):
    sys.stderr.write("janus patch (%s): %s\n" % (unit, msg.rstrip())); sys.exit(1)
def entries(root):
    E = {}
    def err(e): bad.append("%s: %s" % (e.filename, e.strerror))
    top = os.path.join(root, sub) if sub else root   # scoped: only the selected subtree is walked; paths stay relative to root
    if sub and not os.path.isdir(top): return E   # absent on this side: all of it added or deleted
    for d, dirs, files in os.walk(top, onerror=err):
        links = [x for x in dirs if os.path.islink(os.path.join(d, x))]
        dirs[:] = [x for x in dirs if x not in SKIP and x not in links]
        for f in files + [x for x in links if x not in SKIP]:
            p = os.path.join(d, f)
            try: st = os.lstat(p)
            except OSError as e: bad.append("%s: %s" % (p, e.strerror)); continue
            if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode): E[os.path.relpath(p, root)] = st
            else: notes.append("%s: a fifo, socket or device, which no patch can carry: left out" % p)
    return E
if not os.path.isdir(wt):
    if sub: die("%s is missing: no copy to recover %s from" % (wt, sub))
    sys.exit(0)
if not os.path.isdir(base): die("%s is missing: the tree the unit was sent, without which its changes cannot be told" % base)
if sub is not None:   # checked again here, on the host, whatever the caller checked: never outside the copy, never through a symlink
    parts = [x for x in sub.split("/") if x not in ("", ".")]
    if sub.startswith("/") or not parts or ".." in parts or SKIP & set(parts):
        die("--subdir %r: not a relative subdirectory of the copy (absolute, the copy itself, a .. component or an excluded folder)" % sub)
    sub, have = "/".join(parts), 0
    for root in (base, wt):
        p = root
        for x in parts:
            p = os.path.join(p, x)
            if not os.path.lexists(p): break
            if os.path.islink(p): die("--subdir %s: %s is a symlink, which a scoped collect never follows" % (sub, p))
            if not os.path.isdir(p): die("--subdir %s: %s is not a directory" % (sub, p))
        else: have += 1
    if not have: die("--subdir %s exists in neither %s nor %s: nothing to recover" % (sub, base, wt))
A, B = entries(base), entries(wt)
def same(r):
    a, b, pa, pb = A[r], B[r], os.path.join(base, r), os.path.join(wt, r)
    if stat.S_IFMT(a.st_mode) != stat.S_IFMT(b.st_mode): return False
    if stat.S_ISLNK(a.st_mode): return os.readlink(pa) == os.readlink(pb)
    if (a.st_mode ^ b.st_mode) & 0o100 or a.st_size != b.st_size: return False
    try: return filecmp.cmp(pa, pb, shallow=False)
    except OSError as e: bad.append("%s: %s" % (e.filename or pb, e.strerror)); return True
ch = sorted(r for r in set(A) | set(B) if r not in A or r not in B or not same(r))
for r in ch:
    p = os.path.join(wt if r in B else base, r)
    if stat.S_ISREG(os.lstat(p).st_mode) and not os.access(p, os.R_OK): bad.append("%s: cannot be read" % p)
if bad: die("no complete patch: " + "; ".join(bad[:20]) + (" (and %d more)" % (len(bad) - 20) if len(bad) > 20 else ""))
for x in notes: sys.stderr.write("janus patch (%s): note: %s\n" % (unit, x))
if not ch: sys.exit(0)
tmp = tempfile.mkdtemp(prefix="janus-patch-")
try:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_DIR=tmp + "/g", HOME=tmp, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_ATTR_NOSYSTEM="1", LC_ALL="C")
    G = ["git", "-c", "core.bare=false", "-c", "core.fileMode=true", "-c", "core.symlinks=true", "-c", "core.autocrlf=false",
         "-c", "core.safecrlf=false", "-c", "core.quotePath=true", "-c", "core.precomposeUnicode=false", "-c", "core.ignoreCase=false",
         "-c", "core.protectHFS=false", "-c", "core.protectNTFS=false"]
    def git(args, cwd=tmp, inp=None, out=subprocess.PIPE, **e):
        try: p = subprocess.run(G + args, cwd=cwd, input=inp, stdout=out, stderr=subprocess.PIPE, env=dict(env, **e), close_fds=False)
        except OSError as x: die("git cannot be run on this host (%s): no patch without it" % x)
        if p.returncode: die("no complete patch: git %s failed: %s" % (args[0], p.stderr.decode(errors="replace").strip()[-800:]))
        return p.stdout.decode().strip() if out == subprocess.PIPE else ""
    git(["init", "-q"])
    with open(tmp + "/g/info/attributes", "w") as f: f.write("* -text -filter -ident -working-tree-encoding !eol !diff\n")
    new = [r for r in ch if r not in A]   # row 251: a NEW file the tree's own .gitignore rules ignore (build output, a log) is not the unit's work
    if new:
        p = subprocess.run(G + ["check-ignore", "--no-index", "-z", "--stdin"], cwd=wt, input=b"".join(os.fsencode(r) + b"\0" for r in new),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(env, GIT_WORK_TREE=os.path.abspath(wt)), close_fds=False)
        if p.returncode not in (0, 1): die("no complete patch: git check-ignore failed: %s" % p.stderr.decode(errors="replace").strip()[-800:])
        ign = set(os.fsdecode(x) for x in p.stdout.split(b"\0") if x)
        if ign: sys.stderr.write("janus patch (%s): note: %d new path(s) ignored by .gitignore left out: %s\n" % (unit, len(ign), ", ".join(sorted(ign)[:8])))
        ch = [r for r in ch if r not in ign]
        if not ch: sys.exit(0)
    def tree(root, E, name):
        e = dict(GIT_INDEX_FILE=tmp + "/" + name, GIT_WORK_TREE=os.path.abspath(root))
        git(["update-index", "--add", "-z", "--stdin"], cwd=root, inp=b"".join(os.fsencode(r) + b"\0" for r in ch if r in E), **e)
        return git(["write-tree"], cwd=root, **e)
    ta, tb = tree(base, A, "ia"), tree(wt, B, "ib")
    with open(tmp + "/patch", "wb") as f:
        git(["diff-tree", "-r", "-p", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color", ta, tb], out=f)
    os.mkdir(tmp + "/w"); e = dict(GIT_INDEX_FILE=tmp + "/iv", GIT_WORK_TREE=tmp + "/w")
    git(["read-tree", ta], cwd=tmp + "/w", **e); git(["apply", "--cached", "--whitespace=nowarn", tmp + "/patch"], cwd=tmp + "/w", **e)
    got = git(["write-tree"], cwd=tmp + "/w", **e)
    if got != tb: die("no complete patch: the patch git wrote does not rebuild the unit's tree from its base (%s, wanted %s)" % (got, tb))
    with open(tmp + "/patch", "rb") as f: shutil.copyfileobj(f, sys.stdout.buffer)
    sys.stdout.buffer.flush()
finally:
    shutil.rmtree(tmp, ignore_errors=True)
"""

# The helper's containment on a shared host. One patch helper per host and user at a time: a lock taken without waiting (busy:
# exit PATCH_BUSY, nothing queued). Its fd is inherited by the helper and every git it runs, so the lock is held until the last
# of them ends, whatever happens to the ssh session that started it. A remote helper runs in its own systemd scope
# (patch_scope: the project's name and Description) capped at PATCH_MEM_MB in all with no swap; it checks inside the scope that
# the cgroup limits are in force and that the lock reached it, else no patch (PATCH_UNSAFE: fail closed). A scope of that name
# left over is stopped first only when it is this project's. GNU timeout puts the helper in its own process group: TERM to the
# whole group at PATCH_DEADLINE s, KILL PATCH_KILL s later, well inside the caller's PATCH_CALLER s; then the scope is stopped
# too (a process that left the group). The patch goes to a temporary file of its own and is renamed over <fname> only when the
# helper succeeded: any failure keeps the earlier complete patch. A local host (no systemd) gets the lock and the deadline.
PATCH_MEM_MB, PATCH_DEADLINE, PATCH_KILL, PATCH_CALLER, PATCH_BUSY, PATCH_UNSAFE = 512, 240, 5, 300, 75, 70
PATCH_LOCK_DIR = '"${XDG_RUNTIME_DIR:-/tmp}"'   # shell text: the host's per-user folder
CGROUP_ROOT = "/sys/fs/cgroup"
PATCH_GUARD = ('CG=$(systemctl --user show -p ControlGroup --value "$1" 2>/dev/null) && [ -n "$CG" ] && [ "$(cat "$2$CG/memory.max" 2>/dev/null)" = "$3" ] '
               '&& [ "$(cat "$2$CG/memory.swap.max" 2>/dev/null)" = 0 ] || { echo "$4 the memory limits of its scope $1 are not in force (cgroup v2 with '
               'the memory controller delegated to the user is needed): no patch" >&2; exit %(unsafe)d; }\n'
               'flock -n 9 || { echo "$4 the host patch lock did not reach the helper: no patch" >&2; exit %(unsafe)d; }\nshift 4; exec "$@"')

def patch_script(h, n, fname, sub=None):
    """The shell remote_patch runs on host h (see above). sub: only that subtree of the copy (janus collect --subdir)."""
    q = shlex.quote; f = q(f"out/{n}/{fname}"); remote = not h.get("local"); sc = patch_scope(h); U = PATCH_UNSAFE
    run = f'"$TO" -k {PATCH_KILL} -s TERM {PATCH_DEADLINE} python3 -c {q(PATCH_PY)} {q(n)} {q("base/" + n)} {q("worktrees/" + n)}' + (f" {q(sub)}" if sub is not None else "")
    s = (f"cd {qp(h['root'])} || exit $?\nP={q(f'janus patch ({n}):')}\n"
         f"for t in flock python3 mktemp{' systemd-run systemctl' if remote else ''}; do command -v $t >/dev/null 2>&1 || "
         f'{{ echo "$P $t is missing on this host: no patch without its protections" >&2; exit {U}; }}; done\n'
         f'TO=$(command -v timeout || command -v gtimeout) || {{ echo "$P no GNU timeout on this host: no patch without its deadline" >&2; exit {U}; }}\n'
         f'L={PATCH_LOCK_DIR}/janus-patch-$(id -u).lock\nexec 9>>"$L" || {{ echo "$P cannot open the patch lock $L: no patch" >&2; exit {U}; }}\n'
         f'flock -n 9 || {{ echo "$P busy: another patch helper of this user runs on this host (one at a time, nothing queued): try again later" >&2; exit {PATCH_BUSY}; }}\n')
    if remote:
        s += (f"s=$({patch_scope_sh(h, 'stop')})\n"
              f'case "$s" in none|owned-inactive) ;; *) echo "$P its scope {sc} is $s, not free for this project (one that is not this project\'s is left alone): no patch" >&2; exit {U};; esac\n')
    s += f'T=$(mktemp {f}.tmp.XXXXXX) || exit {U}\nchmod 644 "$T"\n'
    # started in the background only to know its pid: systemd-run --scope and the guard exec, so it is timeout's pid, which
    # is its process group's id. timeout returns once the helper itself ended; a process of its group that ignored the TERM
    # gets the KILL here, PATCH_KILL s later.
    if remote:
        s += (f"systemd-run --user --scope -q --unit={q(sc)} --description={q(patch_scope_desc(h))} -p MemoryMax={PATCH_MEM_MB}M -p MemorySwapMax=0 -- "
              f'sh -c {q(PATCH_GUARD % {"unsafe": U})} sh {q(sc)} {q(CGROUP_ROOT)} {PATCH_MEM_MB * 1048576} "$P" {run} > "$T" &\n')
    else:
        s += f'{run} > "$T" &\n'
    s += f'pid=$!; wait $pid; rc=$?\ncase $rc in 124|137) kill -0 -$pid 2>/dev/null && {{ sleep {PATCH_KILL}; kill -KILL -$pid 2>/dev/null; }};; esac\n'
    if remote: s += f"{patch_scope_sh(h, 'stop', PATCH_KILL)} >/dev/null 2>&1\n"   # what is left in its scope (a process that left the group): stopped, when owned
    return s + (f'if [ $rc -eq 0 ]; then mv -f "$T" {f} && exit 0; rc=$?; fi\nrm -f "$T"\n'
                f'case $rc in 124|137) echo "$P no patch within {PATCH_DEADLINE} s: the helper\'s process group was stopped (TERM, then KILL after {PATCH_KILL} s); the earlier patch is kept" >&2;; esac\n'
                f"exit $rc\n")

def remote_patch(cfg, h, n, fname, sub=None):
    """Write out/<unit>/<fname> on host h: the agent's changes as a patch against the tree it was sent (base/<unit>), written
    and verified by git (PATCH_PY), contained as patch_script says. Returns (rc, stdout, stderr): rc is non-zero when no
    complete patch could be made (PATCH_BUSY: another helper runs there; PATCH_UNSAFE: its protections are not available), and
    then <fname> is left as it was (a partial patch never takes the place of an earlier whole one); stderr says why. sub: the
    changes under that subtree only (scoped_subdir), paths still relative to the copy."""
    return X(cfg, h, patch_script(h, n, fname, sub), timeout=PATCH_CALLER)

SCOPED_PATCH = "changes-scoped.patch"   # janus collect --subdir: never changes.patch, never the unit's collected state
PATCH_SKIP = frozenset(ast.literal_eval(re.search(r"^SKIP = (\{.*?\})", PATCH_PY, re.M).group(1)))   # the helper's own excluded folders

def scoped_subdir(s):
    """--subdir as the helper gets it ('a/b'), or Err: a relative subdirectory of the copy, never the copy itself, a .. component
    or a folder the patch leaves out (SKIP in PATCH_PY). Symlinks and existence are checked on the host (PATCH_PY), again."""
    parts = [x for x in (s or "").split("/") if x not in ("", ".")]
    if (s or "").startswith("/") or not parts or ".." in parts or PATCH_SKIP & set(parts):
        raise Err(f"--subdir {s!r}: give a relative subdirectory of the unit's copy (not absolute, not the copy itself, no .. component, "
                  f"no excluded folder: {', '.join(sorted(PATCH_SKIP))})")
    return "/".join(parts)

def collect_scoped(cfg, a, sub):
    """janus collect UNIT --subdir PATH: a patch of the changes under one subtree of a remote copy, in out/<unit>/changes-scoped.patch
    (written and verified on the host as remote_patch does, then fetched by rsync, which renames into place). A partial recovery: no
    fingerprint, no ledger, no record written, so stop and close still refuse until a full janus collect succeeds."""
    if ":" in a.unit: raise Err(f"--subdir is for a remote unit, not a job ({a.unit})")
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]; n = u["name"]
    if h.get("local"): raise Err(f"--subdir: {n} is a local unit: its tree is here, nothing to recover")
    if u.get("closing"): raise Err(f"{n} is being closed: its copy on {u['host']} is being removed (janus close {n} finishes it; an interrupted close that removed nothing: janus close {n} --abort)")
    od = cfg.p("out", "out") / n; p = (od / SCOPED_PATCH).resolve()
    prc, _, perr = remote_patch(cfg, h, n, SCOPED_PATCH, sub)
    if prc: raise Err(f"collect {n} --subdir {sub}: NO PATCH could be made on {u['host']} (exit {prc}): {(perr or '').strip()[-1500:] or 'no message'}"
                      + (f"; {p} is from an earlier scoped collect, kept" if p.exists() else ""))
    for l in (perr or "").splitlines():
        if l.strip(): say(l.strip())
    cmd = ["rsync", "-a", "-e", " ".join(SSH), f"{h['ssh']}:{h['root']}/out/{n}/{SCOPED_PATCH}", str(od) + "/"]
    if cfg.run.dry: say("+ " + shlex.join(cmd)); return
    od.mkdir(parents=True, exist_ok=True)
    rc, o, e = sh(cmd, timeout=600)
    if rc: raise Err(f"collect {n} --subdir {sub}: the patch was made on {u['host']} but fetching it failed (exit {rc}): {(e or o).strip()[-160:]}")
    say(f"scoped patch of {sub} (paths relative to the copy): {p} ({p.stat().st_size} bytes; apply in {u['cwd']} with git apply --3way)\n"
        f"partial recovery: {n} is NOT recorded collected; stop and close still need a full janus collect {n}")

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
    if h.get("local") or no_remote_copy(u): return None
    kv = fingerprint(cfg, u)
    if kv is None: return f"cannot read its copy on {u['host']} (unreachable?), so cannot tell whether work would be lost"
    if kv.get("fp") == u.get("collected"): return None
    d = kv.get("fp", "").split("|")[0].split()
    if len(d) > 1 and d[1] == "0" and int(kv.get("n", "0").strip() or 0) <= 1 and int(kv.get("p", "0").strip() or 0) == 0: return None   # nothing written yet
    return (f"its copy on {u['host']} has changes or output not collected " + (f"since {ts(u['collected_at'])}" if u.get("collected_at") else "yet")
            + f": janus collect {u['name']} first, or --force")

def cmd_collect(cfg, a):
    if getattr(a, "subdir", None) is not None: return collect_scoped(cfg, a, scoped_subdir(a.subdir))   # validated before anything runs
    if ":" in a.unit:
        with cfg.command_writer(): return collect_job(cfg, a.unit)
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]
    if h.get("local"): say("local unit: nothing to collect"); return
    if no_remote_copy(u) and u.get("closing"): raise Err(f"{u['name']} is being closed: its copy on {u['host']} is being removed (janus close {u['name']} finishes it; an interrupted close that removed nothing: janus close {u['name']} --abort)")
    root = h["root"]; n = u["name"]; od = cfg.p("out", "out") / n
    # The agent's changes come back as a patch against the tree it was sent (base/<unit>), never as an rsync over the Mac
    # checkout, which may have moved on or be shared (H9). Apply it with git apply --3way in the unit's worktree (the patch
    # carries git's blob ids, so a checkout that has moved on is merged, not refused).
    prc, _, perr = remote_patch(cfg, h, n, "changes.patch")
    if not save_output(cfg, u): raise Err(f"collect from {u['host']} failed (unreachable?): {u.get('_sync_error')}")   # out/<unit>/, the task log
    p = od / "changes.patch"
    if prc:   # no complete patch: the copy stays uncollected (no fingerprint recorded), so stop and close keep refusing
        raise Err(f"collect {n}: NO PATCH of its changes could be made on {u['host']} (exit {prc}): {(perr or '').strip()[-1500:] or 'no message (python3 missing on the host, or the 300 s limit?)'}\n"
                  f"Its output folder was brought here, but its copy on {u['host']} is NOT collected and its changes exist only there"
                  + (f"; {p} is from an earlier collect, not current" if p.exists() else "") + ". Remove the cause on the host, then janus collect again.")
    for l in (perr or "").splitlines():
        if l.strip(): say(l.strip())   # a path no patch can carry (fifo, socket, device) is named, never dropped in silence
    if not cfg.run.dry and not save_transcript(cfg, u): say(f"note: no transcript of {n} saved (none found on {u['host']})")   # out/<unit>/transcript.md
    st = task_status(h, u["task"]) if u.get("via") == "pueue" and u.get("task") is not None and not cfg.run.dry else None
    if not (st and task_rc(st) is None): X(cfg, h, f"rm -f {qp(f'{root}/run/{n}.key')}")   # the key fifo, once no task can still read it (row 52)
    kv = fingerprint(cfg, u) if not cfg.run.dry else None   # the last remote read; the lock is taken only now, for the state below
    with cfg.command_writer():
        bd, u = get_unit(cfg, a.unit)   # the record as it is now: a unit another command changed or closed meanwhile is not overwritten
        collect_state(cfg, bd, u, h, kv, p)

def collect_state(cfg, bd, u, h, kv, p):
    """The state half of cmd_collect, under the writer lock: the run's cost into the ledger, the fingerprint on the unit record."""
    n = u["name"]; od = cfg.p("out", "out") / n; mc = cfg.models.get(u.get("model"), {})
    if not cfg.run.dry and u.get("via") == "pueue" and mc.get("kind") == "api" and u.get("run"):
        cost, reported = run_cost(od / "api.json", None, u["run"])
        if reported: keep_run_json(cfg, od, u["run"]); ledger_done(cfg, cfg.prov[mc["provider"]], u["run"], n, cost, "collected")   # idempotent: once per run id
    if kv: u["collected"] = kv.get("fp"); u["collected_at"] = int(now()); bd.save(u)   # stop and close compare against it
    if not cfg.run.dry: bd.comment(u["id"], f"{ts()} collected from {u['host']}")
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

def duplicate_check(cfg, u, units, orig):
    """`janus close <unit> --duplicate-of <orig>` (soak 43): the way to close a relaunch that duplicated a unit still at work.
    It has no verdict and shares the original's tree, so neither the verdict rule nor --force can close it. Allowed only when
    janus recorded it as a relaunch of the open unit <orig> and its agent is stopped (asked of its host now); the original, its
    tree and its out folder are not touched, and the uncollected guard still holds for a remote copy."""
    o = next((x for x in units if x["name"] == orig and x["id"] != u["id"]), None)
    if not o: raise Err(f"--duplicate-of {orig}: no other open unit of that name")
    if u.get("relaunch_of") != orig and o.get("relaunched") != u["name"]: raise Err(f"{u['name']} is not a relaunch of {orig} (janus recorded none): close it by its verdict")
    if cfg.run.dry: return
    why = agent_running(cfg, u)
    if why: raise Err(why)

def agent_running(cfg, u):
    """Why u's agent may still run (asked of its host now), else None: still working, queued or blocked, or its host cannot say."""
    with cfg.unlocked(): ps = probe(u["host"], cfg.hosts[u["host"]])
    st = unit_state(u, {u["host"]: ps}, herdr_agents(cfg))
    if agent_live(u, st):
        return f"{u['name']}: its agent is " + (f"still {st}" if st != "unknown" else "in an unknown state (its host could not say whether it runs)") + f"; stop it first (janus stop {u['name']})"
    return None

def host_gone(cfg, u):
    """The owner marked u's host gone (janus hosts --mark H gone): its copy there is lost by that word."""
    return host_liveness(cfg).get(u.get("host"), ("up", None))[0] == "gone"

def tree_changed(cfg, cwd, head0):
    """Why the unit's tree is no longer what close checked (uncommitted changes now, or its HEAD moved from head0), else None."""
    dirty, _ = unit_dirty(cfg, cwd)
    if dirty: return f"{cwd} has uncommitted changes now ({', '.join(dirty[:3])})"
    head = sh(["git", "rev-parse", "HEAD"], cwd=cwd)[1].strip() or None
    return f"its HEAD moved ({(head0 or '?')[:9]} -> {(head or '?')[:9]})" if head != head0 else None

def close_reason(cfg, u):
    """What a close without --reason records: done, else the report's own final verdict, else that it had none."""
    v = verdict(cfg, u)
    return "done" if str(v).startswith("VERDICT: DONE") else (v or "no final verdict")[:200]

def close_abort(cfg, bd, u):
    """janus close <unit> --abort: a close that died after it marked the unit closing and before its cleanup began is undone, so the
    unit can be resumed, collected or landed (sweep-review-cc11-temporal-panel sat closing with a PENDING verdict and unlanded changes:
    resume and collect refused it, and a second close refused the same checks). Refused while that close runs, or once its cleanup began."""
    n = u["name"]
    if not u.get("closing"): raise Err(f"{n} is not being closed: nothing to abort")
    if u.get("busy") and not busy_dead(u): raise Err(f"{n}: its close is running (pid {u['busy'].get('pid')}): wait for it")
    if u.get("removing"): raise Err(f"{n}: its close began removing its copy and tab: run janus close {n} again to finish it")
    if cfg.run.dry: say(f"+ {n}: clear its closing mark"); return
    with cfg.writer():
        u = next((x for x in bd.units() if x["id"] == u["id"]), u); u.pop("closing", None); u.pop("busy", None); bd.save(u)
    say(f"{n}: the interrupted close is undone; it is open again (resume, collect, land or close it)")

def cmd_close(cfg, a):
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]; units = bd.units()
    if getattr(a, "abort", False): return close_abort(cfg, bd, u)
    handover = bool(getattr(a, "handover", False)); dup = getattr(a, "duplicate_of", None)
    redo = bool(u.get("closing")) and bool(busy_dead(u))   # a close that was interrupted: its checks passed then, the remote cleanup is repeated
    if u.get("busy") and not busy_dead(u): raise Err(f"{u['name']}: a {u['busy']['op']} of it is running (pid {u['busy'].get('pid')}); wait for it")
    if dup and a.force: raise Err("--duplicate-of with --force: --duplicate-of is the close without --force")
    mine = own_tree(cfg, u, units) or handover   # item 13: a successor took its tree, so --force is right
    head0 = None   # the tree's HEAD the git checks saw: rechecked before the cleanup, after the lock was let go
    gone = host_gone(cfg, u)
    if a.force:
        why = force_close_refusal(cfg, u, units)
        if why: raise Err(f"--force refused: {why}")
    # the uncollected guard: --force skips it for a tree of its own (as before) and for a gone host's copy (lost); a unit without a
    # tree of its own on a host that answers keeps it, since its host copy is the only copy of its work
    skip_guard = a.force and not handover and (mine or gone)
    if mine:
        dirty, state_repo = unit_dirty(cfg, u["cwd"])
        if dirty and not a.force: raise Err(f"{u['cwd']} has uncommitted changes ({', '.join(dirty[:3])}); commit and push, or --force")
        rc, o, e = sh(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=u["cwd"])
        if rc == 0 and o.strip() != "0" and not a.force and not state_repo:
            # a squash landing leaves the branch's own commits unpushed for good: only its published landing, verified on the
            # remote now and holding every file the branch changed, stands in for the push (branch.published_contains)
            with cfg.unlocked(): why = published_contains(cfg, u)   # git fetch: never under the writer lock
            if why: raise Err(f"unpushed commits; push first, or --force (not squash-landed: {why})")
            say(f"{u['name']}: its commits are squash-landed in the published {u['published']['commit'][:12]} (verified now)")
        head0 = sh(["git", "rev-parse", "HEAD"], cwd=u["cwd"])[1].strip() or None
    elif dup: duplicate_check(cfg, u, units, dup)   # soak 43: a stopped duplicate has no verdict to give and no tree of its own
    elif a.force:   # no tree of its own: --force closes without a verdict, never while its agent may still run (a gone host's never will)
        why = None if gone or cfg.run.dry else agent_running(cfg, u)
        if why: raise Err(why)
    elif not verdict(cfg, u) and u.get("via") != "failed":   # no tree of its own (H10): the git state is not the unit's; its REPORT is
        raise Err(f"{u['name']} has no final VERDICT line in {report_path(cfg, u)}")
    # A handover is never a reason to skip the uncollected guard (finding 3): a remote predecessor's dirty tree is its copy on
    # the host, so removing it without collect loses work. Only a plain --force skips the guard.
    with cfg.unlocked():   # the fingerprint and the probe ask the unit's host: an ack must not wait for them
        why = None if skip_guard or cfg.run.dry or redo else uncollected(cfg, u)
        st = unit_state(u, probe_all(cfg), herdr_agents(cfg)) if handover and not cfg.run.dry else None
    if why: raise Err(f"refused: {u['name']}: " + (why.replace(", or --force", " (--force keeps this guard for a unit without a tree of its own: its host copy is the only copy of its work)") if a.force else why))
    if handover and not cfg.run.dry:   # never close a predecessor whose agent is still running (finding 3; finding 5: unknown too)
        if agent_live(u, st):
            what = f"still {st}" if st != "unknown" else "in an unknown state (its host could not say whether it runs)"
            raise Err(f"{u['name']}: cannot hand over its tree: its agent is {what}; stop it first (janus stop {u['name']}), then hand its tree over")
    if not cfg.run.dry:   # the record as it is now; marked closing before the remote cleanup, which runs without the lock
        u = get_unit(cfg, a.unit)[1]
        if u.get("busy") and not busy_dead(u): raise Err(f"{u['name']}: a {u['busy']['op']} of it is running (pid {u['busy'].get('pid')}); wait for it")
        u["closing"] = True; u["busy"] = busy_mark("close"); bd.save(u)
    say(f"close {u['name']}" + ("" if mine else " (no worktree of its own: git checks skipped)"))
    why2 = None
    with cfg.unlocked():   # the host's cleanup, its probe and the task log take as long as the host does: the unit is recorded closing, busy
        if not (skip_guard or cfg.run.dry or redo):   # revalidate: the guard above ran before the lock was let go; collect now refuses this unit, so a fresh fingerprint against the current record is final
            why2 = uncollected(cfg, {**u, "closing": False, "busy": None})
    if why2:   # something changed since the guard: nothing was removed; undo the marks and refuse
        u = next((x for x in bd.units() if x["id"] == u["id"]), u); u.pop("closing", None); u.pop("busy", None); bd.save(u)
        raise Err(f"refused: {u['name']}: {why2} (it changed while the close was starting)")
    why3 = tree_changed(cfg, u["cwd"], head0) if mine and not a.force else None   # under the lock again, right before the cleanup
    if why3:
        if not cfg.run.dry: u = next((x for x in bd.units() if x["id"] == u["id"]), u); u.pop("closing", None); u.pop("busy", None); bd.save(u)
        raise Err(f"refused: {u['name']}: {why3} (it changed while the close was starting): commit and push, or land it, then close again")
    if not cfg.run.dry: u["removing"] = True; bd.save(u)   # from here a close is finished, never aborted (close_abort)
    with cfg.unlocked():
        close_tab(cfg, u)
        if u.get("via") == "tmux" and not gone: X(cfg, h, f'PATH="$HOME/.local/bin:{h.get("bin", "")}:$PATH"; tmux kill-session -t {tmux_target(unit_tmux(u), False)} 2>/dev/null; true')   # =: that name only, never a prefix match (a relaunch janus-<unit>-r2); its pid holds the agent's reservation
        if gone: say(f"{u['host']} is marked gone: its copy there is not removed, and its task log not saved (out/{u['name']}/ keeps what was synced)")
        elif not h.get("local"):
            root = h["root"]; n = u["name"]
            others = [x["name"] for x in units if x["id"] != u["id"] and x.get("host") == u["host"] and x.get("rcwd") == u.get("rcwd")]
            if others: say(f"keeping {u.get('rcwd')} on {u['host']}: open unit(s) {', '.join(others)} use it")
            else: X(cfg, h, f"cd {qp(root)} && rm -rf worktrees/{n} base/{n} briefs/{n}.md out/{n} run/{n}.* && rmdir worktrees base briefs out run 2>/dev/null; true")
        if u.get("via") == "pueue" and not gone:   # the task's log is kept under out/<unit>/ before the task is removed
            if not cfg.run.dry: save_task_log(cfg, h, u, u["task"])
            X(cfg, h, f"{pueue(h)} remove {int(u['task'])} 2>/dev/null; true")
        X(cfg, {"local": True}, f"BOARD_DIR={shlex.quote(str(board_dir(cfg)))} {shlex.quote(str(board_script(cfg)))} close {u['name']} 2>/dev/null; true")
        # a gone host's run ended with the host: settled now, or it stayed open in the ledger for good (its state reads unknown)
        ended = bool(u.get("run")) and not cfg.run.dry and (gone or unit_state(u, {u["host"]: probe(u["host"], h)}, {}) not in ("working", "queued", "unknown"))
    if cfg.run.dry: return
    u = next((x for x in bd.units() if x["id"] == u["id"]), u)   # as it is now: the run is settled and the issue closed on the current record
    if ended: settle_run(cfg, u, "closed")
    bd.comment(u["id"], f"{ts()} closed"); bd.run("close", u["id"], "--force", "--reason", a.reason or close_reason(cfg, u))   # --force: an ordering edge (blocks) never keeps an ended unit open
    say("closed")

def job_result(cfg, hs, ref, agent_host):
    """(exit code or None, log path the agent can read) of a finished job. A remote job's files are copied here first when
    the agent runs here (janus collect <host>:<task> does the same)."""
    hn, _, tid = ref.partition(":"); h = cfg.hosts[hn]; p = hs.get(hn) or {}
    t = ((p.get("pueue") or {}).get("tasks") or {}).get(tid); rc = task_rc(t); rd = runs_dir(cfg, h)
    jid = job_of(t) or (recorded_jid(cfg, hn, tid) if t is None and p.get("ok") else None)   # row 223: cancelled and removed, it is still that job
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

def unit_used(cfg, u):
    """(dollars the unit's runs have used, whether all of it is known). Review F10: a run that ended but is still open in the
    ledger (it is closed inside resume_unit, a remote one when its output arrives) counts at the cost its own json reports,
    and is said to be unknown without one: never at its budget, which is what the unit then read as money it had spent."""
    pv = cfg.prov.get(cfg.models.get(u.get("model"), {}).get("provider"), {})
    if "ledger" not in pv: return 0.0, True
    runs = ledger_read(cfg, pv)[0]; tot, known = 0.0, True
    for k in unit_runs(u):
        r = runs.get(k)
        if not r: continue
        if r["done"]: tot += r["cost"] or 0.0; continue
        c, rep = run_cost(cfg.p("out", "out") / u["name"] / "api.json", None, k) if k == u.get("run") else (0.0, False)
        if rep: tot += c
        else: known = False
    return round(tot, 2), known

def resume_message(cfg, hs, u, jobs):
    b = u.get("budget") or 0; s, known = unit_used(cfg, u) if b else (0.0, True)
    report = str(report_path(cfg, u)) if cfg.hosts[u["host"]].get("local") else f"{cfg.hosts[u['host']]['root']}/out/{u['name']}/REPORT.md"
    used = "" if not b else f" (your unit has used ${s:.2f} of its ${b:g} budget)" if known else \
        f" (your unit has used ${s:.2f} of its ${b:g} budget in its finished runs; what the run that just ended cost is not known yet)"
    L = [f"Your jobs finished{used}:"]
    for j in jobs:
        rc, log = job_result(cfg, hs, j, u.get("host"))
        note = ""
        if rc == 121:   # finding 10: nothing read exit 121; the unit is told what it means (the command never ran)
            note = " (never admitted: its host's admission never freed within the job's wait limit, so the command never ran; the log says why)"
        elif rc == 124: note = " (the command hit its own timeout)"
        L.append(f"- {j}: exit {rc if rc is not None else 'unknown (the task is gone and left no exit code)'}{note}; log {log}")
    L.append(f"Read only the logs you need, then continue from where {report} stands: detached jobs and `WAITING <job ids>` as before; "
             f"finish with the VERDICT line and the real exit codes.")
    return "\n".join(L)

class NoBudget(Err):
    """A resume refused because the unit's own budget cannot pay for it: the unit becomes resumable (item 7)."""

def reopen_report(cfg, u):
    """A unit resumed to do more: a final verdict on its report's line 1 goes back to PENDING (units.reopened) where its agent
    writes the report: here, and on its host, or the next sync would bring the old line back."""
    p = report_path(cfg, u)
    try:
        t = reopened(p.read_text())
        if t is not None: atomic_write(p, t)
    except OSError: pass
    h = cfg.hosts[u["host"]]
    if not h.get("local"):
        rp = f"{h['root']}/out/{u['name']}/REPORT.md"; rc, o, _ = hrun(h, f"cat {qp(rp)}")
        t = reopened(o) if rc == 0 else None
        if t is not None: put_script(cfg, h, rp, t, "644")

def resume_unit(cfg, u, msg, budget=None):
    """Continue the unit's own agent session with msg (see above). budget: a new budget for this run (`janus resume --budget`,
    same tree), instead of the unit's remaining budget (item 24: a capped unit with an unpushed commit can be continued). The
    caller saves u. Returns a line saying what was done."""
    name = u["name"]; h = cfg.hosts[u["host"]]; mc = cfg.models.get(u.get("model"), {}); via = u.get("via"); mem = memory_text(cfg); text = JANUS_MARK + msg + (f"\n\nProject memory (binding; it may have changed since you started):\n{mem}" if mem else "")
    ok, why = route_ok(cfg, u.get("model"))   # the config's restrictions bind every resume as they bind a launch: a removed, unknown or disabled model fails closed; no other route is tried
    if not ok: raise Err(f"{name}: not resumed: model {u.get('model')} is not allowed now ({why})")
    ag = herdr("agent", "list") if via == "herdr" and not cfg.run.dry else None   # None: herdr did not answer, so nothing is known gone
    if ag is not None and name not in {a.get("name") for a in ag.get("agents", [])}:   # stopped, or its pane closed: a prompt reaches nobody
        from janus.actions.backlog import relaunch_cmd
        raise Err(f"{name}: its herdr agent is gone: nothing prompted; relaunch it: {relaunch_cmd(cfg, u)}")
    turns = via == "tmux" and (u.get("turns") or per_turn(mc))
    if turns and not cfg.run.dry and turn_running(cfg, h, unit_tmux(u)):   # a new turn would kill the one running (respawn-pane -k)
        raise Err(f"{name}: its turn is still running in tmux {unit_tmux(u)} on {u['host']}: the message goes when that turn ends (or janus stop {name})")
    if not cfg.run.dry: reopen_report(cfg, u); u.pop("saved", None)   # saved: this turn's end is brought here again
    if turns:   # a per-turn client (opencode run): a new turn on the same session, the message on its command line, nothing pasted
        sid = u.get("session")
        if not sid: raise Err(f"{name}: no session id recorded (launched by an older janus): relaunch it with janus launch --resume-of {name}")
        msgf = f"{h['root']}/run/{name}.msg"
        try: u["pid"] = start_turn(cfg, h, unit_tmux(u), turn_command(cfg, h, mc, u["rcwd"], remote_env(cfg, h, name, level_of(cfg, u)), sid, msgf), text, msgf)
        except Err as e: raise Err(f"{name}: {e}")
        u["turns"] = True
        if not cfg.run.dry: agent_res(cfg, u["host"], h, name, u["pid"])   # the new turn's process holds the agent's reservation
        return f"{name}: " + (f"a new turn on session {sid} in tmux {unit_tmux(u)} on {u['host']}" if not cfg.run.dry else f"would start a new turn on session {sid} (dry run)")
    if via == "herdr":
        if not cfg.run.dry and herdr("agent", "prompt", name, text) is None: raise Err(f"{name}: herdr did not take the prompt (its agent waits at a dialog or prompt, or is gone): janus check names what it is blocked on")
        return f"{name}: " + ("prompted in its herdr pane" if not cfg.run.dry else "would prompt in its herdr pane")
    if via == "tmux":
        tm = shlex.quote(unit_tmux(u)); pre = f'PATH="$HOME/.local/bin:{h.get("bin", "")}:$PATH"; '
        if not cfg.run.dry and hrun(h, pre + f"tmux has-session -t {tm}")[0]:   # its session ended: start the client again on its own session, or say it cannot
            sid = session_of(cfg, u) if mc.get("kind") == "claude" else None
            if not sid: raise Err(f"{name}: its tmux session ended and its {mc.get('kind')} session id cannot be read here: resume it by hand in {u.get('rcwd')}")
            env_ = " ".join(f"{k}={shlex.quote(v)}" for k, v in remote_env(cfg, h, name, level_of(cfg, u)).items())   # JANUS_REPORT too: the Stop hook reads it
            # started without the prompt (its last argument): tmux refuses a command line that carries the project memory
            inner = f"cd {qp(u['rcwd'])} && exec env {env_} {agent_scope(cfg, h, mc)}{shlex.join(tool_argv(mc, text, sid)[:-1])}"
            try: u["pid"] = start_tmux(cfg, h, unit_tmux(u), inner); tmux_submit(cfg, h, unit_tmux(u), text)
            except Err as e: raise Err(f"{name}: {e}")
            return f"{name}: its session ended; started {mc['kind']} again on session {sid[:8]} in tmux {unit_tmux(u)} on {u['host']}"
        tmux_submit(cfg, h, unit_tmux(u), text)
        return f"{name}: " + (f"resumed in tmux {unit_tmux(u)} on {u['host']}" if not cfg.run.dry else f"would resume in tmux {unit_tmux(u)} on {u['host']} (dry run)")
    if via != "pueue" or mc.get("kind") != "api": raise Err(f"{name}: a unit run via {via} cannot be resumed")
    pv = cfg.prov[mc["provider"]]; od = cfg.p("out", "out") / name
    sid = session_of(cfg, u)
    if not sid: raise Err(f"{name}: no session id (api.json or transcript) to resume")
    if u.get("run"):   # the run before is closed first (idempotent): its reported cost, else its budget
        cost, rep = run_cost(od / "api.json", (u.get("budget") or pv.get("budget_per_unit", 0)), u["run"])
        if rep: keep_run_json(cfg, od, u["run"])   # the next run of this session is charged against it
        ledger_done(cfg, pv, u["run"], name, cost, "resumed")
    spent = unit_spent(cfg, u); explicit = budget is not None
    # The agent CLI stops a resumed run when THIS run's spend reaches its cap, and the first request of a resume loads the whole
    # context again: a cap under that cost stops at turn 1 with nothing done. So the wrap-up gets its budget on top of the
    # reload, and any other resume is refused when what is left cannot pay for the reload plus [budget] resume_min_usd.
    use = session_usage(cfg, u, sid=sid); reload = reload_usd(cfg, use); floor = round(reload + 0.1, 2)   # a resume must be able to pay for loading the context again
    if budget is None: budget = round((u.get("budget") or pv.get("budget_per_unit", 0)) - spent, 2)
    if budget < floor:
        need = f", and a resume needs ${floor:.2f} (${reload:.2f} to load its {use['ctx']}-token context again)" if reload else " (under [budget] resume_min_usd)"
        raise (Err if explicit else NoBudget)(f"{name}: ${budget:.2f} budget {'given' if explicit else 'left'}{need}: resume it with janus resume {name} --budget <usd> (same tree), or janus launch --resume-of and a new budget")
    # Item 4: a plain resume (and the wrap-up) draws on what is kept for this unit; a NEW budget must leave the other open units theirs.
    why = finish_refusal(cfg, mc, name, budget) if explicit and not cfg.run.dry else None
    if why: raise Err(why)
    k = len(unit_runs(u)) + 1; run = f"{name}.r{k}.{int(now())}"
    run = reserve_budget(cfg, mc, name, budget, run)
    text += "\n\n" + budget_note(cfg, budget, spent, round(spent + budget, 2) if explicit else u.get("budget"), reload)   # item 6
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
    u.pop("resumable", None)
    chk = f"herdr {name}" if via == "herdr" else f"pueue {u['host']}:{u['task']}" if via == "pueue" else f"tmux {u['host']}:{unit_tmux(u)}"
    board(cfg, "post", name, "waiting", f"resumed as task {u['task']}", env={"BOARD_CHECK": chk})   # item 9: a queued run is not GONE
    return f"{name}: resumed session {sid[:8]} as run {run} (${budget:g}), task {u['task']}"

def park_tab(cfg, u):
    """A parked unit's tab only shows a run that ended (pueue follow) or mirrors a session that waits (tmux attach): closed, so
    tabs do not pile up (issue 267). A herdr agent lives in its tab and is resumed there: that one stays."""
    return u.get("via") != "herdr" and close_tab(cfg, u)

def cmd_park(cfg, a):
    """Stop a live agent's turn while its jobs wait or run (the remedy for waiting:<unit>); the watcher resumes it, same session,
    when they finish. API units: the run is ended (SIGTERM: its cost is still recorded); herdr and tmux agents: Escape."""
    bd, u = get_unit(cfg, a.unit); h = cfg.hosts[u["host"]]; n = u["name"]
    with cfg.unlocked(): hs = probe_all(cfg)   # the hosts' answers: never under the writer lock
    jobs = unit_jobs(cfg, hs, n)
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
    park_tab(cfg, u); u["parked"] = {"jobs": jobs, "since": int(now()), "by": "janus park"}; bd.save(u)
    bd.comment(u["id"], f"{ts()} parked on {', '.join(jobs)}"); say(f"parked {n}")

def resume_outside_lock(cfg, bd, u, make_msg, budget=None):
    """resume_unit with its host I/O outside the writer lock (the message's job logs, ssh, tmux, queueing, confirm_started take as long as
    the host does). The unit is recorded busy first, under the lock, and saved: whoever meets that mark (another resume, the watcher, close,
    stop, check) sees an operation in flight, so a second resume of the same session cannot start. The lock is let go for the body and
    retaken after it; the mark is cleared when the resume ended (done or failed). Where the lock cannot be retaken (LockLost) the resume
    ran but its record was not written: the mark stays and check names the unit interrupted."""
    if cfg.run.dry: return resume_unit(cfg, u, make_msg(), budget=budget)
    if u.get("busy"): raise Err(f"{u['name']}: a {u['busy'].get('op')} of it " + ("is running" if not busy_dead(u) else "was interrupted") + f" (pid {u['busy'].get('pid')}): no second resume beside it; "
                                + (f"undo an interrupted close with janus close {u['name']} --abort" if busy_dead(u) and u.get("closing") else "janus check names it"))
    u["busy"] = busy_mark("resume"); bd.save(u)
    try:
        with cfg.unlocked(): line = resume_unit(cfg, u, make_msg(), budget=budget)
    except LockLost: raise
    except BaseException:
        u.pop("busy", None)
        with contextlib.suppress(Err): bd.save(u)
        raise
    u.pop("busy", None)
    return line

def answer_unit(cfg, name, q, text):
    """janus answer's delivery to the unit that asked: an agent whose turn ended on its question never reads the board again, so it
    is resumed with the answer (cmd_resume, with every guard of a resume). A herdr agent is prompted only when idle: working, it
    reads the board itself; gone or unknown, a prompt reaches nobody and the resume would still reopen its report."""
    try: _, u = get_unit(cfg, name)
    except Err: return   # not an open unit (the orchestrator's own agent, a closed unit): the board answer is all
    st = unit_state(u, {}, herdr_agents(cfg)) if u.get("via") == "herdr" else None
    if st not in (None, "idle"): return say(f"{name} not resumed with the answer: its herdr agent is {st}" + (" and reads it on the board" if st == "working" else ""))
    try: cmd_resume(cfg, argparse.Namespace(unit=name, message=f"The answer to your question {q}: {text}", force=False, budget=None))
    except LockLost: raise
    except Err as e: say(f"{name} not resumed with the answer: {e}")

def cmd_resume(cfg, a):
    """Resume a unit's own session now: parked (its jobs' results, when they finished), or with --message.

    Finding 2: never a second agent beside a running one -- a pueue API run's resume queues a NEW task, so a still-running agent
    task is refused even with --force; tmux/herdr only type into the agent's own pane, so they are never refused on state (a
    blocked herdr unit can be answered), and a review/audit unit may resume in the tree it reviews (the launch H9 exception).
    A still-running job is never marked handled, so the next park line still names it; --force overrides only the 'not finished'
    refusal."""
    with cfg.unlocked(): hs = probe_all(cfg)   # the hosts' answers: never under the writer lock; the unit is read after it, so its record is current
    bd, u = get_unit(cfg, a.unit)
    jobs = (u.get("parked") or {}).get("jobs") or waiting_line_jobs(cfg, u)
    if a.message: msg = lambda: a.message
    elif jobs: msg = lambda: resume_message(cfg, hs, u, jobs)
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
    say(resume_outside_lock(cfg, bd, u, msg, budget=getattr(a, "budget", None)))
    if cfg.run.dry: return
    done = [j for j in jobs if job_state(hs, j)[0] in ("done", "gone")]   # finding 2: a running job is never marked handled
    if done: u["handled_jobs"] = [*u.get("handled_jobs", []), *done]
    u.pop("parked", None); bd.save(u)
    bd.comment(u["id"], f"{ts()} resumed")

def save_output(cfg, u):
    """Bring a remote unit's output folder here (out/<unit>/: REPORT.md, api.json and each run's api-<run>.json, logs; never the
    key fifo) and keep its pueue task's log. Nothing is written outside out/<unit>/ (soak 43b: the report of a unit that continued
    another's used to be copied over that other unit's REPORT.md). True when done."""
    h = cfg.hosts[u["host"]]; n = u["name"]; od = cfg.p("out", "out") / n
    cmd = ["rsync", "-a", "--exclude", "*.key", "-e", " ".join(SSH), f"{h['ssh']}:{h['root']}/out/{n}/", str(od) + "/"]
    if cfg.run.dry: say("+ " + shlex.join(cmd)); return True
    od.mkdir(parents=True, exist_ok=True)
    # the report first, on its own: a bulky output folder that timed out left the 17-byte seed here while the host held the report
    src = f"{h['ssh']}:{h['root']}/out/{n}/"
    rr = sh(["rsync", "-a", "--ignore-missing-args", "-e", " ".join(SSH), src + "REPORT.md", src + "report.json", str(od) + "/"], timeout=120)
    rc, o, e = sh(cmd, timeout=600)
    if rc:
        u["_sync_error"] = (f"timed out after 600 s copying out/{n}/ (its bulky files: janus tidy)" if rc == 124 else f"exit {rc}: {(e or o).strip()[-160:]}") \
            + ("; its REPORT.md was brought here" if rr[0] == 0 else f"; its REPORT.md was not brought here either (exit {rr[0]})")
        return False
    if u.get("via") == "pueue" and u.get("task") is not None: save_task_log(cfg, h, u, u["task"])
    return True

def settle_remote(cfg, u):
    """A remote API unit whose agent ended and whose output was just brought here: its run is closed in the ledger at the cost
    its json reports (its own cost, run_cost), so it no longer holds its whole reservation until someone runs janus collect. A
    run that reported no cost stays open: collect, stop, close or janus ledger reconcile close it (never undercounted)."""
    mc = cfg.models.get(u.get("model"), {}); pv = cfg.prov.get(mc.get("provider"), {})
    if cfg.run.dry or u.get("via") != "pueue" or mc.get("kind") != "api" or not u.get("run") or "ledger" not in pv: return
    od = cfg.p("out", "out") / u["name"]; cost, reported = run_cost(od / "api.json", None, u["run"])
    if reported: keep_run_json(cfg, od, u["run"]); ledger_done(cfg, pv, u["run"], u["name"], cost, "ended")

def saved_marker(u):
    """What `saved` records when a remote unit's output was brought here: its pueue task, else the end of its tmux turn (a per-turn
    client's pane records when its process ended: each turn's end is a new marker, so each is brought here), else 'tmux' (a tmux
    unit has no task, finding 5, so `saved == task` is never None == None)."""
    if u.get("task") is not None: return u["task"]
    return f"tmux:{u['_turn_end']}" if u.get("_turn_end") else "tmux"

def refresh_due(cfg, u):
    """True when refresh_remote brings a remote unit's output here now: its agent ended and it is unsaved, its report was written
    on its host after it was last brought here (the probe's mtime: a copy that lagged 11 lines, 2026-11-24), or its last backup is
    older than [collect] backup_min. A record without a known host (a launch still writing it) is never due."""
    h = cfg.hosts.get(u.get("host"))
    if h is None or no_remote_copy(u) or h.get("local"): return False
    if u.get("state") == "unknown": return False   # its host did not answer this pass's probe: an ssh per unit per pass only fails and floods watch.log
    if u.get("_report_mtime", 0) > u.get("saved_at", 0): return True
    return (u.get("state") in ENDED and u.get("saved") != saved_marker(u)) or now() - u.get("saved_at", 0) > cfg.c.get("collect", {}).get("backup_min", 30) * 60

def fetch_unit(cfg, u):
    """One unit's remote read phase, writing no state: (backup patch rc, its stderr, whether the output folder was saved)."""
    prc, _, perr = remote_patch(cfg, cfg.hosts[u["host"]], u["name"], "backup.patch") or (0, "", "")   # the agent's changes so far, as a patch (out/<unit>/backup.patch)
    base = (u.get("repo") or {}).get("base")
    if base and u.get("rcwd") and u.get("state") in ENDED and not cfg.run.dry:   # an agent that committed and ended with its report PENDING
        rc, o, _ = hrun(cfg.hosts[u["host"]], f"git -C {qp(u['rcwd'])} rev-list --count {shlex.quote(base)}..HEAD", timeout=30)
        if rc == 0 and o.strip().isdigit(): u["_commits"] = int(o.strip())
    return prc, perr, bool(save_output(cfg, u))

def refresh_remote(cfg, u, bd, log, fetched=None):
    """A remote unit's output is read here: saved once when its agent's turn ended (then its verdict, WAITING line and api.json
    are judged here), and every [collect] backup_min while it runs (item 4). fetched: what fetch_remote_outputs brought before the
    writer lock was taken (the watcher); a unit that became due since is left to the next pass, never copied under the lock."""
    if not refresh_due(cfg, u): return
    fin = u.get("state") in ENDED
    if fetched is None: got = fetch_unit(cfg, u)
    elif u["name"] in fetched: got = fetched[u["name"]]
    else: return
    prc, perr, ok = got
    if prc: log(f"{u['name']}: no backup patch could be made on {u['host']} (exit {prc}; the earlier one is kept): {(perr or '').strip()[-300:]}")
    if not ok: log(f"{u['name']}: saving its output from {u['host']} failed ({u.get('_sync_error')})"); return
    u["saved_at"] = int(now())
    if "_commits" in u: u["commits_since"] = u["_commits"]   # its commits since launch on its host (status, unit:<n>:ended-pending)
    if fin: u["saved"] = saved_marker(u); settle_remote(cfg, u)   # its run is closed when it ends, not at collect
    bd.save(u)
    u["verdict"] = v = verdict(cfg, u)
    if not v and not u.get("_park") and fin:
        pk = waiting_line_jobs(cfg, u)
        if pk: u["_park"] = pk; u["state"] = "parked"

def remote_to_sync(cfg, units):
    """The units whose remote output check brings here: a remote unit whose agent ended and whose output is not saved yet."""
    return [u for u in units if not cfg.hosts.get(u.get("host"), {}).get("local") and not no_remote_copy(u)
            and u.get("state") in ENDED and u.get("saved") != saved_marker(u)]

def fetch_remote_outputs(cfg, units):
    """The read phase of sync_remote_outputs and refresh_remote (ssh and rsync, no state written; it runs outside the writer
    lock): {unit name: fetch_unit's result} for each unit one of them will bring here."""
    got = {}
    if cfg.run.ro: return got
    for u in units:
        if refresh_due(cfg, u):
            try: got[u["name"]] = fetch_unit(cfg, u)
            except Err: got[u["name"]] = (1, "", False)
    return got

def sync_remote_outputs(cfg, units, bd, fetched=None):
    """Item 11 addendum (finding 5): before check judges a remote unit parked vs orphan, bring its report and the end-of-turn
    backup patch here once its agent ended (the local report has no WAITING line until janus collect, so its jobs looked
    orphaned). Remote tmux units (no pueue task) are synced too, and a failed sync fails closed: the unit is marked so its jobs
    are never cancelled on the stale local report. fetched: what fetch_remote_outputs brought before the writer lock was taken; the
    record is judged again here, so a unit another command saved or ended meanwhile is not overwritten, and one that ended after
    the fetch is marked failed (its report was not brought)."""
    if cfg.run.ro: return
    if fetched is None: fetched = fetch_remote_outputs(cfg, units)
    for u in remote_to_sync(cfg, units):
        if not (fetched.get(u["name"]) or (0, "", False))[2]: u["_sync_failed"] = True; continue
        u["saved"] = saved_marker(u); u["saved_at"] = int(now())
        try: settle_remote(cfg, u); bd.save(u)
        except Err: pass

RESUME_TRIES = 3   # failed resumes of the same finished jobs before the watcher stops trying

def once(cfg, key, cause):
    """Record that one action was taken for (key, cause) and say whether it is new: False when it was already taken, so a
    resume is never started twice for the same finished jobs. Only a real, observed condition reaches it."""
    rec = cfg.jget("once.json"); k = f"{key}|{cause}"
    if k in rec: return False
    rec[k] = int(now()); cfg.jput("once.json", dict(list(rec.items())[-300:])); return True

def unonce(cfg, key, cause):
    """Undo once() for an action that failed before it started anything: the next pass may try again (bounded by the next pass)."""
    rec = cfg.jget("once.json")
    if rec.pop(f"{key}|{cause}", None) is not None: cfg.jput("once.json", rec)

def tend_parked(cfg, hs, units, log, fetched=None):
    """The watcher's part: record parked units, and resume those whose jobs all finished (done or gone; an unknown host is
    never a reason)."""
    bd = Beads(cfg)
    for u in units:
        try:
            refresh_remote(cfg, u, bd, log, fetched)
            jobs = u.get("_park")
            if jobs:
                if (u.get("parked") or {}).get("jobs") != jobs:
                    park_tab(cfg, u); u["parked"] = {"jobs": jobs, "since": int(now()), "by": "report"}; bd.save(u); log(f"park {u['name']} on {', '.join(jobs)}")
                    if card_fields(cfg, u["name"]).get("state") != "waiting": board(cfg, "post", u["name"], "waiting", " ".join(jobs))   # never SILENT/GONE
                st = [job_state(hs, j)[0] for j in jobs]
                if all(x in ("done", "gone") for x in st):
                    # once only (panel-eng F1): the handled fact is recorded here before the resume and guards it; the unit
                    # record (bd) is saved best-effort after, so a failed save cannot start a second run on the same session
                    key, cause = f"parked:{u['name']}", f"resume:{','.join(jobs)}"
                    if (u.get("resume_error") or {}).get("n", 0) >= RESUME_TRIES: continue
                    if not cfg.run.dry:   # an earlier unit's resume let the lock go: judge this unit on its record as it is now
                        cur = next((x for x in bd.units() if x["id"] == u["id"]), None)
                        if cur is None or cur.get("busy") or set(jobs) <= set(cur.get("handled_jobs", [])) or (cur.get("parked") or {}).get("jobs") != jobs:
                            log(f"{u['name']}: changed or busy since this pass read it: left to the next pass"); continue
                    if cfg.run.dry or once(cfg, key, cause):
                        try: log(resume_outside_lock(cfg, bd, u, lambda: resume_message(cfg, hs, u, jobs)))
                        except LockLost: raise   # the resume ran, its record is not written: the pass stops here, never on without the lock
                        except Err as e:   # nothing started: recorded on the unit, check reports it; the next pass tries again
                            if not cfg.run.dry: unonce(cfg, key, cause)
                            if not cfg.run.dry:   # bounded: after RESUME_TRIES failures the watcher leaves it (parked:<unit>:resume says so)
                                u["resume_error"] = {"why": str(e), "at": int(now()), "n": (u.get("resume_error") or {}).get("n", 0) + 1}; save_soft(bd, u, log)
                            raise
                        u.pop("resume_error", None)
                    elif set(jobs) <= set(u.get("handled_jobs", [])): continue
                    else: log(f"{u['name']}: already resumed for {', '.join(jobs)}; saving the unit record again")
                    if cfg.run.dry: continue
                    u["handled_jobs"] = [*u.get("handled_jobs", []), *jobs]; u.pop("parked", None); save_soft(bd, u, log)
                continue
        except LockLost: raise
        except Err as e: log(f"{u['name']}: {e}")

def save_soft(bd, u, log):
    """Save the unit record best-effort: the once-only facts live in .janus/autopilot.json, so a busy bd only delays it."""
    try: bd.save(u)
    except Err as e: log(f"{u['name']}: saving the unit record failed ({e}); retried next pass")

def save_transcript(cfg, u):
    """out/<unit>/transcript.md: the whole condensed transcript (collect; it outlives the remote copy)."""
    rc, txt = read_transcript(cfg, u, "", 0)
    if rc == 0 and txt and not cfg.run.dry: atomic_write(cfg.p("out", "out") / u["name"] / "transcript.md", txt + "\n")
    return rc == 0
