"""Quality findings: the gate test-signal, and the tests-earn-their-place rule at landing.

A gate that keeps taking a long time and has not failed in its last N runs is listed with its time, proposed for scoping or
trimming; deleting a step is the owner's call, so the finding only proposes it. A finished unit whose diff adds or changes test
files while its report.json names no test it added (or names one without the failure it catches) is listed before it lands:
tests earn their place by catching a real failure (the quality rule every brief header states)."""
import re
from janus.gates import gate_signals
from janus.quality import qset
from janus.units import report_json

def test_signal_findings(cfg):
    """test-signal:<gate>: a long gate step that has not failed in N runs ([quality] signal_runs, signal_min_min)."""
    return [(f"test-signal:{n}", f"gate {n} takes about {m:g} min and has not failed in its last {k} runs: scope or trim it "
             f"(propose the change; deletion of a step stays the owner's call)")
            for n, m, k in gate_signals(cfg, int(qset(cfg, "signal_runs")), float(qset(cfg, "signal_min_min")))]

# A path that is a test file: a tests/ or specs/ directory, a test_<name> or <name>_test.<ext> module, a *.test.* / *.spec.*
# file, or a Test*.java class. Deliberately broad: it only decides whether to look at report.json, and a false positive asks the
# agent to name the failure a test catches (cheap), never changes what lands by itself.
TESTFILE_RX = re.compile(r"(^|/)(tests?|specs?)/|(^|/)test_[^/]+$|_test\.(py|go|rs|rb|js|ts|tsx|java|c|cc|cpp)$"
                         r"|\.(test|spec)\.[a-z]+$|(^|/)Test[^/]*\.java$", re.I)

def looks_like_test(p): return bool(TESTFILE_RX.search(p))

def _missing_catches(j):
    """The tests_added entries of a report.json that do not name the failure the test catches (an entry that is not an object
    with a non-empty catches, or names a test with none)."""
    return [e for e in (j.get("tests_added") or [])
            if not (isinstance(e, dict) and str(e.get("catches") or "").strip())]

def quality_findings(cfg, units):
    """quality:<unit>: a finished DONE unit (report.json written) whose diff adds or changes test files, but whose report.json
    names no test it added with the failure each catches. The failure caught: tests added with no failure named, slipping into a
    landing unreviewed (the rule every brief header states). Only the unit's own worktree is judged."""
    from janus.findings.units import unit_changes   # lazy: findings.units imports the unit layer, not this module
    F = []
    for u in units:
        v = u.get("verdict") or ""
        if "DONE" not in v: continue
        j = report_json(cfg, u)
        if not j: continue   # no structured report: the report.json rule is prompted in the brief header, judged elsewhere
        _, files = unit_changes(cfg, u, units)
        if not any(looks_like_test(f) for f in files): continue
        bad = _missing_catches(j)
        if not (j.get("tests_added") or []):
            F.append((f"quality:{u['name']}", f"{u['name']} changes test file(s) ({', '.join(f for f in files if looks_like_test(f))[:120]}) "
                      f"but its report.json lists no tests_added: name the failure each test catches, or drop the test"))
        elif bad:
            F.append((f"quality:{u['name']}", f"{u['name']} changed test file(s) and {len(bad)} report.json tests_added entr(y|ies) name no "
                      f"failure they catch: give each a 'catches' text, or drop the test"))
    return F

__all__ = ["test_signal_findings", "quality_findings", "looks_like_test"]
