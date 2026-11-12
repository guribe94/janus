"""The one adapter for OS- and tool-specific behaviour (owner direction 2026-11-11).

Every OS- or tool-specific choice lives here, so a second platform is a new function, not a new branch inside the tool:
process listing (`ps`), the elapsed-time flavour `ps` prints, the terminal/pane tool (`herdr`), the secret store (the macOS
keychain) and the service manager (launchd/systemd). Only the macOS/Linux variants that already exist are implemented;
behaviour is unchanged. `sh` is imported inside the functions so `platform` stays below `context` (no import cycle), and
`context` re-exports `ps_table`/`secs`/`herdr`, so existing call sites are unchanged. Still to fold in during the split:
containers/scopes (`podman`, `systemd-run`) and the host-discovery variants.
"""
import json


def _sh():
    from orchkit.context import sh as _s
    return _s


def ps_table():
    """{pid: (ppid, pgid, started, command)} for every process (one ps call)."""
    rc, o, e = _sh()(["ps", "-Aww", "-o", "pid=,ppid=,pgid=,lstart=,command="], timeout=10)
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
    rc, o, e = _sh()(["herdr", *a], timeout=timeout)
    try: return json.loads(o).get("result", {}) if rc == 0 else None
    except Exception: return None


def keychain_get(item):
    """(rc, password) from the macOS keychain, or None where there is no keychain (not macOS)."""
    import sys
    if sys.platform != "darwin": return None
    rc, o, e = _sh()(["security", "find-generic-password", "-s", item, "-w"])
    return rc, o


def launchd_watcher_loaded(name="com.orch.watch"):
    """True when the launchd watcher job is loaded (macOS)."""
    return _sh()(["launchctl", "list", name])[0] == 0


def systemd_watcher_active(name="orch-watch.timer"):
    """True when the systemd user timer is active (Linux)."""
    return _sh()(["systemctl", "--user", "is-active", name])[0] == 0


__all__ = ["ps_table", "secs", "herdr", "keychain_get", "launchd_watcher_loaded", "systemd_watcher_active"]
