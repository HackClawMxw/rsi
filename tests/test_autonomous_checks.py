import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from rsi_service.autonomous_checks import check


class AutonomousChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rsi-checks-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / "output"
        self.output.mkdir()
        self.context = {"result_path": str(self.output / "result.json"), "workspace": str(self.root),
                        "skill_dir": str(self.root), "check_stage": "execution", "checks": [],
                        "log_limit_bytes": 1024}

    def run_source(self, source, timeout=2):
        self.context["checks"] = [{"id": "validator", "argv": ["{python}", "-c", source],
                                   "timeout_seconds": timeout}]
        result = check(self.context)
        details = json.loads((self.output / "checks.json").read_text())["checks"][0]
        return result, details

    def descendant_source(self, parent_wait, marker, ready=None):
        child = "import time,pathlib; time.sleep(0.35); pathlib.Path(" + repr(str(marker)) + ").write_text('late')"
        source = "import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); "
        if ready:
            source += "pathlib.Path(" + repr(str(ready)) + ").write_text('ready'); "
        return source + "time.sleep(" + str(parent_wait) + ")"

    def test_python_placeholder_records_success_with_bounded_log_and_receipt(self):
        before = signal.getsignal(signal.SIGTERM)
        result, detail = self.run_source("print('validated')")
        self.assertTrue(result["passed"])
        self.assertEqual(detail["returncode"], 0)
        self.assertFalse(detail["log_truncated"])
        self.assertEqual((self.output / "validator.log").read_text(), "validated\n")
        self.assertEqual(set(result["evidence"]), {"checks.json", "validator.log"})
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_nonzero_exit_is_a_measured_failure(self):
        result, detail = self.run_source("import sys; print('failure'); sys.exit(7)")
        self.assertFalse(result["passed"])
        self.assertEqual(detail["returncode"], 7)

    def test_timeout_kills_owned_descendants_even_when_their_output_is_redirected(self):
        marker = self.root / "late-timeout"
        result, detail = self.run_source(self.descendant_source(60, marker), timeout=0.1)
        self.assertFalse(result["passed"])
        self.assertEqual(detail["reason"], "timeout")
        time.sleep(0.45)
        self.assertFalse(marker.exists(), "timed-out check left a running descendant")

    def test_successful_parent_cannot_leave_background_validation_running(self):
        marker = self.root / "late-background"
        result, detail = self.run_source(self.descendant_source(0, marker))
        self.assertFalse(result["passed"])
        self.assertEqual(detail["reason"], "descendants_outlived_check")
        self.assertTrue(detail["descendant_cleanup"])
        time.sleep(0.45)
        self.assertFalse(marker.exists())

    def test_log_limit_stops_command_and_prevents_pass(self):
        result, detail = self.run_source("import os,time; os.write(1,b'x'*65536); time.sleep(60)")
        self.assertFalse(result["passed"])
        self.assertEqual(detail["reason"], "log_limit")
        self.assertTrue(detail["log_truncated"])
        self.assertLessEqual((self.output / "validator.log").stat().st_size, 1024)
        self.assertGreater(detail["output_bytes"], detail["log_bytes"])

    def test_sigterm_cleans_group_and_does_not_launch_next_check(self):
        marker, ready, later = (self.root / name for name in ("late-cancel", "ready", "second-check"))
        self.context["checks"] = [
            {"id": "long", "argv": ["{python}", "-c", self.descendant_source(60, marker, ready)], "timeout_seconds": 60},
            {"id": "next", "argv": ["{python}", "-c", "import pathlib; pathlib.Path(" + repr(str(later)) + ").touch()"],
             "timeout_seconds": 2},
        ]
        context_path = self.root / "context.json"
        context_path.write_text(json.dumps(self.context))
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen([sys.executable, "-m", "rsi_service.autonomous_checks", "--context", str(context_path)],
                                   cwd=Path(__file__).resolve().parents[1], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr.decode())
            result = json.loads(Path(self.context["result_path"]).read_text())
            self.assertFalse(result["passed"])
            self.assertEqual(result["cancelled_signal"], signal.SIGTERM)
            time.sleep(0.45)
            self.assertFalse(marker.exists())
            self.assertFalse(later.exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)

    def test_check_ids_and_log_links_cannot_escape_output(self):
        for identity in ("../escaped", "/absolute", "a/b", "..", "a\\b"):
            with self.subTest(identity=identity):
                self.context["checks"] = [{"id": identity, "argv": ["{python}", "-c", "pass"], "timeout_seconds": 1}]
                with self.assertRaisesRegex(ValueError, "safe file names"):
                    check(self.context)
        target = self.root / "outside"
        target.write_text("unchanged")
        (self.output / "validator.log").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.run_source("print('overwrite')")
        self.assertEqual(target.read_text(), "unchanged")

    def test_no_operator_checks_is_disclosed_not_an_invented_validation(self):
        result = check(self.context)
        self.assertTrue(result["passed"])
        report = json.loads((self.output / "checks.json").read_text())
        self.assertEqual(report["checks"], [])
        self.assertIn("No independent task checks", report["note"])


if __name__ == "__main__":
    unittest.main()
