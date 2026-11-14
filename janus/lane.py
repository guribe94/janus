"""The shared core of queueing a job in a host's pueue lane (stdlib only, Python 3.8+: it is also shipped to a remote host's
<root>/bin and run there as the unit's `lane` command, so a remote agent's job passes through the same code as `janus run`).

qp/pq (shell quoting), the whole-suite guard (broad_why), queue_job (write the script, make the groups, `pueue add`) and
main (the remote runner: <root>/run/<unit>.lane execs `python3 <root>/bin/lane.py <root>/run/<unit>.lane.json <args>`).
The host's own runs/cap.sh stays the authority on class, reservations and admission; this file only calls it."""
import json, os, re, shlex, subprocess, sys, time

def pq(cmd):
    """The command part of `pueue add -- <command>`, as one word of the caller's shell. pueue joins its arguments with spaces and
    runs the result with sh -c, so a command must reach it already shell-quoted: cmd is a list of words (quoted here) or shell
    text, and the whole is quoted once more for the caller's shell. Never `-- {words}` unquoted (a path with a space, a quote
    or a backtick broke a launch, and is an injection)."""
    return shlex.quote(cmd if isinstance(cmd, str) else shlex.join(cmd))

def qp(p):
    """A path as one shell word: shlex-quoted, except that a leading $HOME/ or ~/ (a remote root or worktree from janus.toml,
    default $HOME/janus) still expands."""
    p = str(p)
    for pre in ("$HOME/", "~/", "${HOME}/"):
        if p.startswith(pre): return '"$HOME"/' + (shlex.quote(p[len(pre):]) if p[len(pre):] else "")
    return shlex.quote(p)

# A whole-suite run needs --broad <reason> (owner rule after two broad runs held 7 cores for 3 h while short jobs waited 4 h): it
# then runs at the lowest priority (nice 19, pueue priority -5, counted double in the short-first order). Broad: the command matches
# [capacity] broad_patterns (default below), or its job class ran a median of [capacity] broad_min minutes or more on that host.
BROAD_PATTERNS = [r"\bcargo\s+(test|nextest\s+run|clippy|build|check)\b.*\s--(workspace|all)(?![-\w])", r"(^|[;&|]\s*)(\S*/)?pytest\s*($|[;&|])"]
PKG_RX = re.compile(r"(^|\s)(?:-p\s*|--package[=\s])\S")   # a cargo command that names one package (-p X, --package=X)
WS_RX = re.compile(r"--(workspace|all)(?![-\w])")          # a whole-workspace/all-packages flag (never --all-targets/--all-features)
CARGO_RX = re.compile(r"(^|\s)cargo\s+(test|nextest\s+run|clippy|build|check)\b")
CARGO_BROAD = BROAD_PATTERNS[0]   # the default cargo whole-suite pattern: the one-package exception applies to IT only (finding 4)

def one_package_cargo(seg):
    """True when seg is a cargo test/build/check run that names ONE package (`-p X`/`--package=X`) and has no
    `--workspace`/`--all`: only then is it not a whole-suite run (item 25b). `--all-targets`/`--all-features` do not count as
    `--all` (finding 1), and `--workspace` wins over `-p` (cargo runs the whole workspace then)."""
    return bool(CARGO_RX.search(seg)) and bool(PKG_RX.search(seg)) and not WS_RX.search(seg)

def _segments(c):
    """The commands in c, each a list of words: quoting and backslash escapes are honoured, so a ; or | inside a quoted
    argument (an ack or a commit message) never starts a new command (finding 4), and the `&` of a redirect (`2>&1`, `&>log`,
    `<&3`) and the `|` of a clobber redirect (`>|`) are part of their word, never a separator (finding 4, fix-install6, finding E)."""
    segs, cur, w, q, esc = [], [], "", None, False
    for i, ch in enumerate(c):
        if esc: w += ch; esc = False; continue
        if q:
            if ch == q: q = None
            elif ch == "\\" and q == '"': esc = True
            else: w += ch
        elif ch in "'\"": q = ch
        elif ch == "\\": esc = True
        elif ch in ";&|()\n":
            if ch in "()" and not w and cur:
                continue   # a paren inside a command (an unquoted ps rendering of a quoted message): not a subshell, not a separator
            if ch == "&" and (i > 0 and c[i - 1] in "<>" or i + 1 < len(c) and c[i + 1] == ">"):
                w += ch   # the `&` of a redirect (`2>&1`, `&>log`, `<&3`), not a separator
            elif ch == "|" and i > 0 and c[i - 1] == ">":
                w += ch   # the `|` of a clobber redirect (`>|`), not a pipe (finding E)
            else:
                if w: cur.append(w); w = ""
                if cur: segs.append(cur); cur = []
        elif ch.isspace():
            if w: cur.append(w); w = ""
        else: w += ch
    if w: cur.append(w)
    if cur: segs.append(cur)
    return segs

def broad_why(cmd, patterns, broad_min, cls=None, secs=None):
    """Why cmd is a whole-suite run, or None. Item 25(b): a cargo command that names a package (`-p X`, `--package=X`) covers one
    package, not the whole workspace, so the DEFAULT cargo pattern does not apply to it. Each command is judged on its own, split
    by the quote-aware `_segments`; a project's own cargo-like pattern keeps its meaning. cls/secs: the host's job class of cmd and
    its median run time there (from the host's own cap.sh): a class that ran broad_min minutes or more is whole-suite too."""
    segs = [" ".join(w) for w in _segments(cmd)]
    for rx in patterns:
        for seg in segs:
            if re.search(rx, seg) and not (rx == CARGO_BROAD and one_package_cargo(seg)):
                return f"it matches the broad pattern {rx!r}"
    if secs and secs >= broad_min * 60: return f"its job class {cls} ran a median of {secs // 60} min here (broad_min {broad_min:g})"
    return None

