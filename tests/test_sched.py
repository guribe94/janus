"""imp-sched: host discovery, late-binding placement, CPU scheduling, install-host. Standard library only; no network."""
import os, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

from janusload import load

janus = load()
FIX = Path(tempfile.mkdtemp(prefix=janus.FIXTURE_MARK + "-sched-", dir="/tmp"))

def project(extra=""):
    d = Path(tempfile.mkdtemp(prefix="janus-sched-", dir=FIX))
    (d / "janus.toml").write_text(textwrap.dedent('[project]\nname = "t"\n[hosts.here]\nlocal = true\n') + textwrap.dedent(extra))
    return d, janus.Cfg(d)

class Discovery(unittest.TestCase):
    def test_this_machine_is_discovered_and_sizes_the_host(self):
        """Catches: a host's cores and memory taken from a default (4 cores, 4 GB) instead of the machine, and macOS reporting
        arm64 where Linux says aarch64 (one --needs arch= would match only one of them)."""
        d, cfg = project()
        self.assertEqual(janus.capv(cfg, cfg.hosts["here"], "cores"), 4)   # nothing discovered yet: the default
        f = janus.host_facts(cfg)["here"]
        self.assertEqual(f["os"], "mac" if os.uname().sysname == "Darwin" else "linux")
        self.assertEqual(f["cores"], os.cpu_count()); self.assertGreater(f["mem_mb"], 256)
        self.assertNotEqual(f["arch"], "arm64"); self.assertIn(f["arch"], f["features"]); self.assertIn("python3", f["tools"])
        self.assertEqual(janus.capv(cfg, cfg.hosts["here"], "cores"), os.cpu_count())   # discovered, not assumed
        cfg.hosts["here"]["cores"] = 3; self.assertEqual(janus.capv(cfg, cfg.hosts["here"], "cores"), 3)   # janus.toml still wins

def facts(os_, arch, cores=8, mem_mb=8192, tools=(), devices=()):
    return {"os": os_, "arch": arch, "cores": cores, "mem_mb": mem_mb, "disk_gb": 100, "tools": list(tools), "logins": [], "devices": list(devices),
            "features": sorted({os_, arch, *tools}), "at": 2**40}

def probe_of(**k):
    return {"ok": True, "res": [], "hist": [], "sched": [], "direct": [], "pueue": {"tasks": {}}, "cap_pause": "", "cap_admit": "yes", **k}

class Placement(unittest.TestCase):
    """--needs: placed on a host that meets the requirements and has room by its admission; none with room: a placement waits."""
    def setUp(self):
        self.d, self.cfg = project("""
            [hosts.lx]
            ssh = "lx"
            cores = 4
            ram_gb = 8
            [hosts.big]
            ssh = "big"
            cores = 8
            ram_gb = 16
            """)
        (self.d / ".janus/hosts-facts.json").write_text(__import__("json").dumps({
            "here": facts("mac", "aarch64"), "lx": facts("linux", "x86_64", tools=["podman"], devices=["usb"]),
            "big": facts("linux", "x86_64", tools=["podman"])}))
        self.probes = {}; self.real = janus.probe
        janus.probe = lambda n, h, ro=True, cfg=None: self.probes.get(n) or probe_of()
    def tearDown(self): janus.probe = self.real
    def test_needs_filter_and_room_decide_the_host(self):
        """Catches: a job placed on a host without its device or OS, or on a full host while another that meets the needs has room."""
        n = janus.parse_needs(["linux,device=usb"])
        best, lines = janus.place_job(self.cfg, "jobs", n)
        self.assertEqual(best, "lx"); self.assertIn("here: does not meet the needs (no linux, no device usb)", lines)
        self.assertTrue(any(l.startswith("big: does not meet") and "no device usb" in l for l in lines))
        n = janus.parse_needs(["os=linux", "feature=podman"])
        self.probes["big"] = probe_of(cap_admit="cores", cap_rj="4", cap_rc="8")          # big is full by its own admission
        self.assertEqual(janus.place_job(self.cfg, "jobs", n)[0], "lx")
        self.probes["lx"] = probe_of(cap_admit="yes", wait_jobs="4")                     # lx admits, but 4 jobs already wait there
        best, lines = janus.place_job(self.cfg, "jobs", n)
        self.assertIsNone(best, lines)                                                   # no room anywhere: the placement waits
        self.assertEqual(janus.parse_needs(["arm64"])["features"], {"aarch64"})          # macOS's name for the arch matches
        self.probes.clear(); self.assertEqual(janus.place_job(self.cfg, "jobs", janus.parse_needs(["linux", "mem=12G"]))[0], "big")   # lx has 8 GB
        with self.assertRaises(janus.Err): janus.place_job(self.cfg, "jobs", janus.parse_needs(["linux", "mem=20G"]))   # none has 20 GB
    def test_pinned_job_stays_and_needs_are_kept_on_requeue(self):
        """Catches: a thrash stop moving a job pinned with --host, or re-queueing a --needs job on a host that lacks them."""
        hs = {"here": probe_of(), "lx": probe_of(), "big": probe_of()}
        self.assertEqual(janus.requeue_host(self.cfg, hs, [], "lx", {"pinned": True, "lane": "jobs"}), "lx")
        self.assertEqual(janus.requeue_host(self.cfg, hs, [], "lx", {"needs": ["linux"], "lane": "jobs"}), "big")
        self.assertEqual(janus.requeue_host(self.cfg, hs, [], "big", {"needs": ["device=usb"], "lane": "jobs"}), "lx")

