"""The reduced design's high-signal behaviors: stop without a loop, routing that cannot be bypassed, an unknown host is never a trigger,
admission that does not over-admit, no gate lane, the monitor's filter and read-only root."""
import tempfile, unittest
from pathlib import Path
from unittest import mock

from janusload import conf, load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    conf(d).write_text('[project]\nname = "t"\n[hosts.here]\nlocal = true\n[providers.api]\nenabled = true\n[providers.sub]\nenabled = true\nauth = "subscription"\n'
                       '[models.m]\nprovider = "api"\nkind = "api"\napi_id = "x"\nvendor = "anthropic"\n[models.c]\nprovider = "sub"\nkind = "codex"\nvendor = "openai"\n' + extra)
    return d, janus.Cfg(d)


class Reduction(unittest.TestCase):
    def test_the_orchestrator_stop_blocks_once_over_the_threshold_and_never_otherwise(self):
        d, cfg = project(); cfg.state.mkdir(exist_ok=True)
        with mock.patch.object(janus, "orch_tokens", lambda c: 10):
            self.assertIsNone(janus.stop_reset(cfg, {}))                       # under the threshold: nothing
        with mock.patch.object(janus, "orch_tokens", lambda c: 10**9), mock.patch.dict("os.environ", {"JANUS_IN_TESTS": "1"}):
            self.assertIn("HANDOFF.md", janus.stop_reset(cfg, {}))             # one reminder to update the handoff
            self.assertIsNone(janus.stop_reset(cfg, {"stop_hook_active": True}))   # the re-fired stop passes: no loop, the reset is asked

    def test_a_denied_route_cannot_launch_or_be_substituted(self):
        d, cfg = project('[routing]\nallow_tools = ["claude"]\nallow_auth = ["api_key"]\nnormal = ["c", "m"]\n')
        self.assertFalse(janus.model_ok(cfg, "c")[0]); self.assertIn("allow_tools", janus.model_ok(cfg, "c")[1])
        note = []; self.assertEqual(janus.pick_model(cfg, "normal", "implement", None, note=note), "m"); self.assertIn("skipped", note[0])   # said, not silent
        with self.assertRaises(janus.Err): janus.pick_model(cfg, "normal", "implement", None, fallback=False)   # unattended: a refusal, not a substitute

    def test_an_unknown_job_state_is_not_done(self):
        self.assertEqual(janus.job_state({"here": {"ok": False}}, "here:5")[0], "unknown")   # tend_parked resumes only on done or gone

    def test_no_gate_lane_and_the_first_job_must_fit_the_host_alone(self):
        self.assertEqual(janus.LANES, ("jobs", "place"))
        d, cfg = project('[capacity]\njob_gb = 64\n'); h = cfg.hosts["here"]; h.update(cores=4, ram_gb=8, reserve_gb=1)
        p = {"ok": True, "cap_rj": "0", "cap_rc": "0", "cap_rm": "0", "cap_ra": "0", "cap_ram": "0", "hist": []}
        self.assertEqual(janus.host_cap(cfg, "here", p, [])["free"], 0)       # a 64 GB job on an 8 GB host: not admitted as the first job either

    def test_the_monitor_filter_keeps_matching_rows_and_counts(self):
        M = janus.Model(); M.rows["Active"] = [janus.Row("a", ("alpha", "working"), detail=["Host: x"]), janus.Row("b", ("beta", "blocked"))]
        M.counts["Active"] = 2
        f = janus.filtered(M, "ALPH"); self.assertEqual([r.key for r in f.rows["Active"]], ["a"]); self.assertEqual(f.counts["Active"], 2)
        self.assertIn("No row matches", janus.filtered(M, "zzz").empty["Active"])


if __name__ == "__main__":
    unittest.main()
