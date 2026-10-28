"""Process-level checks for the long-running, restart-independent supervisor."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DurableWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.job = Path(self.temporary.name)
        self.workers = []

    def tearDown(self):
        (self.job / "cancel").touch()
        for process in self.workers:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        self.temporary.cleanup()

    def prepare(self, source, **overrides):
        request = {
            "argv": [sys.executable, "-c", source],
            "cwd": str(self.job),
            "env": {},
            "stdin": None,
            "timeout_seconds": 10,
            "kill_grace_seconds": 0.15,
            "log_limit_bytes": 4096,
        }
        request.update(overrides)
        (self.job / "request.json").write_text(json.dumps(request), encoding="utf-8")

    def start(self):
        process = subprocess.Popen(
            [sys.executable, "-m", "rsi_service.worker", str(self.job)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.workers.append(process)
        return process

    def status(self):
        try:
            return json.loads((self.job / "status.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if predicate(status):
                return status
            time.sleep(0.03)
        self.fail("status condition not reached: %r" % self.status())

    def test_silent_child_has_heartbeat_and_completes(self):
        self.prepare("import time; time.sleep(1.6); print('done')")
        process = self.start()
        first = self.wait_for(lambda status: status.get("child_pid"))
        second = self.wait_for(lambda status: status.get("updated_at", 0) > first["updated_at"] + 0.7)
        self.assertEqual(second["state"], "running")
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(self.status()["state"], "completed")
        self.assertEqual((self.job / "stdout.log").read_text(), "done\n")

    def test_drains_both_streams_after_log_cap(self):
        self.prepare("import os; [(os.write(1, b'x'*65536), os.write(2, b'y'*65536)) for _ in range(20)]")
        self.assertEqual(self.start().wait(timeout=5), 0)
        status = self.status()
        self.assertTrue(status["log_truncated"])
        self.assertEqual(status["stdout_bytes"], 20 * 65536)
        self.assertEqual(status["stderr_bytes"], 20 * 65536)
        for name in ("stdout", "stderr"):
            self.assertEqual((self.job / (name + ".log")).stat().st_size, 4096)

    def test_large_stdin_does_not_block_timeout(self):
        self.prepare("import time; time.sleep(60)", stdin="a" * 1048576, timeout_seconds=0.2)
        self.assertEqual(self.start().wait(timeout=5), 1)
        self.assertEqual(self.status()["state"], "timed_out")

    def test_cancel_file_terminates_silent_command(self):
        self.prepare("import time; time.sleep(60)")
        process = self.start()
        self.wait_for(lambda status: status.get("child_pid"))
        (self.job / "cancel").touch()
        self.assertEqual(process.wait(timeout=5), 1)
        self.assertEqual(self.status()["state"], "cancelled")

    def test_timeout_kills_descendant_ignoring_term(self):
        marker = self.job / "escaped"
        child = (
            "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(1.2); pathlib.Path(%r).write_text('leak')" % str(marker)
        )
        parent = (
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); "
            "time.sleep(60)" % child
        )
        self.prepare(parent, timeout_seconds=0.3)
        self.assertEqual(self.start().wait(timeout=5), 1)
        self.assertEqual(self.status()["state"], "timed_out")
        time.sleep(1.0)
        self.assertFalse(marker.exists())

    def test_duplicate_and_terminal_relaunch_cannot_execute_again(self):
        self.prepare("import pathlib,time; p=pathlib.Path('calls'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); time.sleep(0.6)")
        original = self.start()
        self.wait_for(lambda status: status.get("child_pid"))
        self.assertEqual(self.start().wait(timeout=5), 2)
        self.assertEqual(original.wait(timeout=5), 0)
        self.assertEqual(self.start().wait(timeout=5), 2)
        self.assertEqual((self.job / "calls").read_text(), "x")

    def test_spawn_failure_is_terminal(self):
        self.prepare("", argv=[str(self.job / "does-not-exist")])
        self.assertEqual(self.start().wait(timeout=5), 1)
        self.assertEqual(self.status()["state"], "failed")
        self.assertIn("FileNotFoundError", self.status()["reason"])

    def test_worker_outlives_short_lived_launcher(self):
        self.prepare("import time; time.sleep(0.7); print('survived')")
        launcher = (
            "import subprocess,sys; subprocess.Popen([sys.executable,'-m',"
            "'rsi_service.worker',%r],start_new_session=True,"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
            % str(self.job)
        )
        subprocess.run([sys.executable, "-c", launcher], cwd=str(ROOT), check=True, timeout=3)
        result = self.wait_for(lambda status: status.get("state") == "completed")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual((self.job / "stdout.log").read_text(), "survived\n")

    def test_inherited_grandchild_pipes_do_not_hold_job_open(self):
        self.prepare("import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('parent done')")
        self.assertEqual(self.start().wait(timeout=5), 0)
        self.assertTrue(self.status().get("descendant_cleanup"))

    def test_failed_child_remains_failure(self):
        self.prepare("import sys; print('bad', file=sys.stderr); sys.exit(17)")
        self.assertEqual(self.start().wait(timeout=5), 1)
        self.assertEqual(self.status()["returncode"], 17)
        self.assertEqual(self.status()["state"], "failed")


if __name__ == "__main__":
    unittest.main()
