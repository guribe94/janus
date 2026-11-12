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

    def api(self, procs, info, stop=None, others=()):
        """host_findings + enforce twice (the second after the grace period) for API processes; (finding keys, stop commands)."""
        if stop is not None: self.cfg.c.setdefault("enforce", {})["stop_api"] = stop
        hs = {"here": host_probe(apiproc=procs, apiinfo=info, avail_mb="64000", disk_gb="500", load="0")}
        viol, stops = [], []
        with mock.patch.object(janus, "ready_work", lambda cfg: []), mock.patch.object(janus, "other_projects", lambda cfg: list(others)):
            F = janus.host_findings(self.cfg, hs, [], viol)
        with mock.patch.object(janus, "hrun", lambda h, c, **k: stops.append(c) or (0, "stopped", "")), mock.patch.object(janus, "board", lambda *a, **k: (0, "", "")):
            janus.enforce(self.cfg, F, viol); self.assertEqual(stops, [], "stopped without the grace period")
            st = self.cfg.jget("enforce.json")
            for k in st:
                if k.startswith("spend:proc"): st[k]["warned"] -= 3600
            self.cfg.jput("enforce.json", st); janus.enforce(self.cfg, F, viol)
        return [k for k, _ in F if k.startswith("spend:proc")], stops

    def test_an_api_process_janus_did_not_start_is_reported_and_never_stopped_by_default(self):
        """Catches: a Claude API process started outside janus going unnoticed, or (re-check N2) stopped by default when it may
        be another project's or the owner's."""
        keys, stops = self.api(["4242 4242 01:10 claude -p --model x"], ["4242 ?? - /tmp"])
        self.assertEqual((keys, stops), (["spend:proc:here:4242"], []))
        keys, stops = self.api(["4242 4242 01:10 claude -p --model x"], ["4242 ?? - /tmp"], stop=True)
        self.assertEqual(stops, [], "stopped a process outside this project's unit trees")

    def test_only_this_projects_unit_trees_are_stopped_and_other_projects_or_owner_sessions_never_flagged(self):
        """Catches (re-check N2): this project's watcher flagging and stopping another janus project's orchestrator or agent's
        tree, or the owner's own interactive claude session, while an API process in this project's unit tree is never stopped."""
        other = str(self.d.parent / "other-proj")
        procs = ["5151 5151 03:10 claude --continue", "5252 5252 01:00 claude -p", "5353 5353 01:00 claude -p",
                 "5454 5454 01:00 claude", "5555 5555 01:00 claude -p"]
        info = ["5151 ?? - /x", f"5252 ?? - {other}/sub dir", "5353 ?? other-id /y", "5454 ttys003 - /z", "5555 ?? t /w"]
        keys, stops = self.api(procs, info, stop=True, others=[(other, "5151")])
        self.assertEqual(keys, ["spend:proc:here:5555"], "another project's process or the owner's session flagged")
        self.assertEqual(len(stops), 1); self.assertIn("p=5555", stops[0])
        self.assertEqual(self.api(procs, info, others=[(other, "5151")])[1], [], "stopped with stop_api not turned on")

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
