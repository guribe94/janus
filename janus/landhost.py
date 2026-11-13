"""Landing on any host (skill-fix55): a host's landing lane, the choice of the landing host, git on that host.

A host's landing lane is exactly: git 2.38 or newer; a bare clone of the project's repository at <[hosts.<h>] land, default
<root>/land>/<project>.git whose origin is the project's remote ([landing] origin, else the remote of the unit's worktree); one
tree per landing unit beside it (<land>/wt/<unit>); the janus launcher in [hosts.<h>] bin; and, to fetch and push there, a key
the remote accepts ([hosts.<h>] land_env: shell run before each git command there, e.g. an export of GIT_SSH_COMMAND naming a
key outside a shared home). `janus install-host <h>` creates the clone and the launcher and says what is missing. The unit's
commits reach the clone by a push over ssh from the coordinator, so a host without a key still merges and gates; only the
last push to the remote then falls back to the coordinator, and land says so."""
import shlex
from janus.context import Err, hrun, qp, sh, slug

GIT_BATCH = "export GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=\"${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=10}\"; "

def origin_url(cfg, wt=None):
    """The project's remote: [landing] origin, else the [git] remote (default origin) of wt or of [landing] repo. None: unknown."""
    ld = cfg.c.get("landing", {})
    if ld.get("origin"): return ld["origin"]
    for d in (wt, cfg.root / ld["repo"] if ld.get("repo") else None):
        if d:
            rc, o, _ = sh(["git", "-C", str(d), "remote", "get-url", cfg.c.get("git", {}).get("remote") or "origin"])
            if rc == 0 and o.strip(): return o.strip()
    return None

def land_dir(h): return h.get("land") or f"{h.get('root', '$HOME/janus')}/land"

def land_clone(cfg, h): return f"{land_dir(h)}/{slug(cfg.c.get('project', {}).get('name', '') or 'repo') or 'repo'}.git"

def land_tree(h, unit): return f"{land_dir(h)}/wt/{unit}"

def ssh_url(h, path):
    """The clone on a host as a git URL from here (scp form; a path under $HOME is relative to the login directory)."""
    return f"{h['ssh']}:{path[6:] if path.startswith('$HOME/') else path}"

def lane_script(cfg, h, url):
    """Shell that creates the host's landing clone when it is missing and points its origin at url. No network, repeatable."""
    c = qp(land_clone(cfg, h)); u = shlex.quote(url)
    return (f"command -v git >/dev/null || {{ echo 'no git on this host'; exit 3; }}; mkdir -p {qp(land_dir(h))}/wt && "
            f"{{ [ -d {c}/objects ] || git init -q --bare {c}; }} && "
            f"{{ git -C {c} remote set-url origin {u} 2>/dev/null || git -C {c} remote add origin {u}; }}")

def lane_probe(cfg, h):
    """Shell that prints the lane's state in one line: git version, the clone, whether the host itself reaches the remote."""
    c = qp(land_clone(cfg, h))
    return (GIT_BATCH + h.get("land_env", "") + f"\nprintf 'git %s; clone %s; ' \"$(git --version | awk '{{print $3}}')\" {c}; "
            f"if git -C {c} ls-remote -q origin HEAD >/dev/null 2>&1; then echo 'remote: reachable from here'; "
            f"else echo 'remote: NOT reachable from here (no key it accepts: an owner item; land still merges and gates here, the last push is made by the coordinator)'; fi")

class HostGit:
    """git -C <path> on a host: the coordinator's own process, or over ssh with the committer's identity and land_env."""
    def __init__(self, h, path, ident=None):
        self.h, self.path = h, str(path)
        self.env = GIT_BATCH + "".join(f"export {k}={shlex.quote(v)}; " for k, v in (ident or {}).items()) + (h.get("land_env", "") + "\n" if h.get("land_env") else "")
    def __call__(self, *a, timeout=120, inp=None):
        if self.h.get("local"): return sh(["git", "-C", self.path, *a], timeout=timeout, inp=inp)
        return hrun(self.h, f"{self.env}git -C {qp(self.path)} " + " ".join(shlex.quote(str(x)) for x in a), timeout=timeout, inp=inp)

def identity(wt):
    """The committer identity of the unit's worktree, as the environment git reads (a host's clone has none of its own)."""
    n, e = (sh(["git", "-C", str(wt), "config", k])[1].strip() for k in ("user.name", "user.email"))
    return {f"GIT_{w}_{k}": v for k, v in (("NAME", n), ("EMAIL", e)) if v for w in ("AUTHOR", "COMMITTER")}

def land_host(cfg, want=None):
    """(host, why): where a land's git work and its gate run. --host, else [landing] host: a host's name, or "auto": among the
    hosts that meet [landing] needs (as janus run --needs, e.g. "linux": the gate's platform) and have git, the one with the
    most room now, any of them when none has room; the coordinator only when it is the one that qualifies. Unset: the coordinator
    (what land did before). The coordinator is never required: any host with a landing lane lands."""
    from janus.actions.jobs import host_meets, parse_needs, place_job
    from janus.probe import host_facts
    ld = cfg.c.get("landing", {}); w = want or ld.get("host")
    local = next(n for n, h in cfg.hosts.items() if h.get("local"))
    if not w: return local, "the coordinator ([landing] host is not set)"
    if w != "auto":
        if w not in cfg.hosts: raise Err(f"unknown host {w} (land --host, or [landing] host)")
        return w, "--host" if want else "[landing] host"
    needs = parse_needs([ld["needs"]]) if ld.get("needs") else {}; fs = host_facts(cfg, refresh="stale")
    ok = [n for n, h in cfg.hosts.items() if host_meets(cfg, n, fs.get(n), needs)[0] and (h.get("local") or "git" in (fs.get(n) or {}).get("features", []))]
    if not ok: return local, f"no host meets [landing] needs {ld.get('needs', '')!r} with git: the coordinator"
    try: best, lines = place_job(cfg, "gates", needs)
    except Exception: best, lines = None, []
    if best in ok: return best, f"auto: meets {ld.get('needs') or 'no needs'}, most room ({'; '.join(lines)})"
    return ok[0], f"auto: meets {ld.get('needs') or 'no needs'} (no host has room now: its lane queues)"
