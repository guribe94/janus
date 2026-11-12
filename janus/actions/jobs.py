"""Build and test jobs in each host's own pueue lanes: job scripts, admission, status, cancel, busy, orphans, thrash.

janus run / janus cancel / janus busy / janus thrash, and the job queries every layer above uses (task_status, task_rc,
job_state, unit_jobs, orphan_jobs)."""
import argparse, calendar, json, os, re, shlex, subprocess, sys, time
from pathlib import Path
from janus.beads import Beads
from janus.board import board
from janus.cap import cap_lib, cap_nums, class_mb, host_cap
from janus.context import Err, SSH, atomic_write, fl, hrun, now, pq, qp, say, sh
from janus.probe import probe, probe_all, pueue
from janus.templates import ADMIT
from janus.units import agent_ended, unit_state

# A job belongs to the host's pueue daemon, never to the ssh connection that queued it: if the caller dies the job keeps running,
# and its log and exit code stay under <root>/runs/<jid>.{log,rc} on that host for `janus collect <host>:<task>`.

# Opt-in per host: [hosts.X] shared = true, thrash_psi (memory PSI avg10, default 40), thrash_min (default 5). When the host's
# memory pressure stays over thrash_psi for thrash_min, the newest admitted job of THIS project there (a job janus run recorded
# in .janus/runs/jobs, running past admission) is stopped (cancel_job: its files saved, pueue kill, its container or scope by
# name) and re-queued on another host where it may run, else on the same host (it waits for admission). Nothing else is touched.

def job_state(hs, ref):
    """(state, exit code) of job <host>:<task> from the probes: queued | admission (running, waiting in janus's admission) |
    running | done | gone (no such task any more) | unknown (host unreachable) | paused, stashed, locked."""
    hn, _, tid = ref.partition(":"); p = hs.get(hn) or {}
    if not p.get("ok"): return "unknown", None
    t = ((p.get("pueue") or {}).get("tasks") or {}).get(tid)
    if not t: return "gone", None
    rc = task_rc(t)
    if rc is not None: return "done", rc
    k = t["status"] if isinstance(t["status"], str) else next(iter(t["status"]), "")
    if k == "Running": return ("admission" if job_of(t) in p.get("waitjob", []) else "running"), None
    return k.lower(), None

def unit_jobs(hs, name):
    """The unit's own jobs that have not finished (pueue label unit:<name>, any host): ['<host>:<task>']."""
    return [f"{hn}:{tid}" for hn, p in hs.items() for tid, t in ((p.get("pueue") or {}).get("tasks") or {}).items()
            if t.get("label") == f"unit:{name}" and task_rc(t) is None]

def save_task_log(cfg, h, u, tid, remove=False):
    """Keep a pueue task's log under out/<unit>/task-<id>.log (the agent's stdout and stderr), then optionally remove the task."""
    rc, o, e = hrun(h, f"{pueue(h)} log {int(tid)} --full", timeout=60)
    if rc == 0 and not cfg.run.dry:
        od = cfg.p("out", "out") / u["name"]; od.mkdir(parents=True, exist_ok=True); atomic_write(od / f"task-{tid}.log", o)
        if remove: hrun(h, f"{pueue(h)} remove {int(tid)} 2>/dev/null; true")
    return rc == 0

LANES = ("gates", "jobs", "land", "gates-quick")   # gates-quick: one slot kept for gates under [landing] quick_gate_min

AGE_MIN = {"gates": 240, "jobs": 120, "land": 240, "gates-quick": 60}   # class age limits (minutes from enqueue); janus.toml [lanes.<lane>] age_min overrides

