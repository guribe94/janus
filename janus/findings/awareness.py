"""Findings that make two agents aware of each other only when their work meets (imp-awareness).

related:<a>:<b> when two running units change the same file or the same module (a directory), one is on a branch the other
contains, a failed gate sits in an area another unit owns, or a learned post names another unit's area; its fix is the
`janus introduce` command. note:<agent>:<n> for a shared-file note another agent left on a card. Both families are registered
in findings/__init__.py; check.py calls these. Nothing here writes (a finding is a reader)."""
import concurrent.futures as cf
import re
from pathlib import Path
from janus.board import board_dir, learned_posts
from janus.context import now, sh
from janus.findings.units import unit_changes
from janus.gates import gate_results
from janus.msgboard import notes_of
from janus.units import ENDED, all_units

HERE = ""   # a file at the tree root has no module of its own (two root files are not "the same module")

INTRO_WINDOW = 12 * 3600   # a pair made aware of each other is not raised again for this long (janus introduce records it)


def introduced_recently(cfg, a, b):
    """True when janus introduce made a and b aware of each other recently (related:<a>:<b> then stays quiet)."""
    d = cfg.jget("related.json").get(f"{a}:{b}" if a < b else f"{b}:{a}") or {}
    return now() - (d.get("t") or 0) < INTRO_WINDOW


def module_of(rel):
    """The module/crate/package a changed file belongs to: its directory ('' at the tree root)."""
    d = str(Path(rel).parent)
    return HERE if d in (".", "") else d


REPO_KEYS = {}

