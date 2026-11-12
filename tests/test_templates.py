"""Every shell template janus writes is at least valid shell: render it and run `sh -n` (section 7, Step 0 / seam 4).

Templates live in janus/templates.py; the renders here exercise each one with sample values, the way its caller would.
The renderers themselves (cap_lib, clean_pueue_script, job_script, scope_script, tidy_host, gate) stay with their concern;
this test is the honest minimum the seam promises: a template that no longer parses fails here, not on a host at runtime.
Standard library only; no network, no API spend."""
import os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path

from janusload import load

janus = load()


def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-test-"))
    (d / "janus.toml").write_text(textwrap.dedent('''
        [project]
        name = "t"
        [hosts.here]
        local = true
        [providers.api]
        enabled = true
        cap_usd = 10
        budget_per_unit = 1
        ledger = "out/ledger.tsv"
        key_file = "KEYFILE"
        [models.m]
        provider = "api"
        kind = "api"
        api_id = "test-model"
        ''').replace("KEYFILE", str(d / "key.env")) + extra)
    (d / "out").mkdir(); (d / "briefs").mkdir()
    return d, janus.Cfg(d)


class Templates(unittest.TestCase):
    """Each rendered script parses with `sh -n` (the shell every host runs)."""

    def sh_n(self, text, name):
        r = subprocess.run(["sh", "-n"], input=text, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, f"{name} is not valid sh:\n{r.stderr}\n---\n{text[:400]}")

    def test_cap_lib(self):
        d, cfg = project('\n[hosts.b]\nssh = "b"\ncores = 8\ncores_per_job = 2\nram_gb = 8\nservices_gb = 2\njob_gb = 2.5\nagent_gb = 1\nmin_disk_gb = 0\n')
        h = dict(cfg.hosts["b"], local=True, _rd=str(d / "rd")); (d / "rd").mkdir()
        self.sh_n(janus.cap_lib(cfg, "b", h), "CAP_LIB")

    def test_probe_and_direct_probe(self):
        d, cfg = project('\n[hosts.b]\nssh = "b"\ncores = 8\nram_gb = 8\n')
        h = dict(cfg.hosts["b"], _rd=str(d / "rd"))
        ctx = {"bin": h.get("bin", ""), "root": janus.qp(h.get("root", "/")), "pueue": janus.pueue(h), "extra": janus.probe_extra(h),
               "rd": janus.qp(h["_rd"]), "ro": "1", "cap": "cap_report() { :; }"}
        self.sh_n(janus.PROBE % ctx, "PROBE")
        self.sh_n(janus.DIRECT_PROBE % {"rx": "'cargo'"}, "DIRECT_PROBE")

    def test_job_templates(self):
        self.sh_n(janus.ADMIT % {"lane": "jobs", "clsrc": "'cargo test'", "jid": "'j1'"}, "ADMIT")
        self.sh_n(janus.STOP_GROUP % {"pid": 1, "pgid": 2}, "STOP_GROUP")
        self.sh_n(janus.CLEAN_PUEUE % {"real": "pueue"}, "CLEAN_PUEUE")
        self.sh_n(janus.UNIT_PROCS % {"unit": "u", "roots": "1 2"}, "UNIT_PROCS")
        self.sh_n(janus.FINGERPRINT % {"root": "'/tmp'", "n": "u", "ex": "--exclude=.git"}, "FINGERPRINT")

    def test_gate_default(self):
        self.sh_n(janus.GATE_DEFAULT, "GATE_DEFAULT")


if __name__ == "__main__":
    unittest.main()
