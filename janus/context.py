"""Config, state, processes and the low-level services every other module builds on (section 7, Step 1).

Err/say/now/ts, find_root/Cfg, clean_env/sh/hrun/X, the registered session's process identity, the shared quoting and
small helpers (pq/qp/secs/findkey/slug), Locked and atomic_write, the HANDOFF.md path and refuse_agent (what only the
orchestrator may do). The one project-root resolver (find_root) lives here: every part of janus goes through it. No module imports janus or a higher layer. Run is what one
invocation is (dry, read-only, what it read once), carried as cfg.run (section 7, seam 2)."""
import contextlib, json, os, re, shlex, sys, time, tomllib
from dataclasses import dataclass
from pathlib import Path
from janus.lane import pq, qp   # shell quoting: one copy, shared with the remote lane runner
from janus.platform import herdr, ps_table, run, secs   # re-exported: the OS/tool adapter (owner direction 2026-11-11)

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]

MUX_REFUSED = "mux_client_request_session: session request failed: Session open refused by peer"

EXCL = ["--exclude", ".git", "--exclude", "target", "--exclude", ".venv", "--exclude", "node_modules"]

WAKE = "Wake ({reason}): if the owner waits for an answer, answer first; then in {root} run janus status and act on what changed."
# after a reset or a brand-new session (it holds no state): the memory, then the handoff, both by ABSOLUTE path -- the session's
# cwd may be outside the project root.
WAKE_CLEAR = "read {memory} (binding instructions) and {handoff}, then " + WAKE

HANDOFF0 = """# HANDOFF
## Objective
## Decisions
## Blockers
## Next actions
"""

@dataclass
class Run:
    """One invocation of janus (section 7, seam 2), carried as cfg.run: no module global, no attribute set on the side.

    dry: --dry-run, print what would change and change nothing (main sets it). ro: read-only, a check or status by anyone but
    the registered orchestrator or the watcher changes nothing (run_check sets it; jput/touch then write nothing). all_units
    and rates: what this invocation read once (every unit ever recorded; learned $ per weighted token), None until read."""
    dry: bool = False
    ro: bool = False
    all_units: dict | None = None
    rates: dict | None = None

class Err(Exception): pass

class LockLost(Err):
    """The writer lock could not be taken again after a command let it go for its remote part: the work is done, its state is not written."""

def now(): return time.time()

def ts(t=None): return time.strftime("%Y-%m-%d %H:%M", time.localtime(t or now()))

def say(*a): print(*a, flush=True)

CONF = Path(".janus") / "janus.toml"

def find_root(start=None, explicit=None):
    """The project root: the nearest ancestor of start (default the cwd) that holds .janus/janus.toml. A worker outside the
    root names its project explicitly: explicit, else $JANUS_ROOT (set on everything janus starts). The workspace root need
    not be a Git root. None when there is none."""
    r = explicit or (None if start else os.environ.get("JANUS_ROOT"))
    if r: return Path(r).resolve() if (Path(r) / CONF).is_file() else None
    p = Path(start or os.getcwd()).resolve()
    return next((d for d in [p, *p.parents] if (d / CONF).is_file()), None)

def old_layout(start=None):
    """The nearest ancestor with a pre-.janus janus.toml at its top (the one layout `janus migrate` moves), or None."""
    p = Path(start or os.getcwd()).resolve()
    return next((d for d in [p, *p.parents] if (d / "janus.toml").is_file() and not (d / CONF).is_file()), None)

