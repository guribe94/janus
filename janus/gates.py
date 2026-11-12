"""Gate policy: the gate log's results, known failures, and whether a unit's tree is on the landing branch.

Queries only; the gate and land commands are in actions/landing.py."""
import re, time
from pathlib import Path
from janus.context import sh

# Gate policy (janus-fixes10 item 2): known failures, time limit,
# a quick slot, an affected-scope hook. The known-failures file ([landing] known_failures, tab-separated): <test id columns...>
# <cause> <unit fixing it at the root>; '#' lines are comments. The id is the columns before the last two joined by '/'.

def gate_results(cfg):
    """{gate name: (time, PASS|FAIL)}, the latest line of each gate in paths.gate_log."""
    try: lines = cfg.p("gate_log", "out/gates.log").read_text().splitlines()
    except OSError: return {}
    r, last = {}, {}
    for l in lines:
        m = GATE_LINE.match(l)
        if m: r[m.group(1)] = (l[:19], m.group(2)); last[m.group(1)] = l
    kf = known_failures(cfg)
    for n, (t, res) in list(r.items()):   # a project runner's FAIL whose named failures are all known passes (item 2)
        if res == "FAIL" and kf and "EXIT_timeout" not in last[n] and gate_ids(cfg, n, "failed"):
            try:
                if (cfg.p("out", "out") / n / "failed.txt").stat().st_mtime >= time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S")) - 600 \
                        and judge_gate(cfg, n, 1)[0] == "PASS": r[n] = (t, "PASS")
            except (OSError, ValueError): pass
    return r

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

GATE_LINE = re.compile(r"^\S+ \S+ (\S+) .*RESULT: (\S+)")

# $1 tests.sh, $2 name, $3 gate log, $4 python, $5 janus.py. tests.sh may name its failing tests in $JANUS_GATE_OUT/failed.txt and
# the tests that ran and passed in passed.txt (one id per line): gate-judge then passes a gate whose failures are all known.
GATE_DEFAULT = ('cd "$JANUS_ROOT" && JANUS_GATE_OUT=$(dirname "$1") bash "$1"; rc=$?; '
                'line=$("$4" "$5" gate-judge "$2" $rc); echo "$line" >> "$3"; case $line in *"RESULT: PASS"*) exit 0;; esac; exit $rc')

def known_failures(cfg):
    """{test id: (cause, unit)} from [landing] known_failures ({} when unset or missing)."""
    p = cfg.c.get("landing", {}).get("known_failures")
    if not p: return {}
    try: lines = (cfg.root / p).read_text(errors="replace").splitlines()
    except OSError: return {}
    r = {}
    for l in lines:
        if not l.strip() or l.startswith("#"): continue
        c = [x.strip() for x in l.split("\t")]
        if len(c) < 3: c += [""] * (3 - len(c))
        r["/".join(c[:-2])] = (c[-2], c[-1])
    return r

def kf_active(v): return v[1] not in ("", "-")   # set aside only while a unit fixing it is named (owner rule)

def gate_ids(cfg, name, which):
    """Test ids a gate run wrote to out/<name>/<which>.txt (failed or passed); None when the file is missing."""
    try: return [l.strip() for l in (cfg.p("out", "out") / name / f"{which}.txt").read_text(errors="replace").splitlines() if l.strip()]
    except OSError: return None

def judge_gate(cfg, name, rc):
    """(PASS|FAIL, new failures, known failures that occurred): PASS when rc is 0, or when the gate named its failing tests and
    every one is a known failure with a fixing unit. A non-zero gate that names no failing test is a failure (build, timeout)."""
    if int(rc) == 0: return "PASS", [], []
    fs = gate_ids(cfg, name, "failed")
    if not fs: return "FAIL", [f"exit {rc}"], []
    kf = known_failures(cfg); new = [i for i in fs if not (i in kf and kf_active(kf[i]))]
    return ("FAIL" if new else "PASS"), new, [i for i in fs if i not in new]
