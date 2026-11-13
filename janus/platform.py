"""The one adapter for OS- and tool-specific behaviour (owner direction 2026-11-11).

Every OS- or tool-specific choice lives here, so a second platform is a new function, not a new branch inside the tool:
process listing (`ps`), the elapsed-time flavour `ps` prints, the terminal/pane tool (herdr, else tmux), the secret store
(macOS keychain, Linux secret-tool or pass; a key file is the caller's fallback), the service manager (launchd or systemd user
units) and how a host is discovered (HOST_FACTS: one read-only shell script, macOS and Linux branches, parsed by parse_facts).
The coordinator may be either OS. run() is the one subprocess call (context.sh adds the environment policy on top of it), so this
module imports nothing of janus and sits at the bottom of the package. Still to fold in: containers/scopes (`podman`,
`systemd-run`) in job scripts, and launch.py's direct herdr calls (pane_tool says which tool a machine has).
"""
import json, re, shutil, subprocess, sys
from pathlib import Path


def run(cmd, cwd=None, inp=None, timeout=25, env=None):
    """(rc, stdout, stderr) of a command; 127 when it is not installed, 124 when it timed out."""
    try:
        r = subprocess.run(cmd, cwd=cwd, input=inp, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError: return 127, "", f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired: return 124, "", f"{cmd[0]}: timeout"


def ps_table():
    """{pid: (ppid, pgid, started, command)} for every process (one ps call)."""
    rc, o, e = run(["ps", "-Aww", "-o", "pid=,ppid=,pgid=,lstart=,command="], timeout=10)
    t = {}
    for l in o.splitlines():
        f = l.split(None, 8)   # pid ppid pgid + lstart is 5 words (Wed Nov 11 15:31:02 2026) + command
        if len(f) >= 8 and f[0].isdigit(): t[int(f[0])] = (int(f[1]), int(f[2]), " ".join(f[3:8]), f[8] if len(f) > 8 else "")
    return t


def env_janus(pid):
    """The JANUS_* variables of a local process ({} when none or unreadable): Linux /proc/<pid>/environ; macOS ps -E, which
    appends the environment after the command, so the command (from plain ps) is cut off first."""
    try: return {k: v for k, _, v in (x.partition("=") for x in Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")) if k.startswith("JANUS_")}
    except OSError: pass
    o, c = (run(["ps", *f, "-ww", "-p", str(pid), "-o", "command="], timeout=10)[1].rstrip("\n") for f in (["-E"], []))
    env = o[len(c):] if c and o.startswith(c) else ""
    return dict(m.groups() for m in re.finditer(r"(?:^|\s)(JANUS_[A-Z_]+)=(\S+)", env))

def secs(et):
    """ps' elapsed time ("[[dd-]hh:]mm:ss") in seconds."""
    d, _, r = et.rpartition("-"); p = [int(x) for x in r.split(":")]
    while len(p) < 3: p.insert(0, 0)
    return (int(d) if d else 0) * 86400 + p[0] * 3600 + p[1] * 60 + p[2]


def herdr(*a, timeout=20):
    """The herdr CLI (panes, agents, workspaces): its JSON result, or None."""
    rc, o, e = run(["herdr", *a], timeout=timeout)
    try: return json.loads(o).get("result", {}) if rc == 0 else None
    except Exception: return None


def secret_get(item):
    """(rc, secret) from the OS secret store: the macOS keychain (`security`), else the freedesktop store (`secret-tool lookup
    service <item>`), else `pass show <item>` (its first line); None where none of them is installed."""
    if sys.platform == "darwin": return run(["security", "find-generic-password", "-s", item, "-w"])[:2]
    if shutil.which("secret-tool"):
        rc, o = run(["secret-tool", "lookup", "service", item])[:2]
        if rc == 0 and o.strip(): return rc, o
    if shutil.which("pass"):
        rc, o = run(["pass", "show", item])[:2]; return rc, (o.splitlines() or [""])[0]
    return None


def watcher_service():
    """What runs the watcher on this machine: ("launchd", label) on macOS, ("systemd", timer) elsewhere."""
    return ("launchd", "com.janus.watch") if sys.platform == "darwin" else ("systemd", "janus-watch.timer")


def watcher_loaded():
    """True when this machine's watcher service is loaded (launchd job) or active (systemd user timer)."""
    kind, name = watcher_service()
    return run(["launchctl", "list", name] if kind == "launchd" else ["systemctl", "--user", "is-active", name])[0] == 0


# The kernel's high-water mark of a job's largest process (run_sampled in templates.ADMIT): a job shorter than the 5 s sampling
# interval, or one whose only sample lands before it allocates (a slow start under load), still records its real peak. Runs on
# the job's host with its own python3: ru_maxrss is bytes on macOS, KiB on Linux. argv: <peak file> <command...>.
MAXRSS_PY = ("import resource, subprocess, sys\n"
             "try: rc = subprocess.call(sys.argv[2:])\n"
             "except OSError: rc = 127\n"
             "m = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss\n"
             "open(sys.argv[1], 'w').write(str(int(m / (1048576 if sys.platform == 'darwin' else 1024))))\n"
             "sys.exit(rc if rc >= 0 else 128 - rc)\n")


def group_cpu_s(pgid):
    """CPU seconds the living processes of process group pgid have used so far (ps time: [d-]h:m:s or m:s.cc), None when ps fails."""
    try: o = subprocess.run(["ps", "-A", "-o", "pgid=,time="], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError): return None
    tot = 0.0
    for l in o.splitlines():
        f = l.split()
        if len(f) != 2 or f[0] != str(pgid): continue
        d, _, r = f[1].rpartition("-"); s = 0.0
        try:
            for x in r.split(":"): s = s * 60 + float(x)
            tot += s + 86400 * int(d or 0)
        except ValueError: pass
    return tot


def pane_tool():
    """The terminal/pane tool agents get here: "herdr", else "tmux", else None."""
    return next((t for t in ("herdr", "tmux") if shutil.which(t)), None)


# Host discovery (imp-sched item 1): read-only, one shell run per host (local sh or one ssh), no assumption about the OS. Prints
# key=value lines; tool=, feature=, login= and dev= repeat. PATH gets the host's bin dir (%(bin)s) like the probe; %(agent_env)s is
# the host's agent_env (agents' HOME and PATH may differ from the login's), run in a subshell before the agent CLI and login checks.
HOST_FACTS = r'''PATH="$HOME/.local/bin:%(bin)s:$PATH:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
echo "arch=$(uname -m)"
if [ "$(uname)" = Darwin ]; then
  echo "os=mac"; echo "cores=$(sysctl -n hw.ncpu)"; echo "mem_mb=$(( $(sysctl -n hw.memsize) / 1048576 ))"
  ioreg -p IOUSB -w0 2>/dev/null | grep -q '+-o .*@' && echo "dev=usb"
  security find-generic-password -s 'Claude Code-credentials' >/dev/null 2>&1 && echo "login=claude"
else
  echo "os=linux"; echo "cores=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)"; echo "mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
  ls /dev/bus/usb/*/* >/dev/null 2>&1 && echo "dev=usb"
  ls /dev/video* >/dev/null 2>&1 && echo "dev=video"; [ -e /dev/kvm ] && echo "dev=kvm"
  { ls /dev/nvidia0 || ls /dev/dri/renderD*; } >/dev/null 2>&1 && echo "dev=gpu"
  case $(systemctl --user is-system-running 2>/dev/null) in running|degraded|starting) echo "feature=systemd-user";; esac
  U=$(id -u); grep -qw cpu /sys/fs/cgroup/user.slice/user-$U.slice/user@$U.service/cgroup.controllers 2>/dev/null && echo "feature=cpu-quota"
  grep -qw memory /sys/fs/cgroup/user.slice/user-$U.slice/user@$U.service/cgroup.controllers 2>/dev/null && echo "feature=memory-max"
fi
d=%(root)s; [ -d "$d" ] || d=$HOME; echo "disk_gb=$(df -Pk "$d" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')"
for t in pueue pueued podman docker systemd-run launchctl bwrap firejail sandbox-exec tmux herdr flock secret-tool pass security git rsync python3 uv cargo node nvidia-smi; do
  command -v "$t" >/dev/null 2>&1 && echo "tool=$t"
done
( %(agent_env)s
  for t in claude codex opencode; do command -v "$t" >/dev/null 2>&1 && echo "tool=$t"; done   # agents' PATH may differ too
  { [ -s "$HOME/.claude/.credentials.json" ] || grep -qs oauthAccount "$HOME/.claude.json"; } && echo "login=claude"
  [ -s "$HOME/.codex/auth.json" ] && echo "login=codex"
  [ -s "$HOME/.local/share/opencode/auth.json" ] && echo "login=opencode" ) 2>/dev/null
true'''

ARCH = {"arm64": "aarch64", "amd64": "x86_64"}


def parse_facts(text):
    """HOST_FACTS output as {os, arch, cores, mem_mb, disk_gb, tools, features, logins, devices}. features holds the os, the
    arch, every tool and the probed features, so one --needs list can name any of them."""
    f = {"tools": [], "features": [], "logins": [], "devices": []}
    many = {"tool": "tools", "feature": "features", "login": "logins", "dev": "devices"}
    for l in text.splitlines():
        k, _, v = l.strip().partition("=")
        if not v: continue
        if k in many:
            if v not in f[many[k]]: f[many[k]].append(v)
        elif k in ("cores", "mem_mb", "disk_gb"): f[k] = int(v) if v.isdigit() else None
        elif k in ("os", "arch"): f[k] = ARCH.get(v, v)
    f["features"] = sorted({f.get("os"), f.get("arch"), *f["tools"], *f["features"]} - {None})
    return f


__all__ = ["group_cpu_s", "run", "ps_table", "secs", "herdr", "secret_get", "watcher_service", "watcher_loaded", "pane_tool", "HOST_FACTS", "ARCH", "parse_facts"]