class CpuScheduling(unittest.TestCase):
    """Owner rule after two broad runs held 7 cores for 3 h while short jobs waited 4 h: learned CPU per class and a cap at it,
    short jobs first with aging, --broad for whole-suite runs, queue wait and CPU recorded, overruns flagged."""
    def setUp(self):
        self.d, self.cfg = project("""
            [hosts.b]
            ssh = "b"
            cores = 4
            cores_per_job = 2
            ram_gb = 8
            job_gb = 1
            min_disk_gb = 0
            """)
        self.rd = self.d / "rd"; self.rd.mkdir()
        self.h = dict(self.cfg.hosts["b"], local=True, _rd=str(self.rd)); (self.rd / "cap.sh").write_text(janus.cap_lib(self.cfg, "b", self.h))
    def sh(self, script):
        fake = 'cap_measure() { PSI=0; LOAD=0; DF=50; }\n'
        return subprocess.run(["bash", "-c", f"RD={self.rd}; . $RD/cap.sh\n{fake}{script}"], capture_output=True, text=True).stdout.strip()
    def test_learned_cores_match_python(self):
        """Catches: every job reserving cores_per_job although its class used one core (idle reserved cores), or the shell
        admission and janus's view disagreeing on a class's cores."""
        hist = ["1\tlint\t300\t100\t0\t95\t3", "2\tsuite\t900\t100\t0\t190\t40", "3\told\t900\t100\t0", "4\tpod\t900\t100\t0\t0\t5"]
        (self.rd / "history.tsv").write_text("\n".join(hist) + "\n")
        for cls, want in (("lint", 1), ("suite", 2), ("old", 2), ("pod", 2), ("none", 2)):   # pod: CPU not measured
            self.assertEqual(int(self.sh(f"cap_cores {cls}")), want, cls); self.assertEqual(janus.class_cores(hist, cls, 2), want, cls)
        self.assertEqual(self.sh("cap_secs suite"), "100"); self.assertEqual(self.sh("cap_secs none"), "")
    def test_short_job_goes_first_and_a_waiting_job_ages(self):
        """Catches: a long job taking the last cores while a short one waits (FIFO by poll order), and a long job starved forever."""
        sleeper = subprocess.Popen(["sleep", "60"]); me = self.rd / "long.wait"; t = int(time.time())
        try:
            (self.rd / "short.wait").write_text(f"{sleeper.pid} 60 {t} 500 2\n")
            me.write_text(f"{os.getpid()} 3600 {t} 500 2\n")
            self.assertEqual(self.sh(f"ahead {me}; echo $NB $BCC $BMB"), "1 2 500")                  # the short job is ahead
            (self.rd / "h.res").write_text(f"{sleeper.pid} 2 500 {t} other\n")                       # 2 of 4 cores taken
            out = self.sh(f"admit_ok long && {{ ahead {me}; [ $((RC + CC + BCC)) -le $CORES ] && echo GO || echo YIELD; }}")
            self.assertEqual(out, "YIELD")                                                           # it would take the short job's cores
            me.write_text(f"{os.getpid()} 3600 {t - 4000} 500 2\n")                                  # waited over an hour: aged past it
            self.assertEqual(self.sh(f"ahead {me}; echo $NB"), "0")
            (self.rd / "g.gwait").write_text(f"{sleeper.pid} 1 {t} 500 2\n")                         # a gate waits in its own order
            self.assertEqual(self.sh(f"ahead {me}; echo $NB"), "0")
        finally: sleeper.kill(); sleeper.wait()
    def test_job_records_cpu_and_wait_and_is_capped(self):
        """Catches: no CPU figure to learn from (only memory was recorded), no queue wait for the report, and a job left free to
        use every core (its parallelism and its Linux scope or container not set to its reservation)."""
        a = type("A", (), {"lane": "jobs", "subdir": None, "wt": str(self.d), "usb": False, "broad": None})()
        cmd = sys.executable + " -c 'import os, time; t = time.time()\nwhile time.time() - t < 2.5: pass\nprint(os.environ[\"CARGO_BUILD_JOBS\"])'"
        os.environ["JANUS_TQ"] = str(int(time.time()) - 30)
        try: js = janus.job_script(self.cfg, "b", self.h, a, "t", cmd, "t-1")
        finally: del os.environ["JANUS_TQ"]
        (self.rd / "t-1.sh").write_text(js)
        env = {**os.environ, "JANUS_POLL_S": "1"}
        for k in ("JANUS_CORES", "MAKEFLAGS", "CARGO_BUILD_JOBS", "RUST_TEST_THREADS", "NEXTEST_TEST_THREADS", "PYTEST_XDIST_AUTO_NUM_WORKERS", "OMP_NUM_THREADS"):
            env.pop(k, None)   # the suite itself may run inside a janus lane, which exports the cap: the test is about the default
        r = subprocess.run(["bash", str(self.rd / "t-1.sh")], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn("\n2\n", r.stdout)       # capped at its 2 cores
        f = (self.rd / "history.tsv").read_text().split("\t")
        self.assertGreaterEqual(int(f[5]), 2); self.assertGreaterEqual(int(f[6]), 29)                 # CPU seconds, queue wait
        remote = dict(self.cfg.hosts["b"]); js = janus.job_script(self.cfg, "b", remote, a, "t", "make", "t-2")
        self.assertIn('-p "CPUQuota=$((CC * 100))%"', js)
        pod = dict(remote, runner="podman", container={"image": "i"}); self.assertIn("--cpus=$CC", janus.job_script(self.cfg, "b", pod, a, "t", "make", "t-3"))
    def test_broad_run_needs_a_reason_and_runs_low(self):
        """Catches: a whole-suite run queued like a short job (by pattern, or by its class's learned run time)."""
        (self.d / "wt").mkdir()
        a = lambda cmd, broad=None: type("A", (), {"host": "here", "needs": None, "lane": "jobs", "unit": None, "wt": str(self.d / "wt"), "subdir": None,
                                                 "usb": False, "priority": 0, "no_wait": True, "allow_unmerged": True, "broad": broad, "command": [cmd]})()
        self.cfg.run.dry = True
        with self.assertRaises(janus.Err) as e: janus.cmd_run(self.cfg, a("cargo test --workspace"))
        self.assertIn("--broad", str(e.exception))
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): janus.cmd_run(self.cfg, a("cargo test --workspace", "release check"))
        self.assertIn("nice -n 19", buf.getvalue())
        Path(self.cfg.hosts["here"]["_rd"]).mkdir(parents=True, exist_ok=True)
        (Path(self.cfg.hosts["here"]["_rd"]) / "history.tsv").write_text("".join(f"{i}\tmake_check\t500\t{40 * 60}\t0\n" for i in range(3)))
        with self.assertRaises(janus.Err): janus.cmd_run(self.cfg, a("make check"))                   # its class runs 40 min
        with contextlib.redirect_stdout(io.StringIO()): janus.cmd_run(self.cfg, a("make lint"))
    def test_fresh_host_without_runs_dir_is_not_full(self):
        """Catches: a host whose runs dir does not exist yet reading disk 0 GB, so its admission (and --needs placement) says full."""
        self.h["_rd"] = str(self.rd / "not" / "yet"); (self.rd / "fresh.sh").write_text(janus.cap_lib(self.cfg, "b", self.h))
        out = subprocess.run(["bash", "-c", f"RD={self.rd}/not/yet; . {self.rd}/fresh.sh; cap_measure; echo $DF"], capture_output=True, text=True).stdout
        self.assertGreater(int(out.strip()), 0)
    def test_overrun_flagged_from_class_history(self):
        """Catches: a job stuck for hours unnoticed although its class normally takes minutes."""
        t = int(time.time())
        hist = [f"{i}\tcargo_test\t500\t300\t0" for i in range(3)]
        p = probe_of(hist=hist, res=[f"1 2 500 {t - 7200} cargo_test w-1.res", f"2 2 500 {t - 7200} newclass w-2.res", f"3 2 500 {t - 600} cargo_test w-3.res"])
        F = janus.overrun_findings(self.cfg, {"here": p})
        self.assertEqual([k for k, _ in F], ["overrun:here:w-1"]); self.assertIn("24x", F[0][1])

