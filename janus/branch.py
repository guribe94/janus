"""Landing-branch queries: whether a unit's tree is on the landing branch, and whether its collected patch is applied. Queries only;
the land command is in actions/landing.py."""
import re
from pathlib import Path
from janus.context import sh

def landed(cfg, u):
    """True when the unit's worktree HEAD is on the landing branch ([landing] branch, default the main repo's HEAD branch):
    an ancestor of it, or (squash-landed, janus land) every file it changed has the tip's content (contained). None when
    it cannot be told (no worktree of its own)."""
    c = Path(u.get("cwd", "/"))
    if c.resolve() == cfg.root.resolve() or not c.is_dir(): return None
    br = landing_branch(cfg, c)
    rc, o, e = sh(["git", "-C", str(c), "merge-base", "--is-ancestor", "HEAD", br])
    if rc != 1: return True if rc == 0 else None
    return contained(c, br)

CACHE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis", ".tox"}   # tool caches: never a unit's work (PATCH_PY's SKIP too)

def unapplied_patch(cfg, u):
    """The unit's collected patch (out/<unit>/changes.patch: janus collect of a unit that ran on a host without the repository)
    when it still applies cleanly to the unit's worktree, so its changes are not there; else None. Row 239: such a unit's
    branch equals the landing branch, land said 'already landed ... janus close', and a close would have dropped the work."""
    p = cfg.p("out", "out") / str(u.get("name")) / "changes.patch"; c = Path(u.get("cwd", "/"))
    if not c.is_dir() or c.resolve() == cfg.root.resolve() or not p.is_file() or not p.stat().st_size: return None
    heads = re.findall(rb"^diff --git (.*)$", p.read_bytes(), re.M)   # row 251: a patch of tool caches only holds no change to apply
    if heads and all(h.startswith(b"a/") and CACHE_DIRS & set(h[2:].split(b" b/")[0].decode(errors="replace").split("/")[:-1]) for h in heads): return None
    return p if sh(["git", "-C", str(c), "apply", "--check", str(p.resolve())])[0] == 0 else None

def landing_branch(cfg, c):
    br = cfg.c.get("landing", {}).get("branch")
    if br: return br
    rc, o, e = sh(["git", "-C", str(c), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    return o.strip() if rc == 0 and o.strip() else "main"

def contained(c, br):
    """True when every file the tree at c changed since it left br has br's tip content (squash-landed, or the same change is
    on the branch already: nothing left to land). Tree equality with any of the branch's recent commits was too loose: a
    unit that reverts a change the tip still has matched an old commit (review-fixes9 #5)."""
    mb = sh(["git", "-C", str(c), "merge-base", "HEAD", br])[1].strip()
    if not mb: return None
    fs = [f for f in sh(["git", "-C", str(c), "diff", "--name-only", mb, "HEAD"])[1].splitlines() if f]
    return sh(["git", "-C", str(c), "diff", "--quiet", br, "HEAD", "--", *fs])[0] == 0 if fs else True
