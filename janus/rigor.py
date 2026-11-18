"""Rigor levels: how much testing, reviewing and gating a unit's work gets, from `ship` (write and run nothing, land on the
report) to `thorough` (edge-case tests, the full suite, several reviews). One table, read by every place that acts on it: the
agent's brief header (tests to write, tests to run), the orchestrator's session text (reviews, landing), the agent Stop hook (exit
codes), the whole-suite guard (janus run and a remote unit's lane) and the review gate (check's review:<unit>, janus land).

The level: a brief's `RIGOR:` line or `janus launch --rigor` (recorded on the unit), else `[rigor] level` in janus.toml, else
`lean`: strict and small (low wall time, only high-signal tests, no bloat), one rule instead of prose in several places. A stricter
rule in the project's own instructions (MEMORY.md, AGENTS.md) still binds."""
from janus.context import Err
from janus.units import all_units, verdict

LEVELS = ("ship", "quick", "lean", "careful", "thorough")
DEFAULT = "lean"

# write / run: what the agent is told. broad: the whole-suite guard for agents (refuse: never, even with --broad; reason: --broad
# <reason>; free: no reason needed, still low priority). exits: the Stop hook wants exit codes beside DONE. reviews: DONE
# reviews (units launched --of the unit) a finished unit needs before janus land, by tier; other_vendor: one of them by another
# vendor than the implementer's. land: what the orchestrator is told.
R = {
    "ship": {"write": "write no tests.",
             "run": "run no tests and no builds: implement, commit, and report.",
             "broad": "refuse", "exits": False, "reviews": {"critical": 0, "normal": 0}, "other_vendor": False,
             "land": "land each unit as soon as it reports DONE; no reviews, no test runs before landing."},
    "quick": {"write": "write no new tests.",
              "run": "run only a compile or typecheck of the code you changed (its one package); no tests.",
              "broad": "refuse", "exits": True, "reviews": {"critical": 0, "normal": 0}, "other_vendor": False,
              "land": "land each unit as soon as it reports DONE with a clean compile; no reviews."},
    "lean": {"write": "add a test only when no existing test would fail if your change broke: extend an existing test rather than "
                      "add files or fixtures, never test code you did not change, and remove the tests your change makes obsolete. "
                      "A few tests that carry signal, never a suite of them.",
             "run": "run the ONE targeted check that proves the change (one test, or one filtered run); no suites, no lint or "
                    "format sweeps, no fail-first runs, no repeat of a check that passed on unchanged inputs.",
             "broad": "refuse", "exits": True, "reviews": {"critical": 0, "normal": 0}, "other_vendor": False,
             "land": "land each unit as soon as its one check passes; no reviews, no suite before landing."},
    "careful": {"write": "add a test for each behavior you changed; extend existing tests first, no duplicate coverage.",
                "run": "run the tests of the packages your change touches (a whole-suite run needs `--broad <reason>`).",
                "broad": "reason", "exits": True, "reviews": {"critical": 1, "normal": 0}, "other_vendor": False,
                "land": "a critical unit lands after 1 review of it (janus launch --kind review --of <unit>) reports DONE; after "
                        "a land that integrated moved work, run the affected tests."},
    "thorough": {"write": "add tests for each behavior you changed and its edge cases.",
                 "run": "run the full suite before you report DONE (`--broad` needs no reason at this level).",
                 "broad": "free", "exits": True, "reviews": {"critical": 2, "normal": 1}, "other_vendor": True,
                 "land": "a critical unit lands after 2 reviews report DONE (one by another vendor than its implementer), a "
                         "normal unit after 1; run the full suite after a land that integrated moved work."},
}

def check_level(x, where):
    """x when it names a level, else Err naming where it came from (a typo never silently means the default)."""
    if x not in LEVELS: raise Err(f"unknown rigor level {x!r} ({where}): one of {', '.join(LEVELS)}")
    return x

def project_level(cfg):
    """[rigor] level in janus.toml, else lean."""
    return check_level(str(cfg.c.get("rigor", {}).get("level", DEFAULT)).lower(), "[rigor] level")

def level_of(cfg, u=None):
    """The unit's level: the one recorded at launch (its RIGOR: line or --rigor), else the project's."""
    return check_level(str(u["rigor"]).lower(), f"unit {u.get('name')}") if u and u.get("rigor") else project_level(cfg)

def agent_text(level):
    """The brief header's rules for the agent at this level (launch.report_block)."""
    r = R[level]
    return (f"**Rigor: {level} (janus).** Tests to write: {r['write']} Tests to run: {r['run']} A review at this level reports "
            f"missing coverage; it does not write a test suite. A stricter rule in the project's own instructions still binds.")

def orchestrator_text(cfg):
    """The orchestrator's line about the project's level (hooks.session_start_text)."""
    lv = project_level(cfg)
    return (f"Rigor: {lv} ([rigor] level; a brief's RIGOR: line or janus launch --rigor sets one unit's). At this level: {R[lv]['land']} "
            f"Agents are told what tests to write and run; janus land refuses a unit that still lacks its required reviews.")

def review_gap(cfg, u, units=None):
    """Why u may not land yet at its level: '' when nothing is missing. Counts the reviews of u (units launched --of u, open or
    closed) whose report says VERDICT: DONE; at thorough one of them must be by another vendor than u's."""
    if u.get("kind") in ("review", "audit"): return ""
    lv = level_of(cfg, u); r = R[lv]; need = r["reviews"].get(u.get("tier") or "normal", 0)
    if not need: return ""
    seen = {x["name"]: x for x in all_units(cfg).values()}; seen.update({x["name"]: x for x in units or []})
    done = [x for x in seen.values() if x.get("of") == u["name"] and x.get("kind") == "review" and str(verdict(cfg, x) or "").startswith("VERDICT: DONE")]
    other = [x for x in done if x.get("vendor") and x.get("vendor") != u.get("vendor")]
    if len(done) >= need and (not r["other_vendor"] or other): return ""
    want = f"{need} DONE review(s)" + (f", one by a vendor other than {u.get('vendor') or 'its implementer'}" if r["other_vendor"] else "")
    return f"rigor {lv} needs {want} of this {u.get('tier') or 'normal'} unit (janus launch <name> --kind review --of {u['name']}); has {len(done)}"