class InstallHost(unittest.TestCase):
    def test_steps_follow_the_discovered_host(self):
        """Catches: a pueue build for the wrong arch (macOS calls it arm64), a download over a pueue that is there, a shared
        server's pueue files written into its home, and a host set up without a root folder."""
        d, cfg = project("""
            [hosts.arm]
            ssh = "arm"
            root = "$HOME/janus"
            [hosts.srv]
            ssh = "srv"
            root = "/srv/x/janus"
            pueue_dir = "/srv/x/tools"
            pueue_config = "/srv/x/tools/pueue.yml"
            bin = "/srv/x/tools"
            [hosts.bare]
            ssh = "bare"
            [heartbeat]
            watchdog = "arm"
            """)
        lx = lambda arch, tools=(): {**facts("linux", arch, tools=tools), "features": ["linux", arch, "systemd-user", *tools]}
        steps = dict(janus.install_steps(cfg, "arm", cfg.hosts["arm"], lx(janus.parse_facts("arch=arm64\nos=linux")["arch"])))
        self.assertIn("pueued-aarch64-unknown-linux-musl", steps["pueue"].replace("$x", "pueued"))
        self.assertIn('mkdir -p "$HOME"/janus/runs', steps["folders"]); self.assertIn("enable --now pueued.service", steps["pueued service"])
        steps = dict(janus.install_steps(cfg, "srv", cfg.hosts["srv"], lx("x86_64", ["pueued"])))
        self.assertNotIn("pueue", steps); self.assertNotIn("~/.config", steps["pueued service"])
        self.assertIn("systemctl --user link /srv/x/tools/pueued.service", steps["pueued service"])
        with self.assertRaises(janus.Err): janus.install_steps(cfg, "bare", cfg.hosts["bare"], lx("x86_64"))
        self.assertEqual(janus.heartbeat_env(cfg)["file"], "$HOME/janus/heartbeat")
        if janus.heartbeat_files() is None: janus.heartbeat_beat(cfg)                  # inert until heartbeat/ ships: no ssh, no error

if __name__ == "__main__": unittest.main()