def queue_job(run, pueue, ensure, rd, jid, script, lane, label, priority=0, low=False, clean=""):
    """Write the job script to <rd>/<jid>.sh on the host, make its pueue groups (ensure), and queue it: (rc, task id or error).
    run(shell, inp=None) -> (rc, out, err) executes on the host (ssh for `janus run`, a local shell for the remote runner);
    clean: a command prefix for the add (the remote runner's allow-listed environment, so a task never stores a key)."""
    sp = f"{rd}/{jid}.sh"
    rc, o, e = run(f"mkdir -p {qp(rd)} && cat > {qp(sp)} && {ensure}", script)
    if rc: return rc, f"writing the job failed: {(e or o).strip()[-120:]}"
    rc, o, e = run(f"{clean}{pueue} add -g {shlex.quote(lane)} -l {shlex.quote(label)} -w {qp(rd)} --print-task-id"
                   + (f" -o={int(priority)}" if priority else " -o=-5" if low else "") + f" -- {pq('bash ' + qp(sp))}", None)
    if rc: return rc, f"pueue add failed: {(e or o).strip()[-120:]}"
    return 0, o.strip().splitlines()[-1] if o.strip() else ""

def local_run(script, inp=None):
    r = subprocess.run(["sh", "-c", script], input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return r.returncode, r.stdout, r.stderr

def main(argv):
    """The remote unit's `lane [--detach] [--broad REASON] <command>`: guard, record, queue in this host's jobs lane through the
    same functions `janus run` uses; wait for the exit code unless --detach. argv[0]: the unit's lane.json (written at launch:
    host, unit, label, runs dir, pueue, group setup, the job script with __JID__, its broad twin, guard settings)."""
    with open(argv[0]) as f: c = json.load(f)
    a = argv[1:]; det, broad = False, None
    while a and a[0] in ("--detach", "--broad"):
        if a[0] == "--detach": det = True; a = a[1:]
        elif len(a) > 1: broad = a[1]; a = a[2:]
        else: print("lane: --broad needs a reason", file=sys.stderr); return 2
    if not a: print("usage: lane [--detach] [--broad REASON] <command>", file=sys.stderr); return 2
    cmd = a[0] if len(a) == 1 else shlex.join(a); rd = os.path.expandvars(c["rd"]); unit = c["unit"]
    if not broad:   # the whole-suite guard of `janus run`; the class and its median come from this host's own cap.sh
        cls = secs = None
        r = subprocess.run(["sh", "-c", '[ -f "$1/cap.sh" ] || exit 0; RD=$1; . "$1/cap.sh"; k=$(cap_class "$2"); echo "$k"; cap_secs "$k"', "sh", rd, cmd],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout.split()
        if r: cls = r[0]; secs = int(r[1]) if len(r) > 1 and r[1].isdigit() else None
        why = broad_why(cmd, c["patterns"], c["broad_min"], cls, secs)
        if why:
            print(f"lane: refused: a whole-suite run ({why}) needs --broad <reason>: `lane --broad '<reason>' <command>` runs it at low priority; or narrow it (one package, a test filter)", file=sys.stderr)
            return 2
    jid = f"{unit}-{time.strftime('%m%d-%H%M%S')}-{os.getpid()}"
    os.makedirs(rd, exist_ok=True)
    n = 1
    while os.path.exists(f"{rd}/{jid}.cmd"): n += 1; jid = f"{unit}-{time.strftime('%m%d-%H%M%S')}-{os.getpid()}-{n}"   # two lanes in one second never share files
    with open(f"{rd}/{jid}.cmd", "w") as f: f.write(cmd + "\n")
    script = c["script_broad" if broad else "script"].replace("__JID__", jid)
    clean = 'env -i HOME="$HOME" USER="$USER" ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} LANG="${LANG:-C.UTF-8}" '
    rc, tid = queue_job(local_run, c["pueue"], c["ensure"], rd, jid, script, "jobs", c["label"], low=bool(broad), clean=clean)
    if rc: print(f"lane: {tid}", file=sys.stderr); return 125
    # the host keeps the job's record beside its files (janus collect brings <jid>.* back), as `janus run` keeps one in the project
    with open(f"{rd}/{jid}.json", "w") as f:
        json.dump({"host": c["host"], "task": tid, "lane": "jobs", "unit": unit, "wt": c["wt"], "cmd": cmd, "pinned": True, "broad": broad, "at": int(time.time())}, f)
    print(f"lane: queued {c['host']}:{tid} ({jid})" + (f", broad ({broad}): low priority" if broad else ""), file=sys.stderr)
    if det: print(f"{c['host']}:{tid}"); return 0
    subprocess.call(["sh", "-c", f"{c['pueue']} wait -q {int(tid)}"])
    try:
        with open(f"{rd}/{jid}.log") as f: sys.stdout.write(f.read())
    except OSError: pass
    try:
        with open(f"{rd}/{jid}.rc") as f: return int(f.read().strip())
    except (OSError, ValueError): return 255

if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
