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

def verify_published(c, pub):
    """Whether a unit's recorded publication (pub: {"commit", "remote", "url", "branch"}) is on the remote now: the branch is fetched
    again into the repository at c (only FETCH_HEAD is written) and the commit must be in its history. True or False; None when
    it cannot be told (no repository, the fetch failed, an unknown commit): the callers refuse then (fail closed)."""
    g = lambda *a, t=25: sh(["git", "-C", str(c), *a], timeout=t)
    if not pub.get("commit") or not pub.get("branch") or g("rev-parse", "--git-dir")[0]: return None
    if g("fetch", "-q", pub.get("url") or pub.get("remote") or "origin", f"refs/heads/{pub['branch']}", t=120)[0]: return None
    rc = g("merge-base", "--is-ancestor", pub["commit"], "FETCH_HEAD")[0]
    return True if rc == 0 else False if rc == 1 else None

def published_contains(cfg, u):
    """None when the unit's worktree holds nothing its published landing lacks, else why not. A squash landing leaves the unit's
    own commits unpushed for good, so ancestry cannot tell; here the publication recorded at janus land must be on the remote now
    (verify_published, fetched again), and every file the branch changed since it left the landed commit's history must have
    that commit's content. A unit without a record, an unknown publication, a divergent record or a branch edited after the
    landing is refused: no unit gains a bypass without proof."""
    pub = u.get("published") or {}; c = u.get("cwd", "/"); g = lambda *a: sh(["git", "-C", str(c), *a])
    if not pub.get("commit"): return "no verified publication recorded (janus land --push records it)"
    if not pub.get("source"): return "the publication record has no source HEAD (the commit janus land squashed)"
    rc, head, e = g("rev-parse", "HEAD")
    if rc: return f"its HEAD cannot be read ({e.strip()[:80]})"
    if head.strip() != pub["source"]: return f"its HEAD {head.strip()[:12]} is not the published source {pub['source'][:12]} (changed after the landing, or another landing)"
    ok = verify_published(c, pub); at = f"{pub['commit'][:12]} on {pub.get('remote')}/{pub.get('branch')}"
    if ok is None: return f"the publication {at} cannot be verified now (fetch failed or unknown commit)"
    if not ok: return f"{at} is not on the remote now"
    rc, mb, e = g("merge-base", "HEAD", pub["commit"])
    if rc or not mb.strip(): return f"its branch shares no history with {at}" + (f" ({e.strip()[:80]})" if rc > 1 else "")
    rc, fs, e = g("diff", "--name-only", mb.strip(), "HEAD")
    if rc: return f"git diff failed ({e.strip()[:80]})"
    fs = [f for f in fs.splitlines() if f]
    rc = g("diff", "--quiet", pub["commit"], "HEAD", "--", *fs)[0] if fs else 0
    if rc == 1: return f"its branch differs from the published {at} (a different landing)"
    if rc: return "git diff against the published commit failed"
    return None
