"""Quality findings: the gate test-signal.

A gate that keeps taking a long time and has not failed in its last N runs is listed with its time, proposed for scoping or
trimming; deleting a step is the owner's call, so the finding only proposes it."""
from janus.gates import gate_signals
from janus.quality import qset

def test_signal_findings(cfg):
    """test-signal:<gate>: a long gate step that has not failed in N runs ([quality] signal_runs, signal_min_min)."""
    return [(f"test-signal:{n}", f"gate {n} takes about {m:g} min and has not failed in its last {k} runs: scope or trim it "
             f"(propose the change; deletion of a step stays the owner's call)")
            for n, m, k in gate_signals(cfg, int(qset(cfg, "signal_runs")), float(qset(cfg, "signal_min_min")))]

__all__ = ["test_signal_findings"]