class Cfg:
    def __init__(self, root, run=None, conf=None):
        """conf: another config file, for `janus migrate` only (it reads the config that is still at the project's top)."""
        self.root, self.run = root, run or Run()
        self.c = tomllib.loads((conf or root / CONF).read_text())
        pr = self.c.get("project", {})
        self.name = pr.get("name", root.name)
        self.state = root / (pr.get("state", ".janus") if conf else ".janus")
        self.beads = root / pr.get("beads", ".")
        self.silent = pr.get("silent_min", 30) * 60
        self.reset_tokens = pr.get("reset_tokens", 120000)   # below the runtime's own (lossy) auto-compaction point
        self.hosts = self.c.get("hosts", {}); self.prov = self.c.get("providers", {})
        self.models = self.c.get("models", {}); self.route = self.c.get("routing", {})
        self.paths = self.c.get("paths", {}); self.caps = self.c.get("caps", {})
        self.capc = self.c.get("capacity", {})
        for h in self.hosts.values():   # each host's runs dir: lane job scripts, logs, reservations, history (see cap_lib)
            h["_rd"] = str(self.state / "runs") if h.get("local") else f"{h.get('root', '/tmp')}/runs"
    def p(self, k, d=""):
        """A project path: [paths] k (relative to the project root) when set, else d under .janus (board, briefs, out, worktrees)."""
        v = self.paths.get(k)
        return self.root / v if v else self.state / d
    def f(self, n): return self.state / n
    def jget(self, n, d=None):
        try: return json.loads(self.f(n).read_text())
        except Exception: return {} if d is None else d
    def jput(self, n, v):   # a read-only run changes nothing; temp file + rename: a kill mid-write keeps the old value
        if not self.run.ro: atomic_write(self.f(n), json.dumps(v))
    def writer(self, wait=300):
        """The state folder's one writer lock (.janus/write.lock; panel problem 2): the watcher's pass, a writer check and the
        orchestrator's state-changing commands hold it, so two never interleave read-modify-write of acks, open problems or
        unit records. Readers do not take it. Re-entrant within a process (the watcher's pass runs the check)."""
        return writer_lock(self.f("write.lock"), wait)
    def unlocked(self):
        """The writer lock let go for the body (a command's slow remote part) and taken again after it, whatever depth the process
        holds it at; a no-op where it is not held. The caller records what it is doing on the unit record first (busy)."""
        return writer_unlocked(self.f("write.lock"))
    def command_writer(self):
        """The lock a state-changing command takes: none for a dry run or an agent's call (the orchestrator's commands only)."""
        return self.writer() if not self.run.dry and os.environ.get("JANUS_ROLE") != "agent" else contextlib.nullcontext()
    def touch(self, n):
        if not self.run.ro: self.f(n).touch()
    def mtime(self, n):
        try: return self.f(n).stat().st_mtime
        except OSError: return 0

# Every process janus queues (pueue add) or starts on the Mac gets this allow-listed environment plus explicit JANUS_* values,
# never the caller's: pueue stores a task's whole environment in its state file, so a caller's ANTHROPIC_API_KEY, messaging
# token or HERDR_PANE_ID would be written to disk (audit H1, M2).
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "SSH_AUTH_SOCK", "TZ", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME")

def clean_env(extra=None):
    e = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    # /usr/sbin and /sbin always: launchd gives the watcher a PATH without them, and the macOS probe's sysctl lives there
    # (issue 3: the probe read 0.0 GB and an empty load under the watcher)
    e["PATH"] = os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:/usr/local/bin:" + e.get("PATH", "/usr/bin:/bin") + ":/usr/bin:/bin:/usr/sbin:/sbin"
    return {**e, **{k: str(v) for k, v in (extra or {}).items()}}

def sh(cmd, cwd=None, inp=None, timeout=25, env=None, clean=False):
    """Run a command. clean=True: an allow-listed environment plus env (see clean_env), never the caller's."""
    return run(cmd, cwd=cwd, inp=inp, timeout=timeout, env=clean_env(env) if clean else ({**os.environ, **env} if env else None))

def hrun(h, script, timeout=25, inp=None, env=None):
    """Run a shell script on host h (local or over ssh). Locally it gets a clean environment (clean_env) plus env; over ssh only
    the remote login environment (ssh forwards no variables here), with env exported in the script."""
    if h.get("local"): return sh(["sh", "-c", script], timeout=timeout, inp=inp, env=env, clean=True)
    pre = "".join(f"export {k}={shlex.quote(str(v))}; " for k, v in (env or {}).items())
    r = sh([*SSH, h["ssh"], pre + script], timeout=timeout, inp=inp)
    # The shared master connection refused a new session: ssh sent nothing to run, so one retry without the master cannot repeat a
    # command. Any other 255 may have run it: never retried.
    if r[0] == 255 and MUX_REFUSED in r[2]: r = sh([*SSH, "-o", "ControlPath=none", h["ssh"], pre + script], timeout=timeout, inp=inp)
    return r

def X(cfg, h, script, **k):
    """Run on a host, or print in dry-run."""
    if cfg.run.dry: say(f"+ [{h.get('ssh', 'local')}] " + "".join(f"{k}=... " for k in (k.get("env") or {})) + script); return 0, "", ""
    return hrun(h, script, **k)

