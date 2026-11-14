"""Spend safety (real money): the overshoot margin, the 80/90 % findings, API processes janus did not start, and the done
against committed comparison. Standard library only; no network, no API spend."""
import time, unittest
from unittest import mock

from test_janus import host_probe, janus, project


class SpendSafety(unittest.TestCase):
    def setUp(self): self.d, self.cfg = project(); self.pv = self.cfg.prov["api"]; self.mc = self.cfg.models["m"]   # cap $10

    def test_reservation_holds_the_overshoot_margin(self):
        """Catches: a run reserved at its bare budget while runs overshoot it (21 over, worst 18 %), so the cap is passed."""
        janus.reserve_budget(self.cfg, self.mc, "u1", 8)   # holds $9.60
        with self.assertRaises(janus.Err): janus.reserve_budget(self.cfg, self.mc, "u2", 0.5)   # $0.60 more would pass $10
        self.assertAlmostEqual(janus.committed(self.cfg, self.pv), 9.6)

    def test_findings_at_80_and_90_percent_of_the_cap(self):
        """Catches: the owner hearing about spend only when the cap is already reached."""
        keys = lambda: [k for k, _ in janus.spend_findings(self.cfg)]
        self.assertEqual(keys(), [])
        r = janus.reserve_budget(self.cfg, self.mc, "u1", 7); self.assertEqual(keys(), ["spend:api:80"])   # $8.40 held
        janus.ledger_done(self.cfg, self.pv, r, "u1", 9.2, 0); self.assertEqual(keys(), ["spend:api:90"])

    def api(self, procs, info, others=()):
        """host_findings for API processes: the spend:proc finding keys. Nothing is ever stopped from a finding."""
        hs = {"here": host_probe(apiproc=procs, apiinfo=info, avail_mb="64000", disk_gb="500", load="0")}
        with mock.patch.object(janus, "ready_work", lambda cfg: []), mock.patch.object(janus, "other_projects", lambda cfg: list(others)):
            F = janus.host_findings(self.cfg, hs, [])
        return [k for k, _ in F if k.startswith("spend:proc")]

    def test_an_api_process_janus_did_not_start_is_reported_and_never_stopped_by_default(self):
        """Catches: a Claude API process started outside janus going unnoticed, or (re-check N2) stopped by default when it may
        be another project's or the owner's."""
        self.assertEqual(self.api(["4242 4242 01:10 claude -p --model x"], ["4242 ?? - /tmp"]), ["spend:proc:here:4242"])

    def test_other_projects_and_owner_sessions_are_never_flagged(self):
        """Catches (re-check N2): this project's watcher flagging another janus project's orchestrator or agent's tree, or the
        owner's own interactive claude session."""
        other = str(self.d.parent / "other-proj")
        procs = ["5151 5151 03:10 claude --continue", "5252 5252 01:00 claude -p", "5353 5353 01:00 claude -p",
                 "5454 5454 01:00 claude", "5555 5555 01:00 claude -p"]
        info = ["5151 ?? - /x", f"5252 ?? - {other}/sub dir", "5353 ?? other-id /y", "5454 ttys003 - /z", "5555 ?? t /w"]
        self.assertEqual(self.api(procs, info, others=[(other, "5151")]), ["spend:proc:here:5555"], "another project's process or the owner's session flagged")

    def test_a_gone_run_without_a_done_row_is_flagged_with_done_against_committed(self):
        """Catches: a run whose agent is gone held as running forever, the committed spend drifting from what was spent."""
        janus.ledger_done(self.cfg, self.pv, janus.reserve_budget(self.cfg, self.mc, "a", 1, "a.1"), "a", 0.5, 0)
        janus.reserve_budget(self.cfg, self.mc, "b", 2, "b.1")
        units = [{"name": "b", "run": "b.1", "runs": ["b.1"], "state": "gone"}]
        with mock.patch.object(janus, "all_units", lambda cfg: {"b": units[0]}):
            F = dict(janus.ledger_findings(self.cfg, units))
        self.assertIn("done $0.50 vs committed $2.90", F.get("ledger:api:unreconciled", ""), F)


if __name__ == "__main__":
    unittest.main()
