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


def repo_of(cwd):
    """The repository a unit's tree belongs to (its git common dir), or '' when it cannot be told. Item 16a: two units in
    different repositories are never 'related' by a shared module or file name (two projects that both change a `docs` module). The
    common dir, not the worktree toplevel, so two worktrees of one repository still count as one."""
    if not cwd or not Path(cwd).is_dir(): return ""
    o = sh(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"])[1].strip()
    return str(Path(cwd, o).resolve()) if o else ""


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
        """Item 16a: compare only units in the same repository (an unknown repo on either side compares as before)."""
        ra, rb = repo_for(a), repo_for(b)
        return not (ra and rb) or ra == rb

    def add(a, b, reason, kind, topic=None):
        if not a or not b or a == b: return
        d = cases.setdefault(tuple(sorted((a, b))), {"reasons": [], "kinds": set(), "topics": []})
        if reason not in d["reasons"]: d["reasons"].append(reason)
        d["kinds"].add(kind)
        if topic and topic not in d["topics"]: d["topics"].append(topic)

    def tip(br, c):
        return sh(["git", "-C", c, "rev-parse", br])[1].strip() if c and Path(c).is_dir() else ""

    def branch_related(a, b):
        ba, bb = info[a]["branch"], info[b]["branch"]
        if ba in ("", "?") or bb in ("", "?") or ba == bb: return False
        ca, cb = umap.get(a, {}).get("cwd"), umap.get(b, {}).get("cwd")
        ta, tb = tip(ba, ca), tip(bb, cb)
        if not ta or not tb or ta == tb: return False
        return (ca and sh(["git", "-C", ca, "merge-base", "--is-ancestor", bb, ba])[0] == 0) or \
               (cb and sh(["git", "-C", cb, "merge-base", "--is-ancestor", ba, bb])[0] == 0)

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
        if not un or un in info: continue   # a running unit's own area: the module case above covers it
        rec = au.get(un) or umap.get(un)
        if not rec: continue
        mods = {m for m in (module_of(f) for f in unit_changes(cfg, rec, units)[1]) if m != HERE}
        for v in names:
            if not same_repo(v, un): continue
            shared = sorted(mods & info[v]["mods"])
            if shared: add(v, un, f"gate {gn} failed in the module {', '.join(shared[:3])} that {un} owns", "gate", shared[0])
    for lid, text in learned_posts(cfg):
        poster = lid.rsplit(":", 1)[0]
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