def findkey(o, sub):
    if isinstance(o, dict):
        for k, v in o.items():
            if sub in k and isinstance(v, int): return v
            r = findkey(v, sub)
            if r: return r
    elif isinstance(o, list):
        for v in o:
            r = findkey(v, sub)
            if r: return r

def fl(x, d=0.0):
    try: return float(x)
    except Exception: return d

def ancestors(t, pid):
    out = []
    while pid in t and pid not in out and pid > 1: out.append(pid); pid = t[pid][0]
    return out

AGENT_RX = re.compile(r"(^|/)(claude|codex)(\s|$)|/(claude|codex)(-code)?/|\bnode\b.*\b(claude|codex)\b")

def session_proc(t=None):
    """(pid, start) of the Claude Code or Codex process this process runs under (hooks and Bash commands are its children)."""
    t = t or ps_table()
    for p in ancestors(t, os.getpid())[1:]:
        if AGENT_RX.search(t[p][3]): return p, t[p][2]
    p = int(os.environ.get("CLAUDE_PID", "0") or 0)
    return (p, t[p][2]) if p in t else (None, None)

def record_session_proc(cfg, t=None):
    p, st = session_proc(t)
    if p: atomic_write(cfg.f("session_pid"), f"{p}\t{st}\n")

# A pending registration (session = pending:<ts>[:<pane>], from janus register without an id, janus resume or the watcher's
# start) is claimable by a hook for CLAIM_WINDOW seconds; after it nothing can claim it, so it no longer blocks a start.
CLAIM_WINDOW = 600

def pending_age(sess):
    """Seconds since a pending registration was made, or None when sess is not pending."""
    parts = (sess or "").split(":", 2)
    return now() - int(parts[1]) if parts[0] == "pending" and len(parts) > 1 and parts[1].isdigit() else None

def session_alive(cfg, t=None):
    """True while the registered session's process still runs (same pid and start time). None when nothing is recorded."""
    try: p, st = cfg.f("session_pid").read_text().rstrip("\n").split("\t")
    except (OSError, ValueError): return None
    t = t or ps_table(); x = t.get(int(p))
    return bool(x and x[2] == st)

def is_orchestrator(cfg):
    """The caller is the registered orchestrator session: its session id is the registered one, or the registered session's
    process is among the caller's ancestors. Only then do check and status change state (audit M15)."""
    reg = cfg.f("session").read_text().strip() if cfg.f("session").exists() else ""
    if reg and reg in (os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CLAUDE_SESSION_ID"), os.environ.get("CODEX_THREAD_ID"), os.environ.get("JANUS_SESSION")):
        return True
    try: p, st = cfg.f("session_pid").read_text().rstrip("\n").split("\t")
    except (OSError, ValueError): return False
    t = ps_table(); return int(p) in ancestors(t, os.getpid()) and t[int(p)][2] == st

def projects():
    pl = Path.home() / ".config/janus/projects"
    return [Path(x) for x in (pl.read_text().split() if pl.exists() else []) if (Path(x) / CONF).is_file()]

def session_root(sid):
    """The project whose registered session is sid (hooks never depend on the session's current directory)."""
    for r in projects():
        f = r / ".janus" / "session"
        if f.exists() and f.read_text().strip() == sid: return r
    return None

NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")

class Locked:
    """The ledger's lock: `with Locked(path):` holds an exclusive fcntl lock on <path>.lock."""
    def __init__(self, path): self.p = str(path) + ".lock"
    def __enter__(self):
        import fcntl
        Path(self.p).parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.p, "a"); fcntl.lockf(self.f, fcntl.LOCK_EX); return self
    def __exit__(self, *e): self.f.close()

FIXTURE_MARK = "janus-test-fixture"

def atomic_write(p, text, mode=0o644):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try: tmp.write_text(text); tmp.chmod(mode); os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True); raise

_HELD = {}   # lock path -> [open file, depth]: the writer lock is re-entrant within one process

