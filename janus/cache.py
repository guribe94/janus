"""The result cache of `janus run --inputs <path>...`: a job whose key already passed is not run again.

A key = the host's OS, architecture and toolchain stamp + the command (and its subdirectory) + the git hash of every declared
input at the job's tree (a clean path: its tree or blob hash at HEAD; a dirty one: its files' contents). One small file per key in
<state>/cache/ ({host, task, jid, t, cmd, state}), at most [cache] max (default 2000; the oldest go first). A record is written
"pending" when the job is queued and settled from the job's own exit code (<runs>/<jid>.rc on its host) when it ends or when the
key is next asked for, so a detached job counts too; a failure is removed, never kept."""
import hashlib, json
from janus.context import atomic_write, hrun, now, qp, sh

MAX = 2000

def input_hashes(wt, inputs):
    """[[path, hash]] of the declared inputs (paths relative to the tree), or None when the tree is not a git checkout (a synced
    copy): nothing is cached there."""
    git = lambda *a: sh(["git", "-C", str(wt), *a], timeout=60)
    if git("rev-parse", "--git-dir")[0]: return None
    out = []
    for p in sorted({x.strip().rstrip("/") or "." for x in inputs}):
        rc, o, _ = git("status", "--porcelain", "-uall", "--", p)
        if rc == 0 and not o.strip():   # clean: git's own hash of the tree or blob (ignored files are no input)
            rc, o, _ = git("rev-parse", "--verify", "-q", "HEAD^{tree}" if p == "." else f"HEAD:./{p}")
            out.append([p, o.strip() if rc == 0 and o.strip() else "absent"]); continue
        h = hashlib.sha1()   # dirty: the content of every tracked or untracked (not ignored) file under it
        for f in sorted(x for x in git("ls-files", "-co", "--exclude-standard", "-z", "--", p)[1].split("\0") if x):
            h.update(f.encode() + b"\0")
            try: h.update(hashlib.sha1((wt / f).read_bytes()).digest())
            except OSError: h.update(b"gone")
        out.append([p, "dirty:" + h.hexdigest()])
    return out

def stamp(cfg, hn):
    """The host's toolchain stamp: [cache] toolchain (a literal) and the output of [cache] toolchain_cmd run on that host."""
    c = cfg.c.get("cache", {}); s = str(c.get("toolchain", ""))
    if c.get("toolchain_cmd"):
        rc, o, e = hrun(cfg.hosts[hn], c["toolchain_cmd"], timeout=30)
        if rc: return None   # no stamp, no key: a host whose toolchain cannot be read caches nothing
        s += "\n" + o.strip()
    return s

def cache_key(cfg, hn, facts, cmd, subdir, hashes):
    """The key of a job on host hn (facts: its discovered os and arch), or None when its toolchain stamp cannot be read."""
    st = stamp(cfg, hn)
    if st is None or hashes is None: return None
    f = facts or {}
    return hashlib.sha256(json.dumps([f.get("os"), f.get("arch"), st, subdir or "", cmd, hashes]).encode()).hexdigest()[:16]

def _file(cfg, key): return cfg.f("cache") / f"{key}.json"

def record(cfg, key, host, task, jid, cmd):
    """A queued job's record (pending until its exit code is known)."""
    d = cfg.f("cache"); d.mkdir(parents=True, exist_ok=True)
    atomic_write(_file(cfg, key), json.dumps({"host": host, "task": str(task), "jid": jid, "t": int(now()), "cmd": cmd[:300], "state": "pending"}))
    fs = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime); cap = int(cfg.c.get("cache", {}).get("max", MAX))
    for p in fs[:max(0, len(fs) - cap)]: p.unlink(missing_ok=True)

def settle(cfg, key, rc):
    """The job ended with rc: exit 0 becomes a pass, anything else removes the record (failures are never cached)."""
    p = _file(cfg, key)
    try: r = json.loads(p.read_text())
    except (OSError, ValueError): return None
    if rc != 0: p.unlink(missing_ok=True); return None
    r["state"] = "pass"; atomic_write(p, json.dumps(r)); return r

def lookup(cfg, key):
    """The recorded pass of key, or None. A pending record is settled from its job's exit code file, when the job has ended."""
    try: r = json.loads(_file(cfg, key).read_text())
    except (OSError, ValueError): return None
    if r.get("state") == "pass": return r
    h = cfg.hosts.get(r.get("host"))
    if not h: return None
    rc, o, _ = hrun(h, f"cat {qp(h['_rd'] + '/' + r['jid'] + '.rc')} 2>/dev/null")
    return settle(cfg, key, int(o.strip())) if o.strip().lstrip("-").isdigit() else None
