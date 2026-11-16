#!/usr/bin/env python3
"""The pilot's numbers from Harbor's jobs dir: per task and arm, mean reward, solved (reward >= 0.95), cost and wall time;
then the paired delta B - A per task. Arm B's cost adds the sub-agents' spend from the container's janus ledger
(/logs/agent/janus-ledger.tsv, brought home with the trial's logs), which Harbor's own cost does not see.

    python3 bench/lhtb/report.py [bench/lhtb/jobs]  -> markdown on stdout (paste into REPORT.md)"""
import json, statistics, sys
from pathlib import Path

ARMS = {"lhtb-pilot-a-stock": "A stock", "lhtb-pilot-b-janus": "B janus"}

def ledger_cost(trial):
    """Sum of the done rows' cost in a trial's janus ledger (ts, unit, cost, "done rc=N", run; janus/ledger.py ledger_done),
    0 without one."""
    f = next(trial.glob("**/janus-ledger.tsv"), None)
    if not f: return 0.0
    tot = 0.0
    for l in f.read_text().splitlines():
        c = l.split("\t")
        if len(c) > 3 and c[3].startswith("done"):
            try: tot += float(c[2])
            except ValueError: pass
    return tot

def trials(jobs):
    for job in sorted(Path(jobs).glob("*")):
        arm = next((v for k, v in ARMS.items() if job.name.startswith(k)), None)
        if not arm: continue
        for r in job.glob("*/result.json"):
            d = json.loads(r.read_text())
            rw = ((d.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            ag = d.get("agent_result") or {}; t = d.get("agent_execution") or {}
            secs = None
            if t.get("started_at") and t.get("finished_at"):
                from datetime import datetime
                secs = (datetime.fromisoformat(t["finished_at"]) - datetime.fromisoformat(t["started_at"])).total_seconds()
            yield arm, d["task_name"], (None if rw is None else float(rw)), (ag.get("cost_usd") or 0.0) + ledger_cost(r.parent), secs

def main(jobs="bench/lhtb/jobs"):
    rows = list(trials(jobs))
    if not rows: print(f"no results under {jobs}"); return 1
    by = {}
    for arm, task, rw, cost, secs in rows: by.setdefault(task, {}).setdefault(arm, []).append((rw, cost, secs))
    arms = sorted({a for a, *_ in rows})
    print("| task | " + " | ".join(f"{a} reward (n) | {a} cost" for a in arms) + " | B-A |"); print("|---" * (2 * len(arms) + 2) + "|")
    deltas = []
    for task in sorted(by):
        cells = []; means = {}
        for a in arms:
            xs = by[task].get(a, []); rws = [x[0] for x in xs if x[0] is not None]
            means[a] = statistics.mean(rws) if rws else None
            cells.append(f"{means[a]:.2f} ({len(rws)})" if rws else "-"); cells.append(f"${sum(x[1] for x in xs):.2f}")
        d = (means.get("B janus") - means.get("A stock")) if means.get("B janus") is not None and means.get("A stock") is not None else None
        if d is not None: deltas.append(d)
        print(f"| {task} | " + " | ".join(cells) + f" | {d:+.2f} |" if d is not None else f"| {task} | " + " | ".join(cells) + " | - |")
    print()
    for a in arms:
        xs = [(rw, c, s) for arm, _, rw, c, s in rows if arm == a]; rws = [x[0] for x in xs if x[0] is not None]
        print(f"- **{a}**: mean reward {statistics.mean(rws):.3f} over {len(rws)} scored trials, solved {sum(1 for x in rws if x >= 0.95)}, "
              f"cost ${sum(x[1] for x in xs):.2f}, mean wall {statistics.mean([x[2] for x in xs if x[2]]) / 60:.0f} min" if rws else f"- **{a}**: no scored trials")
    if deltas: print(f"- **paired B-A** over {len(deltas)} tasks: mean {statistics.mean(deltas):+.3f}, B better on {sum(1 for d in deltas if d > 0)}, worse on {sum(1 for d in deltas if d < 0)}")
    return 0

if __name__ == "__main__": sys.exit(main(*sys.argv[1:]))