@contextlib.contextmanager
def writer_lock(path, wait=300):
    """An exclusive flock on path, waiting up to wait seconds (then Err), re-entrant within the process."""
    import fcntl
    k = str(path); held = _HELD.get(k)
    if held: held[1] += 1
    else:
        Path(k).parent.mkdir(parents=True, exist_ok=True); f = open(k, "a"); t0 = time.time()
        while True:
            try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.time() - t0 >= wait: f.close(); raise Err(f"another janus writer ({lock_owner(k) or 'the watcher or a command'}) holds {Path(k).name} for over {wait:g} s: try again")
                time.sleep(0.2)
        held = _HELD[k] = [f, 1, time.time()]
        with contextlib.suppress(OSError): Path(k + ".owner").write_text(f"{os.getpid()} {int(held[2])} janus {' '.join(sys.argv[1:3])}")
    try: yield
    finally:
        held[1] -= 1
        if not held[1]:
            s = time.time() - held[2]
            if s >= LOCK_SLOW:   # skill-fix57: who held the lock for long, in .janus/lock.log (short writers waited behind it)
                with contextlib.suppress(OSError), open(Path(k).parent / "lock.log", "a") as lf: lf.write(f"{ts()} {Path(k).name} held {s:.0f} s by janus {' '.join(sys.argv[1:3])} (pid {os.getpid()})\n")
            _HELD.pop(k, None); held[0].close()

@contextlib.contextmanager
def writer_unlocked(path, wait=300):
    """Let go of this process's hold on the writer lock for the body (the same entry stays, so the outer exits are unchanged),
    then wait up to wait seconds for it again (Err: the caller's work is done but its state not yet written)."""
    import fcntl
    k = str(path); held = _HELD.get(k)
    if not held: yield; return
    fcntl.flock(held[0], fcntl.LOCK_UN); held[0].close()
    try: yield
    finally:
        f = open(k, "a"); t0 = time.time()
        while True:
            try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.time() - t0 >= wait: f.close(); raise LockLost(f"another janus writer ({lock_owner(k) or 'the watcher or a command'}) holds {Path(k).name} for over {wait:g} s; the command's remote part is done but its state is not written (check: unit:<n>:interrupted)")
                time.sleep(0.2)
        held[0] = f; held[2] = time.time()
        with contextlib.suppress(OSError): Path(k + ".owner").write_text(f"{os.getpid()} {int(held[2])} janus {' '.join(sys.argv[1:3])}")

LOCK_SLOW = 30   # seconds a writer may hold the lock before its hold is logged

def lock_owner(k):
    """'janus watch (pid 7), for 212 s': the writer that holds lock k now, as it recorded itself; None when unknown."""
    try: pid, since, cmd = Path(str(k) + ".owner").read_text().split(" ", 2)
    except (OSError, ValueError): return None
    return f"{cmd.strip()} (pid {pid}), for {time.time() - int(since):.0f} s"

JANUS_SELF = Path(__file__).resolve().parent.parent / "janus.py"   # the installed release when run as `janus`: a queued task runs the version that queued it (this package sits next to it)

JANUS_MARK = "[janus] "   # every prompt janus itself sends starts with this, so UserPromptSubmit never files it as an instruction (M4)

def handoff_path(cfg): return cfg.f("HANDOFF.md")

def memory_path(cfg): return cfg.f("MEMORY.md")

def wake(cfg, reason):
    """The wake prompt: it names the project root by absolute path, so a session whose cwd is not the root still finds it."""
    return WAKE.format(reason=reason, root=cfg.root)

def wake_clear(cfg, reason):
    """The wake after a reset or a brand-new session: memory and handoff by absolute path, then `wake`."""
    return WAKE_CLEAR.format(reason=reason, memory=str(memory_path(cfg)), handoff=str(handoff_path(cfg)), root=cfg.root)

DIFF_X = " ".join(f"-x {x}" for x in ("target", ".venv", "node_modules", "__pycache__", ".git"))

def slug(t): return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:40]

def refuse_agent(what):
    if os.environ.get("JANUS_ROLE") == "agent": raise Err(f"refused: {what} is the orchestrator's (an agent proposes with board.sh post <you> learned <text>)")

__all__ = ["SSH", "EXCL", "WAKE", "WAKE_CLEAR", "HANDOFF0", "wake", "wake_clear", "CONF", "old_layout", "memory_path", "Run", "Err", "now", "ts", "say", "find_root", "Cfg", "ENV_KEEP", "clean_env", "sh", "hrun", "X", "herdr", "findkey", "fl", "ps_table", "ancestors", "AGENT_RX", "session_proc", "record_session_proc", "session_alive", "is_orchestrator", "projects", "session_root", "NUM", "Locked", "writer_lock", "FIXTURE_MARK", "secs", "pq", "qp", "atomic_write", "JANUS_SELF", "JANUS_MARK", "handoff_path", "DIFF_X", "slug", "refuse_agent"]
