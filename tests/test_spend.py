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

    def test_an_api_process_janus_did_not_start_is_flagged_then_stopped(self):
        """Catches: a Claude API process started outside janus spending outside the ledger and caps, unnoticed and never stopped."""
        hs = {"here": host_probe(apiproc=["4242 4242 01:10 claude -p --model x"], avail_mb="64000", disk_gb="500", load="0")}
        viol = []
        with mock.patch.object(janus, "ready_work", lambda cfg: []):
            F = janus.host_findings(self.cfg, hs, [], viol)
        self.assertIn("spend:proc:here:4242", [k for k, _ in F])
        stops = []
        with mock.patch.object(janus, "hrun", lambda h, c, **k: stops.append(c) or (0, "stopped", "")):
            janus.enforce(self.cfg, F, viol); self.assertEqual(stops, [], "stopped without the grace period")
            st = self.cfg.jget("enforce.json"); st["spend:proc:here:4242"]["warned"] -= 3600; self.cfg.jput("enforce.json", st)
            janus.enforce(self.cfg, F, viol)
        self.assertEqual(len(stops), 1); self.assertIn("p=4242", stops[0])

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
