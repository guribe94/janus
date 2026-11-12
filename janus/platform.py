"""The one adapter for OS- and tool-specific behaviour (owner direction 2026-11-11).

Every OS- or tool-specific choice lives here, so a second platform is a new function, not a new branch inside the tool:
process listing (`ps`), the elapsed-time flavour `ps` prints, the terminal/pane tool (`herdr`), the secret store (the macOS
keychain) and the service manager (launchd/systemd). Only the macOS/Linux variants that already exist are implemented;
behaviour is unchanged. run() is the one subprocess call (context.sh adds the environment policy on top of it), so this module
imports nothing of janus and sits at the bottom of the package. Still to fold in: containers/scopes (`podman`, `systemd-run`) and
the host-discovery variants.
"""
import json, subprocess, sys


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


def keychain_get(item):
    """(rc, password) from the macOS keychain, or None where there is no keychain (not macOS)."""
    if sys.platform != "darwin": return None
    rc, o, e = run(["security", "find-generic-password", "-s", item, "-w"])
    return rc, o


def launchd_watcher_loaded(name="com.janus.watch"):
    """True when the launchd watcher job is loaded (macOS)."""
    return run(["launchctl", "list", name])[0] == 0


def systemd_watcher_active(name="janus-watch.timer"):
    """True when the systemd user timer is active (Linux)."""
    return run(["systemctl", "--user", "is-active", name])[0] == 0


__all__ = ["run", "ps_table", "secs", "herdr", "keychain_get", "launchd_watcher_loaded", "systemd_watcher_active"]


# The kernel's high-water mark of a job's largest process (run_sampled in templates.ADMIT): a job shorter than the 5 s sampling
# interval, or one whose only sample lands before it allocates (a slow start under load), still records its real peak. Runs on
# the job's host with its own python3: ru_maxrss is bytes on macOS, KiB on Linux. argv: <peak file> <command...>.
MAXRSS_PY = ("import resource, subprocess, sys\n"
             "try: rc = subprocess.call(sys.argv[2:])\n"
             "except OSError: rc = 127\n"
             "m = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss\n"
             "open(sys.argv[1], 'w').write(str(int(m / (1048576 if sys.platform == 'darwin' else 1024))))\n"
             "sys.exit(rc if rc >= 0 else 128 - rc)\n")
