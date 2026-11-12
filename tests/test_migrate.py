"""Legacy cleanup and the setup gate finding: janus migrate ports or lists the project's own scripts that call the retired CLI
(soak 39), and check raises gate-setup for a gate whose steps all fail at once (a setup problem, not tests)."""
import tempfile, time, unittest
from pathlib import Path

from janusload import load

janus = load()


def project():
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    (d / "janus.toml").write_text('[project]\nname = "t"\n[paths]\ngate_log = "out/gates.log"\n[hosts.here]\nlocal = true\n')
    (d / "out").mkdir()
    return d, janus.Cfg(d)


class PortScripts(unittest.TestCase):
    def test_shim_ported_other_calls_listed(self):
        d = Path(tempfile.mkdtemp(prefix="janus-test-"))
        (d / "bin").mkdir(); shim = d / "bin/orch"; shim.write_text('#!/bin/sh\nexec orch "$@"\n')
        (d / "gate.sh").write_text("#!/bin/sh\norch run --host x -- true\n")
        plan = janus.port_old_scripts(d, dry=True)
        self.assertTrue(any("bin/orch" in x and "shim" in x for x in plan), plan)
        self.assertTrue(any("gate.sh:2" in x for x in plan), plan)
        self.assertIn("exec orch", shim.read_text())                      # dry run: nothing changed
        janus.port_old_scripts(d, dry=False)
        self.assertIn("exec janus", shim.read_text())                     # the shim is ported


class GateSetup(unittest.TestCase):
    def test_all_steps_failing_at_once_is_a_setup_problem(self):
        d, cfg = project()
        t = time.strftime("%Y-%m-%d %H:%M:%S")
        (d / "out/gates.log").write_text(f"{t} g EXIT_tests=2 RESULT: FAIL\n{t} h EXIT_tests=1 RESULT: FAIL\n")
        cfg.jput("gate-times.json", {"g": [0.0], "h": [3.0]})
        keys = [k for k, _ in janus.gate_findings(cfg)]
        self.assertIn("gate-setup:g", keys)                              # exit 2 in 0 min: the retired CLI / a broken script
        self.assertNotIn("gate-setup:h", keys)                           # exit 1 after 3 min: a real test failure


class ModelHosts(unittest.TestCase):
    def test_warns_when_a_routed_model_is_limited_to_fewer_hosts(self):
        d = Path(tempfile.mkdtemp(prefix="janus-test-"))
        (d / "janus.toml").write_text(
            '[project]\nname = "t"\n[hosts.a]\nlocal = true\n[hosts.b]\nssh = "x"\n[hosts.c]\nssh = "y"\n'
            '[providers.api]\nenabled = true\n[models.m]\nprovider = "api"\nkind = "claude"\nhosts = ["a"]\n'
            '[models.n]\nprovider = "api"\nkind = "claude"\n[routing]\nnormal = ["m", "n"]\n')
        cfg = janus.Cfg(d)
        self.assertTrue(any("runs on 1 of 3" in w for w in janus.model_host_warnings(cfg)))   # m limited to one host
        self.assertFalse(any("model n" in w for w in janus.model_host_warnings(cfg)))          # n may run anywhere


if __name__ == "__main__":
    unittest.main()