LSSH = [*SSH, "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]

def lane_age(cfg, lane): return cfg.c.get("lanes", {}).get(lane, {}).get("age_min", AGE_MIN.get(lane, 120))

def slots(h, cfg=None):
    """pueue parallel limit of a host's jobs lane: the jobs its cores hold (cores // cores_per_job). Pueue only orders the
    work; each job still waits for admission by reservations (runs/cap.sh), which also counts memory."""
    n = cap_nums(cfg, h); return max(1, n["cores"] // n["cpj"])

def agent_res(cfg, hn, h, name, pid):
    """An interactive agent's reservation (runs/agent-<unit>.res, its pane's pid), as API agents write theirs: the shell admission
    (cap.sh) then counts it like the launcher does (review-fixes8 #3); it is dropped when the pid is gone."""
    if not pid: return
    mb = class_mb([], "agent", cap_nums(cfg, h)["agmb"], 0)
    write_cap(cfg, hn, h)
    hrun(h, f"mkdir -p {qp(h['_rd'])} && echo '{int(pid)} 0 {int(mb)} {int(now())} agent' > {qp(h['_rd'] + f'/agent-{name}.res')}")

def ensure_lanes(h, cfg=None):
    p = pueue(h); n = slots(h, cfg)
    g = {"agents": 1, "jobs": n, "gates": h.get("gate_slots", n)} | ({"land": 1, "gates-quick": 1} if h.get("local") else {})
    return "; ".join(f"{p} group add -p {int(k)} {g_} >/dev/null 2>&1" for g_, k in g.items()) + "; true"

def runs_dir(cfg, h): return h["_rd"]

def write_cap(cfg, hn, h):
    """Write runs/cap.sh on host h (write-then-rename): waiting jobs pick up the current rule at their next poll."""
    rd = h["_rd"]
    if h.get("local"): Path(rd).mkdir(parents=True, exist_ok=True); atomic_write(Path(rd) / "cap.sh", cap_lib(cfg, hn, h)); return 0, "", ""
    d = qp(rd); return hrun(h, f"mkdir -p {d} && cat > {d}/.cap.sh.$$ && mv -f {d}/.cap.sh.$$ {d}/cap.sh", inp=cap_lib(cfg, hn, h))

def ctr(h):
    """A podman host's container settings ([hosts.<h>.container]): image, mount (host dir, default the host's root), mount_at
    (default /f), args, usb_args, usb_lock, host_dirs, name_prefixes, cpus."""
    c = dict(h.get("container", {})); c.setdefault("mount", h.get("fleet", h.get("root", "/tmp"))); c.setdefault("mount_at", "/f")
    return c

def in_container(h, path):
    """A host path as a podman job sees it (under the mount), or the path itself."""
    if h.get("runner") != "podman": return path
    c = ctr(h); m = c["mount"].rstrip("/")
    return c["mount_at"] + path[len(m):] if path.startswith(m + "/") else path

def remote_wt(h, name):
    """Where `janus run` syncs a worktree on a remote host."""
    if h.get("runner") == "podman": return f"{ctr(h)['mount']}/worktrees/{name}"
    return f"{h.get('worktrees', h.get('root', '$HOME/janus') + '/worktrees')}/{name}"

def job_script(cfg, hn, h, a, name, cmd, jid, wd=None, clsrc=None):
    """The bash script pueue runs on the host. Output is tee'd to <runs>/<jid>.log, the exit code written to <jid>.rc.
    Every lane on every host (the Mac too, register item 5) waits for admission by reservations (ADMIT, runs/cap.sh), holds
    its reservation while it runs, and records its class's peak memory in runs/history.tsv. clsrc: shell text giving the
    command whose class it is (default the command itself). wd: the tree on the host (a remote agent's copy); default the
    synced worktree of that name. Per host (janus.toml): run_env (shell run first), job_setup (shell run in the worktree before
    the command), job_log (a line per job start/end), and for runner = "podman" the [hosts.<h>.container] settings."""
    q = shlex.quote; rd = h["_rd"]; sub = a.subdir or "."; sub_ = lambda x: str(x).replace("{name}", name).replace("{jid}", jid)
    head = f"#!/bin/bash\n# janus run {jid}: host {hn}, lane {a.lane}, worktree {name}\nRD={qp(rd)}; R=\"$RD\"/{q(jid)}\nexport JANUS_WRAP={q(jid)}   # janus run inside records the jobs it queues under this (cancel follows them)\n"
    setup = h.get("job_setup", ""); log = h.get("job_log", "/dev/null")
    adm = ADMIT % {"lane": q(a.lane), "clsrc": clsrc or q(cmd), "jid": q(jid)}
    if h.get("local"):
        pre = ""
        if a.lane == "land":   # the local landing lane also waits for: the disk floor, the host's admit_check, no legacy_lanes lane busy
            ck = h.get("admit_check", ""); base = h.get("legacy_lanes", ""); ce = h.get("admit_check_env", "")
            pre = (f"said=\nwhile :; do d=$(df -Pk {q(str(cfg.root))} | awk 'NR==2{{print int($4/1048576)}}'); busy=\n"
                   f"  for s in {base}; do o=$(cat $s/pid 2>/dev/null); [ -n \"$o\" ] && kill -0 $o 2>/dev/null && busy=$s; done\n"
                   f"  [ \"$d\" -ge {h.get('min_disk_gb', 5)} ] && {{ [ -z {q(ck)} ] || {ce + ' ' if ce else ''}sh {q(ck)} >/dev/null 2>&1; }} && [ -z \"$busy\" ] && break\n"
                   f"  [ \"$said\" ] || {{ echo \"janus: land lane waiting (disk ${{d}} GB, floor {h.get('min_disk_gb', 5)}; old lane busy: ${{busy:-none}})\"; said=1; }}; sleep 15; done\n")
        body = f"{pre}cd {q(str(Path(wd or a.wt).resolve()))} && cd {q(sub)} || exit $?\n{adm}run_sampled nice -n 10 bash -c {q(cmd)}\n"
    else:
        cap = int(h.get("job_gb", 2) * 1024)
        scope = f"systemd-run --user --scope -q {q('--unit=janus-' + jid)} -p MemoryMax={cap}M --"
        if h.get("runner") == "podman":   # the job runs in the host's container settings, capped at job_gb
            c = ctr(h)
            if not c.get("image"): raise Err(f"host {hn}: runner podman needs [hosts.{hn}.container] image")
            inner = (f"({setup}) && " if setup else "") + f"cd {q(sub)} && {cmd}"
            hw = " ".join(map(q, map(sub_, c.get("usb_args", [])))) + " " if a.usb else ""
            hw_ = wd or remote_wt(h, name); cw = in_container(h, hw_)
            args = " ".join(q(sub_(x)) for x in c.get("args", []))
            body = (f"[ -d {qp(hw_)} ] || {{ echo {q(f'janus: no worktree {hw_} on {hn}')}; exit 2; }}\n"
                    + "".join(f"mkdir -p {q(sub_(d))}\n" for d in c.get("host_dirs", []))
                    + (f"exec 8>{qp(c['usb_lock'])}; flock 8\n" if a.usb and c.get("usb_lock") else "") + adm +
                    f"printf '%s janus-run start [%s] %s %s\\n' \"$(date '+%F %T')\" {q(name)} {q(jid)} {q(cmd)} >> {qp(log)}\n"
                    f"run_sampled {scope} nice -n 15 ionice -c 3 podman run --rm --init --name {q('janus-run-' + jid)} {hw}"
                    f"--memory={cap}m --memory-swap={cap}m --cpus={q(str(c.get('cpus', h.get('cores_per_job', 2))))} -v {qp(c['mount'])}:{q(c['mount_at'])} -w {qp(cw)} {args} "
                    f"{q(c['image'])} bash -c {q(inner)}; rc=$?\n"
                    f"printf '%s janus-run end [%s] %s exit %s\\n' \"$(date '+%F %T')\" {q(name)} {q(jid)} $rc >> {qp(log)}\nexit $rc\n")
        else:
            wd = wd or remote_wt(h, name)
            body = (f"{h.get('run_env', '')}\ncd {qp(wd)} && " + (f"({setup}) && " if setup else "") + f"cd {q(sub)} || exit $?\n" + adm +
                    f"printf '%s janus-run [%s] %s %s\\n' \"$(date '+%F %T')\" {q(name)} {q(jid)} {q(cmd)} >> {qp(log)}\n"
                    f"run_sampled {scope} nice -n 10 bash -c {q(cmd)}\n")
    return head + "main() {\n" + body + "}\nmain 2>&1 | tee -a \"$R.log\"\nrc=${PIPESTATUS[0]}; echo $rc > \"$R.rc\"; exit $rc\n"

def sync_wt(cfg, hn, h, src, name):
    """Sync a worktree to the host: --checksum and no -t (a changed file gets the transfer time as its mtime, so cargo never
    reuses an artifact built from older content), without the host's sync_exclude list (default .git, target, .venv,
    node_modules; anchor an entry with a leading / so nested folders of that name are still synced). A podman host first clones
    its container.clone_base (cp --reflink) when the worktree is new. Every path in paths.sync_require that exists here must
    arrive (a missing one has broken gates before)."""
    ex = [y for x in h.get("sync_exclude", [".git", "target", ".venv", "node_modules"]) for y in ("--exclude", x)]
    dst = remote_wt(h, name)
    if h.get("runner") == "podman" and ctr(h).get("clone_base"):
        rc, o, e = hrun(h, f"[ -d {qp(dst)} ] || cp -a --reflink=always {qp(ctr(h)['clone_base'])} {qp(dst)}", timeout=300)
        if rc: raise Err(f"reflink clone on {hn} failed: {(e or o).strip()[-120:]}")
    rdst = dst.replace("$HOME/", "")   # rsync resolves a relative path under the remote home
    cmd = ["rsync", "-rlpgoD", "--checksum", "--delete", "-e", " ".join(SSH), *ex, f"{src}/", f"{h['ssh']}:{rdst}/"]
    r = subprocess.run(cmd)
    if r.returncode: raise Err(f"rsync to {hn} failed (exit {r.returncode})")
    for x in cfg.paths.get("sync_require", []):
        if (Path(src) / x).exists() and hrun(h, f"test -e {qp(dst + '/' + x)}")[0]: raise Err(f"{x} did not arrive on {hn}")

def task_status(h, tid):
    rc, o, e = hrun(h, f"{pueue(h)} status --json", timeout=30)
    if rc: return None
    try: return (json.loads(o).get("tasks") or {}).get(str(tid))
    except Exception: return None

def task_rc(t):
    s = (t or {}).get("status"); k = s if isinstance(s, str) else next(iter(s or {}), "")
    if k != "Done": return None
    r = s["Done"].get("result") if isinstance(s, dict) else None
    if r == "Success": return 0
    if isinstance(r, dict) and "Failed" in r: return int(r["Failed"])
    return 137 if r == "Killed" else 1

def finish_job(cfg, hn, h, tid, jid, quiet=False):
    """Wait for the task (retrying through ssh drops), print its log, return its exit code."""
    miss = 0
    while True:
        if h.get("local"): rc = subprocess.run(["sh", "-c", f"{pueue(h)} wait -q {int(tid)}"]).returncode
        else: rc = subprocess.run([*LSSH, h["ssh"], f"{pueue(h)} wait -q {int(tid)}"], stdin=subprocess.DEVNULL).returncode
        t = task_status(h, tid)
        if task_rc(t) is not None: break
        miss = miss + 1 if t is None and rc != 255 else 0
        if miss >= 5:   # the task is gone (removed or cleaned): its rc file, else give up (M16)
            r2, o2, _ = hrun(h, f"cat {qp(f'{runs_dir(cfg, h)}/{jid}.rc')} 2>/dev/null")
            if o2.strip().lstrip("-").isdigit(): return int(o2.strip())
            print(f"janus: {hn} task {tid} is gone and left no exit code", file=sys.stderr); return 255
        print(f"janus: {hn} task {tid} not done yet (wait exit {rc}); retrying", file=sys.stderr); time.sleep(30 if rc == 255 or t is None else 5)
    rd = runs_dir(cfg, h)
    if not quiet:
        if h.get("local"): subprocess.run(["cat", f"{rd}/{jid}.log"])
        else: subprocess.run([*LSSH, h["ssh"], f"cat {qp(f'{rd}/{jid}.log')}"], stdin=subprocess.DEVNULL)
    rc, o, e = hrun(h, f"cat {qp(f'{rd}/{jid}.rc')} 2>/dev/null")
    try: return int(o.strip())
    except ValueError: return task_rc(t)

def cmd_run(cfg, a):
    hn = a.host
    if hn not in cfg.hosts: raise Err(f"unknown host {hn}")
    h = cfg.hosts[hn]
    if a.lane == "land" and not h.get("local"): raise Err("the land lane is the Mac's")
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd: raise Err("no command")
    cmd = cmd[0] if len(cmd) == 1 else shlex.join(cmd)   # one argument is shell text; several are quoted words
    src = str(Path(a.wt).resolve()); name = Path(src).name
    if not Path(src).is_dir(): raise Err(f"worktree {src} not found")
    unit = a.unit or os.environ.get("JANUS_UNIT")
    why = tree_guard(cfg, src, behind=a.lane in ("gates", "land"))   # behind main matters for gates and landing only (item 5)
    if why and not a.allow_unmerged: raise Err(f"refused: {src} {why} (--allow-unmerged to run anyway)")
    jid = f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)[:40]}-{time.strftime('%m%d-%H%M%S')}-{os.getpid()}"
    if cfg.run.dry: say(job_script(cfg, hn, h, a, name, cmd, jid)); return
    if not h.get("local"): sync_wt(cfg, hn, h, src, name)
    rd = runs_dir(cfg, h)
    if h.get("local"): Path(rd).mkdir(parents=True, exist_ok=True)
    rc, o, e = hrun(h, f"mkdir -p {qp(rd)} && cat > {qp(f'{rd}/{jid}.sh')} && {ensure_lanes(h, cfg)}", inp=job_script(cfg, hn, h, a, name, cmd, jid))
    if not rc: rc, o, e = write_cap(cfg, hn, h)
    if rc: raise Err(f"writing the job to {hn} failed: {(e or o).strip()[-120:]}")
    label = f"unit:{unit}" if unit else f"run:{name}"   # unit:<name>: check reports it once that unit is closed (M3)
    rc, o, e = hrun(h, f"{pueue(h)} add -g {shlex.quote(a.lane)} -l {shlex.quote(label)} -w {qp(rd)} --print-task-id"
                       + (f" -o {int(a.priority)}" if a.priority else (" -o 10" if a.lane == "gates" else "")) + f" -- {pq('bash ' + qp(f'{rd}/{jid}.sh'))}")
    if rc: raise Err(f"pueue add on {hn} failed: {(e or o).strip()[-120:]}")
    tid = o.strip()
    if not cfg.run.dry:   # the tree this job uses, for janus busy (row 53): one line per job, dropped once the job has ended
        bd_ = cfg.f("runs") / "busy"; bd_.mkdir(parents=True, exist_ok=True); (bd_ / jid).write_text(f"{hn}:{tid}\t{src}\n")
        js = cfg.f("runs") / "jobs"; js.mkdir(parents=True, exist_ok=True)   # what it ran, so a thrash stop can re-queue it (item 3)
        (js / f"{jid}.json").write_text(json.dumps({"host": hn, "task": tid, "lane": a.lane, "unit": unit, "wt": src, "cmd": cmd,
                                                     "subdir": a.subdir, "usb": bool(a.usb), "priority": a.priority, "at": int(now())}))
    wrap = os.environ.get("JANUS_WRAP")   # queued from inside another janus job or gate (a wrapper): record it, so cancel follows (row 49)
    if wrap and re.fullmatch(r"[\w.-]+", wrap):
        wd = cfg.f("runs") / "wrap"; wd.mkdir(parents=True, exist_ok=True)
        with open(wd / wrap, "a") as f: f.write(f"{hn}:{tid} {jid}\n")
    print(f"janus: queued {hn}:{tid} ({a.lane}, {label}) job {jid}; collect with: janus collect {hn}:{tid}", file=sys.stderr)
    if a.no_wait: say(f"{hn}:{tid}"); return
    return finish_job(cfg, hn, h, tid, jid)