def repo_of(cwd):
    """The identities ('|'-joined) of the repository a unit's tree belongs to, or '' when it cannot be told. Item 16a: two units
    in different repositories are never 'related'. Finding 10: the main checkout path (shared by its worktrees), the remote it
    clones (and a local remote's path, so a SHALLOW clone -- whose root commit is only its own boundary -- relates to its
    source), and, only for a repo with NO remote (so the whole-history walk is not paid on every check), its root commit."""
    if not cwd or not Path(cwd).is_dir(): return ""
    top = sh(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])[1].strip()
    if not top: return str(Path(cwd).resolve())
    key0 = str(Path(top).resolve())
    if key0 in REPO_KEYS: return REPO_KEYS[key0]
    common = sh(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"])[1].strip()
    main = Path(cwd, common).resolve().parent if common and not Path(common).is_absolute() else (Path(common).resolve().parent if common else Path(key0))
    parts = ["path:" + str(main)]
    url = sh(["git", "-C", str(cwd), "config", "--get", "remote.origin.url"])[1].strip()
    if url:
        parts.append("url:" + url.rstrip("/"))
        p = re.sub(r"^file://", "", url).rstrip("/")
        if p.startswith("/"): parts.append("path:" + str(Path(p).resolve()))   # /var and /private/var are one directory (macOS)
    else:
        roots = sorted(set(sh(["git", "-C", str(cwd), "rev-list", "--max-parents=0", "HEAD"])[1].split()))
        if roots: parts.append("root:" + " ".join(roots))
    REPO_KEYS[key0] = k = "|".join(dict.fromkeys(parts))
    return k


def unit_of_gate(name, names):
    """The unit a gate name belongs to: the name itself, 'land-<unit>', or '<unit>-...'; None when it names no unit."""
    for s in (name, name[5:] if name.startswith("land-") else name):
        if s in names: return s
    for u in sorted(names, key=len, reverse=True):
        if name.startswith(u + "-") or name.startswith(u + "_"): return u
    return None


def related_cases(cfg, units, hs=None):
    """{(a, b) sorted: {"reasons", "kinds", "topics"}} for every pair of running units whose work meets (the raw material of
    related_findings and of the watcher's autopilot)."""
    live = [u for u in units if u.get("state") not in ENDED + ("failed",) or u.get("_park")]
    with cf.ThreadPoolExecutor(8) as ex: ch = list(ex.map(lambda u: unit_changes(cfg, u, units), live))
    info = {}
    for u, (br, files) in zip(live, ch):
        info[u["name"]] = {"branch": br, "files": files, "repo": repo_of(u.get("cwd")),
                           "mods": {m for m in (module_of(f) for f in files) if m != HERE}}
    umap = {u["name"]: u for u in units}
    cases = {}
    au = all_units(cfg)

    def repo_for(n):
        if n in info: return info[n]["repo"]
        return repo_of((au.get(n) or umap.get(n) or {}).get("cwd"))

    def same_repo(a, b):
        """Item 16a: compare only units in the same repository (an unknown repo on either side compares as before). Finding 10:
        the keys are identity sets, so a shallow clone matches its source on the shared main-checkout path."""
        ra, rb = repo_for(a), repo_for(b)
        return not (ra and rb) or bool(set(ra.split("|")) & set(rb.split("|")))

    def add(a, b, reason, kind, topic=None):
        if not a or not b or a == b: return
        d = cases.setdefault(tuple(sorted((a, b))), {"reasons": [], "kinds": set(), "topics": []})
        if reason not in d["reasons"]: d["reasons"].append(reason)
        d["kinds"].add(kind)
        if topic and topic not in d["topics"]: d["topics"].append(topic)

    def tip(br, c):
        """The commit a unit is compared by: its branch's tip, or, for a detached HEAD, its own commit (item 18: the literal
        `HEAD` resolved in another worktree is *that* worktree's, which paired units whose branches do not contain each other)."""
        if not c or not Path(c).is_dir(): return ""
        return sh(["git", "-C", c, "rev-parse", "HEAD" if br == "HEAD" else br])[1].strip()

    def branch_related(a, b):
        ba, bb = info[a]["branch"], info[b]["branch"]
        if ba in ("", "?") or bb in ("", "?"): return False
        if ba == bb and ba != "HEAD": return False   # the same branch (two detached HEADs are compared by commit below)
        ca, cb = umap.get(a, {}).get("cwd"), umap.get(b, {}).get("cwd")
        ta, tb = tip(ba, ca), tip(bb, cb)
        if not ta or not tb or ta == tb: return False
        an = lambda c, x, y: bool(c and sh(["git", "-C", c, "merge-base", "--is-ancestor", x, y])[0] == 0)
        return an(ca, tb, ta) or an(cb, ta, tb)   # one tip contains the other (a detached HEAD by its commit)

    names = sorted(info)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if not same_repo(a, b): continue   # item 16a: different repositories never collide
            both = sorted(set(info[a]["files"]) & set(info[b]["files"]))
            if both: add(a, b, f"both change {', '.join(both[:3])}", "file", both[0])
            shared = sorted(info[a]["mods"] & info[b]["mods"])
            if shared: add(a, b, f"both change the module {', '.join(shared[:3])}", "module", shared[0])
            if branch_related(a, b): add(a, b, f"{a} and {b} are on branches one of which contains the other", "branch")
    allnames = set(names) | set(au) | set(umap)
    for gn, (_t, res) in sorted(gate_results(cfg).items()):
        if res != "FAIL": continue
        un = unit_of_gate(gn, allnames)
        if not un or un in info or un not in umap: continue   # a running unit's own area (module case above); only an OPEN unit (item 17)
        rec = au.get(un) or umap.get(un)
        if not rec: continue
        mods = {m for m in (module_of(f) for f in unit_changes(cfg, rec, units)[1]) if m != HERE}
        for v in names:
            if not same_repo(v, un): continue
            shared = sorted(mods & info[v]["mods"])
            if shared: add(v, un, f"gate {gn} failed in the module {', '.join(shared[:3])} that {un} owns", "gate", shared[0])
    for lid, text in learned_posts(cfg):
        poster = lid.rsplit(":", 1)[0]
        if poster not in umap: continue   # item 17: a learned post from a closed unit is not a live collision
        for v in names:
            if v == poster or not same_repo(poster, v): continue
            area = [*info[v]["mods"], *info[v]["files"]]
            if re.search(rf"(?<![\w.-]){re.escape(v)}(?![\w-])", text) or any(x and x in text for x in area):
                add(poster, v, f"learned post {lid} names {v}'s area", "learned")
    return cases


def related_findings(cfg, units, hs):
    """related:<a>:<b>, one per colliding pair, unless janus introduce already made them aware of each other recently."""
    F = []
    for (a, b), d in sorted(related_cases(cfg, units, hs).items()):
        if introduced_recently(cfg, a, b): continue
        why = "; ".join(d["reasons"])[:200]
        F.append((f"related:{a}:{b}", f"{a} and {b}: {why}. Make them aware: janus introduce {a} {b} --why \"{why[:140]}\""))
    return F


def note_findings(cfg):
    """note:<agent>:<n>: a shared-file note another agent left on a card (n counts its notes; an ack hides them)."""
    ad = board_dir(cfg) / "agents"; F = []
    for c in (sorted(ad.glob("*.md")) if ad.is_dir() else []):
        ns = notes_of(c)
        if ns:
            F.append((f"note:{c.stem}:{len(ns)}", f"{ns[-1][1]} left a note on {c.stem}'s card: "
                      + "; ".join(f"{by}: {x}" for _t, by, x in ns[-3:])[:200] + f"; answer with board.sh direct {c.stem} <text>"))
    return F


__all__ = ["module_of", "repo_of", "unit_of_gate", "introduced_recently", "related_cases", "related_findings", "note_findings"]
