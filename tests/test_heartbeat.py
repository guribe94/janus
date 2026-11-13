"""Tests for the heartbeat watchdog (stdlib unittest).

Run: python3 -m unittest -v tests.test_heartbeat
"""

import os
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BEAT = ROOT / "heartbeat" / "beat"
WATCHDOG = ROOT / "heartbeat" / "watchdog"


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.beat = self.dir / "heartbeat"
        self.state = self.dir / "watchdog.state"
        self.outbox = self.dir / "board-outbox" / "outbox.log"
        self.outbox.parent.mkdir()
        self.alerts = self.dir / "alerts"
        self.stub = self.dir / "alert.sh"
        self.stub.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$1\" >> " + shlex.quote(str(self.alerts)) + "\n"
        )
        self.stub.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **kw):
        e = dict(os.environ)
        e.update(
            {
                "HEARTBEAT_FILE": str(self.beat),
                "HEARTBEAT_STATE": str(self.state),
                "HEARTBEAT_OUTBOX": str(self.outbox),
                "HEARTBEAT_ALERT_CMD": shlex.quote(str(self.stub)) + ' "$1"',
                "HEARTBEAT_MAX_AGE": "900",
                "HEARTBEAT_HOST": "coord",
            }
        )
        e.update(kw)
        return e

    def run_watchdog(self, **kw):
        return subprocess.run(
            ["/bin/sh", str(WATCHDOG)],
            env=self.env(**kw),
            capture_output=True,
            text=True,
        )

    def write_beat(self, age):
        self.beat.write_text(f"{int(time.time()) - age} coord 0\n")

    def outbox_lines(self):
        return self.outbox.read_text().splitlines() if self.outbox.exists() else []

    def alert_lines(self):
        return self.alerts.read_text().splitlines() if self.alerts.exists() else []

    def test_stale_beat_raises_one_alert(self):
        self.write_beat(3600)
        r = self.run_watchdog()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.run_watchdog()   # a second run over the same stale beat does not repeat the alert
        self.assertEqual(len(self.alert_lines()), 1)
        self.assertEqual(len(self.outbox_lines()), 1)
        self.assertIn("blocked", self.outbox_lines()[0])
        self.assertIn("stale", self.alert_lines()[0])


    def test_recovery_posts_once(self):
        self.write_beat(3600)
        self.run_watchdog()
        self.write_beat(0)
        self.run_watchdog()
        self.run_watchdog()
        lines = self.outbox_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("recovered", lines[1])
        self.assertEqual(len(self.alert_lines()), 1)

    def test_fresh_beat_does_not_alert(self):
        self.write_beat(0)
        self.run_watchdog()
        self.assertEqual(self.alert_lines(), [])
        self.assertEqual(self.outbox_lines(), [])

    def test_missing_beat_is_an_outage(self):
        r = self.run_watchdog()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.alert_lines()), 1)
        self.assertIn("no beat", self.alert_lines()[0])

    def test_beat_writes_time_host_and_check_exit(self):
        r = subprocess.run(
            ["/bin/sh", str(BEAT)],
            env=self.env(HEARTBEAT_CHECK_EXIT="3"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        parts = self.beat.read_text().splitlines()[-1].split()
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].isdigit())
        self.assertEqual(parts[1], "coord")
        self.assertEqual(parts[2], "3")

    def test_beat_copies_to_the_remote(self):
        log = self.dir / "scp.log"
        scp = self.dir / "scp.sh"
        scp.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + shlex.quote(str(log)) + "\n"
        )
        scp.chmod(0o755)
        r = subprocess.run(
            ["/bin/sh", str(BEAT)],
            env=self.env(
                HEARTBEAT_REMOTE="watchdog",
                HEARTBEAT_REMOTE_PATH="/srv/hb",
                HEARTBEAT_SCP=str(scp),
            ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("watchdog:/srv/hb", log.read_text())

    def test_beat_fails_quietly_when_the_copy_fails(self):
        bad = self.dir / "bad.sh"
        bad.write_text("#!/bin/sh\nexit 1\n")
        bad.chmod(0o755)
        r = subprocess.run(
            ["/bin/sh", str(BEAT)],
            env=self.env(
                HEARTBEAT_REMOTE="watchdog",
                HEARTBEAT_REMOTE_PATH="/srv/hb",
                HEARTBEAT_SCP=str(bad),
                HEARTBEAT_SSH=str(bad),
            ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertEqual(r.stderr, "")


if __name__ == "__main__":
    unittest.main()