def tree_guard(cfg, wt, behind=True):
    """Why a tree must not be built or gated (issues 33, 41): a merge in progress, conflict markers in changed files, or (behind:
    the gates and land lanes and janus gate only, never an agent's own jobs; janus-fixes9 item 5) more than [landing] max_behind
    (default 10) commits behind the landing branch. None when fine or not a git tree."""
    g = lambda *x: sh(["git", "-C", str(wt), *x])
    if g("rev-parse", "--git-dir")[0]: return None
    if g("rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0: return "has a merge in progress (MERGE_HEAD)"
    rc, o, e = g("diff", "--name-only", "--diff-filter=U")
    if o.strip(): return f"has unmerged files: {' '.join(o.split()[:3])}"
    rc, o, e = g("grep", "-n", "-I", "-E", "^(<<<<<<<|>>>>>>>) ", "--", ".")
    if rc == 0 and o.strip(): return f"has conflict markers ({o.splitlines()[0][:80]})"
    if not behind: return None
    br = cfg.c.get("landing", {}).get("branch") or "main"; mx = cfg.c.get("landing", {}).get("max_behind", 10)
    rc, o, e = g("rev-list", "--count", f"HEAD..{br}")
    if rc == 0 and o.strip().isdigit() and int(o) > mx: return f"is {o.strip()} commits behind {br} (max {mx}): merge {br} first"
    return None

BUSY_PATTERNS = ["mutants"]   # [busy] patterns default: tools that edit the tree in place while they run (mutation testing)

def busy_reasons(cfg, wt, hs=None, ps=None):
    """Why the tree wt is in use (row 53): a janus job queued or running on it (any host and lane; a job whose host does not
    answer counts as busy), an open unit's own jobs when the unit's cwd is wt (a remote agent's lane jobs on its copy), or a
    local process matching [busy] patterns (default: mutation testing, which edits sources in place) that runs in or names
    the tree. A periodic WIP snapshot must not commit while any holds."""
    names = {str(Path(wt).absolute()), str(Path(wt).resolve())}; wt = Path(wt).resolve(); why = []
    bdir = cfg.f("runs") / "busy"; recs = []
    for f in sorted(bdir.glob("*")) if bdir.is_dir() else []:
        try: ref, src = f.read_text().strip().split("\t", 1)
        except ValueError: continue
        recs.append((f, ref, Path(src)))
    hosts = {r.split(":")[0] for _, r, _ in recs}
    units = []
    try: units = Beads(cfg).units()
    except Err: pass
    mine = [u for u in units if u.get("cwd") and Path(u["cwd"]).resolve() == wt]
    if hs is None:   # a unit's jobs may run on any host; job records name theirs
        hs = probe_all(cfg) if mine else {n: probe(n, cfg.hosts[n], cfg=cfg) for n in hosts if n in cfg.hosts}
    for f, ref, src in recs:
        hn, _, tid = ref.partition(":"); p = hs.get(hn) or {}
        t = ((p.get("pueue") or {}).get("tasks") or {}).get(tid)
        if p.get("ok") and (t is None or task_rc(t) is not None):
            if not cfg.run.dry: f.unlink(missing_ok=True)   # ended or removed: the record goes
            continue
        if src.resolve() == wt or wt in src.resolve().parents:
            st = t["status"] if p.get("ok") else None; st = st if isinstance(st, str) else next(iter(st or {}), "")
            why.append(f"job {ref} ({f.name}) " + (f"is {st.lower()}" if p.get("ok") else f"on unreachable {hn}: assumed running"))
    for u in mine:
        why += [f"unit {u['name']}'s job {j}" for j in unit_jobs(hs, u["name"])]
    pats = cfg.c.get("busy", {}).get("patterns", BUSY_PATTERNS)
    if pats:
        rx = re.compile("|".join(re.escape(x) for x in pats))
        for l in (ps if ps is not None else sh(["ps", "-axo", "pid=,command="])[1]).splitlines():
            pid, _, c = l.strip().partition(" ")
            if not pid.isdigit() or not rx.search(c) or int(pid) == os.getpid(): continue
            cwd = proc_cwd(int(pid))
            if any(n in c for n in names) or (cwd and (cwd == wt or wt in cwd.parents)):
                why.append(f"process {pid} ({c.strip()[:80]})")
    return why

def proc_cwd(pid):
    """A local process's working directory, or None."""
    try: return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
    except OSError: pass
    rc, o, e = sh(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"], timeout=10)
    n = next((l[1:] for l in o.splitlines() if l.startswith("n")), None)
    return Path(n).resolve() if rc == 0 and n else None

def cmd_busy(cfg, a):
    """janus busy <wt>: exit 0 (and say why) while a job or a mutation run uses the tree, 1 when it is free (row 53)."""
    cfg.run.ro = True   # read-only on every host (the probe leaves the overload tracker alone)
    why = busy_reasons(cfg, a.wt)
    return (0, [f"busy: {w}" for w in why]) if why else (1, [f"free: no janus job, unit job or [busy] process uses {Path(a.wt).resolve()}"])

def cmd_cancel(cfg, a):
    """Cancel a janus job (<host>:<task>) that outlived its intent (issue 36): see cancel_job."""
    for l in cancel_job(cfg, a.job, force=a.force): say(l)

WRAP_RX = re.compile(r"\bJANUS_WRAP=([\w.-]+)")

def wrap_token(t):
    """The token under which a task records the jobs it queues: its job id (job scripts), or JANUS_WRAP=<token> (gates)."""
    m = WRAP_RX.search((t or {}).get("command", "")); return m.group(1) if m else job_of(t)

def wrapped_jobs(cfg, tok):
    try: return [l.split()[0] for l in (cfg.f("runs") / "wrap" / tok).read_text().splitlines() if l.strip()]
    except OSError: return []

def stop_job_cmd(h, jid):
    """Shell that stops what a job left behind, by name: its podman container (runner podman) or its systemd scope (Linux)."""
    if not jid or h.get("local"): return ""
    if h.get("runner") == "podman": return f"podman stop -t 5 janus-run-{jid} >/dev/null 2>&1; "
    return f"systemctl --user stop janus-{jid}.scope >/dev/null 2>&1; "

def cancel_job(cfg, ref, force=False, depth=0):
    """Cancel job <host>:<task>: its files are saved here first (runs/<jid>.*; refused when that fails, unless force: nothing is
    removed before it is collected), then pueue kill and remove, then its container or scope is stopped by name, and every job
    it queued itself (a wrapper: a gate or a job whose script ran janus run; row 49) is cancelled the same way."""
    hn, _, tid = ref.partition(":")
    if hn not in cfg.hosts or not tid.isdigit(): raise Err("expected <host>:<task id>")
    h = cfg.hosts[hn]; t = task_status(h, tid)
    if t is None: raise Err(f"no task {tid} on {hn}")
    if t.get("group") not in LANES and not str(t.get("label", "")).startswith(("unit:", "run:", "gate:")): raise Err(f"{hn}:{tid} is not a janus lane job ({t.get('label')}); stop units with janus stop")
    jid = job_of(t); tok = wrap_token(t); inner = wrapped_jobs(cfg, tok) if tok else []; out = []
    if cfg.run.dry:
        out.append(f"+ save {hn}:{tid}'s files; {stop_job_cmd(h, jid)}{pueue(h)} kill {tid}; remove" + (f"; then follow its jobs {', '.join(inner)}" if inner else ""))
    else:
        if jid and not h.get("local"):
            loc = cfg.f("runs"); loc.mkdir(exist_ok=True)
            r = sh(["rsync", "-a", "-e", " ".join(SSH), f"{h['ssh']}:{runs_dir(cfg, h)}/{jid}.*", f"{loc}/"], timeout=300)
            if r[0] and not force: raise Err(f"refused: {hn}:{tid}'s files could not be saved here ({r[2].strip()[-80:]}); --force cancels anyway")
        elif not jid: save_task_log(cfg, h, {"name": f"_cancelled/{hn}-{tid}"}, tid)
        hrun(h, f"{pueue(h)} kill {tid} 2>/dev/null; {stop_job_cmd(h, jid)}sleep 2; {pueue(h)} remove {tid} 2>/dev/null; true", timeout=60)
        out.append(f"cancelled {hn}:{tid} ({t.get('label')}); its files are in {cfg.f('runs') if jid and not h.get('local') else runs_dir(cfg, h) if jid else cfg.p('out', 'out') / '_cancelled'}")
    for r in inner:
        if depth > 3: break
        try:
            st = task_rc(task_status(cfg.hosts[r.split(':')[0]], r.split(':')[1])) if r.split(":")[0] in cfg.hosts else 0
            if st is None: out += cancel_job(cfg, r, force, depth + 1)
        except Err as e: out.append(f"{r}: {e}")
    return out

def thrash_cfg(h): return (float(h.get("thrash_psi", 40)), float(h.get("thrash_min", 5)))

def host_psi(p):
    v = p.get("cap_psi") or p.get("psi")
    return fl(v) if v not in (None, "") else None

def thrash_track(cfg, hs):
    """{host: (psi, minutes over thrash_psi)} for shared hosts; the over-since times are kept in thrash.json (writer only)."""
    st = cfg.jget("thrash.json"); out = {}
    for n, h in cfg.hosts.items():
        if not h.get("shared") or not (hs.get(n) or {}).get("ok"): st.pop(n, None); continue
        psi = host_psi(hs[n]); lim, _ = thrash_cfg(h)
        if psi is None or psi <= lim: st.pop(n, None); out[n] = (psi, 0.0); continue
        st.setdefault(n, int(now())); out[n] = (psi, (now() - st[n]) / 60)
    cfg.jput("thrash.json", st)
    return out

def own_admitted_jobs(cfg, hs, n):
    """[(ref, jid, spec, started)] this project's jobs running past admission on host n, newest first."""
    jd = cfg.f("runs") / "jobs"; p = hs.get(n) or {}; r = []
    for tid, t in ((p.get("pueue") or {}).get("tasks") or {}).items():
        jid = job_of(t); f = jd / f"{jid}.json" if jid else None
        if not f or not f.is_file() or job_state(hs, f"{n}:{tid}")[0] != "running": continue
        try: spec = json.loads(f.read_text())
        except ValueError: continue
        if spec.get("host") != n or str(spec.get("task")) != str(tid): continue   # recorded by this project for this very task
        info = (t.get("status") or {}).get("Running", {}) if isinstance(t.get("status"), dict) else {}
        try: t0 = calendar.timegm(time.strptime((info.get("start") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError: t0 = int(spec.get("at", 0))
        r.append((f"{n}:{tid}", jid, spec, t0))
    return sorted(r, key=lambda x: x[3], reverse=True)

def requeue_host(cfg, hs, units, n, spec):
    """Another host where the job may run now (reachable, not thrashing, room by admission; the land lane stays local), else n."""
    for m, h in cfg.hosts.items():
        p = hs.get(m) or {}
        if m == n or not p.get("ok") or (spec.get("lane") == "land" and not h.get("local")): continue
        if h.get("shared") and (host_psi(p) or 0) > thrash_cfg(h)[0]: continue
        if host_cap(cfg, m, p, units)["free"] > 0 and not host_cap(cfg, m, p, units)["pause"]: return m
    return n

def tend_thrash(cfg, hs, units, log, dry=False):
    """Lines saying what was (or, dry, would be) done on each shared host over its thrash limit."""
    out = []
    for n, (psi, mins) in thrash_track(cfg, hs).items():
        lim, need = thrash_cfg(cfg.hosts[n])
        if psi is None or psi <= lim or mins < need:
            if dry: out.append(f"{n}: memory PSI {psi if psi is not None else '-'} (limit {lim:g} for {need:g} min): {'over for ' + format(mins, '.0f') + ' min' if mins else 'fine'}")
            continue
        jobs = own_admitted_jobs(cfg, hs, n)
        if not jobs: out.append(f"{n}: memory PSI {psi} over {lim:g} for {mins:.0f} min, but no job of this project runs there: nothing to stop"); continue
        ref, jid, spec, _ = jobs[0]; to = requeue_host(cfg, hs, units, n, spec)
        msg = f"{n} thrashing (memory PSI {psi} over {lim:g} for {mins:.0f} min): stop {ref} ({jid}, newest of {len(jobs)}), re-queue on {to}"
        if dry: out.append("would " + msg); continue
        try:
            done = cancel_job(cfg, ref, force=True)
            args = argparse.Namespace(host=to, lane=spec.get("lane", "jobs"), unit=spec.get("unit"), wt=spec["wt"], subdir=spec.get("subdir"),
                                      usb=spec.get("usb", False), priority=spec.get("priority") or 0, no_wait=True, allow_unmerged=True, command=[spec["cmd"]])
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf): cmd_run(cfg, args)
            new = buf.getvalue().strip().splitlines()[-1:] or ["?"]
            line = f"{msg}: {'; '.join(done)}; now {new[0]}"
            rec = cfg.jget("thrash-acts.json"); rec[str(int(now()))] = line; cfg.jput("thrash-acts.json", dict(list(rec.items())[-50:]))
            if spec.get("unit"): board(cfg, "direct", spec["unit"], f"your job {ref} was stopped on {n} (host thrashing) and re-queued as {new[0]}: wait on that id")
            out.append(line)
        except Err as e: out.append(f"{msg}: failed: {e}")
        log(out[-1])
    return out

def cmd_thrash(cfg, a):
    """Shared hosts' memory pressure and what the watcher does (--dry-run: says what it would do, changes nothing)."""
    if cfg.run.dry: cfg.run.ro = True
    hs = probe_all(cfg); units = Beads(cfg).units()
    for u in units: u["state"] = unit_state(u, hs, {})
    if not any(h.get("shared") for h in cfg.hosts.values()): say("no host has shared = true"); return
    for l in tend_thrash(cfg, hs, units, lambda m: None, dry=cfg.run.dry or cfg.run.ro) or ["nothing to do"]: say(l)

def orphan_jobs(cfg, hs, units):
    """Jobs whose owner is gone (item 3): [(ref, why)]. A lane job of a closed unit, or of an open unit whose agent ended and is
    not parked; a job a wrapper queued (row 49) whose wrapper is no longer queued or running here."""
    names = {u["name"]: u for u in units}; out = []
    for hn, p in hs.items():
        for tid, t in ((p.get("pueue") or {}).get("tasks") or {}).items():
            lab = t.get("label") or ""
            if task_rc(t) is not None or not lab.startswith("unit:"): continue
            u = names.get(lab[5:])
            if not u: out.append((f"{hn}:{tid}", f"its unit {lab[5:]} is closed"))
            elif agent_ended(u) and not u.get("_park") and not u.get("parked"): out.append((f"{hn}:{tid}", f"its unit {u['name']}'s agent ended ({u['state']}) and is not parked"))
    live = {wrap_token(t) for p in hs.values() for t in ((p.get("pueue") or {}).get("tasks") or {}).values() if task_rc(t) is None}
    try: recs = list((cfg.f("runs") / "wrap").iterdir())
    except OSError: recs = []
    for f in recs:
        if f.name in live: continue
        for r in wrapped_jobs(cfg, f.name):
            st = job_state(hs, r)[0]
            if st in ("queued", "admission", "running") and r not in {x for x, _ in out}: out.append((r, f"its wrapper {f.name} has ended"))
            # records of wrappers that ended long ago and whose jobs are done are cleared by the watcher
    return out

def job_of(t):
    m = re.search(r"/runs/([^/ ]+)\.sh", (t or {}).get("command", ""))
    return m.group(1) if m else None

def collect_job(cfg, ref):
    hn, _, tid = ref.partition(":")
    if hn not in cfg.hosts or not tid.isdigit(): raise Err("expected <host>:<task id>")
    h = cfg.hosts[hn]; t = task_status(h, tid)
    if t is None: raise Err(f"no task {tid} on {hn} (unreachable, or cleaned)")
    jid = job_of(t); rc = task_rc(t)
    if rc is None: say(f"{hn}:{tid} is {next(iter(t['status'])) if isinstance(t['status'], dict) else t['status']}, not finished"); return 3
    local = cfg.f("runs"); local.mkdir(exist_ok=True)
    if jid and not h.get("local"):
        r = sh(["rsync", "-a", "-e", " ".join(SSH), f"{h['ssh']}:{runs_dir(cfg, h)}/{jid}.*", f"{local}/"], timeout=300)
        if r[0]: raise Err(f"copy from {hn} failed: {r[2].strip()[-120:]}")
    if jid and (local / f"{jid}.log").exists(): sys.stdout.write((local / f"{jid}.log").read_text())
    try: rc = int((local / f"{jid}.rc").read_text().strip())
    except Exception: pass
    say(f"janus: {hn}:{tid} ({t.get('label')}) exit {rc}; files in {local}/{jid}.*")
    return rc
