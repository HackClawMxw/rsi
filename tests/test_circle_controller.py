"""Behavioral integration tests: real workers, deterministic non-LLM checkers."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rsi_service.config import load_config
from rsi_service.controller import Controller, controller_lock, worker_busy
from rsi_service.storage import atomic_json, read_json


HOOK = r'''
import json
import os
import time
from pathlib import Path

context = json.loads(Path(os.environ["RSI_CONTEXT_FILE"]).read_text())
root = Path(__file__).parent
control = json.loads((root / "control.json").read_text())
phase = context["phase"]
skill = Path(context["skill_dir"])
level = int((skill / "notes.md").read_text())
with (root / "invocations.jsonl").open("a") as stream:
    stream.write(json.dumps({"phase": phase, "cycle": context["cycle_id"],
                             "role": context["evaluation_role"], "level": level,
                             "digest": context["skill_digest"]}) + "\n")
output = Path(os.environ["RSI_RESULT_PATH"])
output.parent.mkdir(parents=True, exist_ok=True)
if phase == "run":
    result = {"outcome": "signed", "summary": "executed selected skill",
              "evidence": []}
elif phase == "improve":
    mode = control.get("improvement", "normal")
    if mode == "protected":
        (Path(context["candidate_dir"]) / "SKILL.md").write_text("weakened gate")
    elif mode != "none":
        (Path(context["candidate_dir"]) / "notes.md").write_text(str(level + 1))
    result = {"summary": "fixture proposal", "changed": mode != "none"}
else:
    (output.parent / "measurement.txt").write_text("measured level=" + str(level))
    result = {"passed": True, "evidence": ["measurement.txt"]}
    if phase == "quick" and control.get("quick") == "fail":
        result["passed"] = False
    if phase == "evaluate":
        time.sleep(control.get("evaluation_delay", 0))
        score = 10 + level
        cases = {"correctness": 1.0, "efficiency": score}
        is_candidate = context["evaluation_role"] == "candidate"
        if is_candidate and control.get("evaluation") == "regression":
            cases["correctness"] = 0.9
        if is_candidate and control.get("evaluation") == "no_gain":
            score -= 1
        if is_candidate and control.get("evaluation") == "fail":
            result["passed"] = False
        if is_candidate and control.get("evaluation") == "missing_case":
            del cases["correctness"]
        if not is_candidate and control.get("baseline") in ("fail", "no_evidence"):
            result["passed"] = False
            cases["correctness"] = 0.0
        if not is_candidate and control.get("baseline") == "no_evidence":
            result["evidence"] = []
        if not is_candidate and control.get("baseline") == "malformed":
            result["passed"] = "false"
        result.update(score=score, cases=cases, skill_digest=context["skill_digest"],
                      validation_key=context["validation_key"])
if control.get("malformed_phase") == phase:
    result = []
output.write_text(json.dumps(result))
'''


class CircleControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rsi-controller-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "target"
        self.skill = self.root / "skill"
        self.state_dir = self.root / "state"
        self.workspace.mkdir()
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text("fixture gates remain fixed")
        (self.skill / "notes.md").write_text("0")
        self.script = self.root / "hook.py"
        self.script.write_text(HOOK)
        self.control = self.root / "control.json"
        self.control.write_text("{}")
        self.config_path = self.root / "circle.json"
        self.raw_config = {
            "schema_version": 1, "workspace": str(self.workspace),
            "skill_source": str(self.skill), "state_dir": str(self.state_dir),
            "task": "Execute the fixture and improve skill notes using measured evidence.",
            "allowed_paths": ["notes.md", "SKILL.md"],
            "protected_paths": [],
            "steps": {phase: {"adapter": "command", "argv": ["{python}", str(self.script)]}
                      for phase in ("run", "improve", "quick", "evaluate")},
            "validation": {"key": "fixture-target-hardware-v1", "inputs": [str(self.script), str(self.control)],
                           "baseline_cache_ttl_seconds": 120, "min_gain": 0.1, "max_case_regression": 0},
            "limits": {"max_cycles": 2, "max_no_change_cycles": 3, "max_wall_seconds": 60,
                       "poll_seconds": 0.02,
                       "timeouts": {phase: 10 for phase in ("run", "improve", "quick", "evaluate")}},
        }
        self.controllers = []
        self.services = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.services:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        jobs = list((self.state_dir / "jobs").glob("*"))
        for job in jobs:
            if worker_busy(job):
                (job / "cancel").touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(worker_busy(job) for job in jobs):
            time.sleep(0.05)
        for controller in self.controllers:
            for process in controller.children:
                process.wait(timeout=5)

    def configure(self, **control):
        self.control.write_text(json.dumps(control))
        self.config_path.write_text(json.dumps(self.raw_config))
        return load_config(self.config_path)

    def controller(self, config=None):
        controller = Controller(config or self.configure())
        self.controllers.append(controller)
        return controller

    def until(self, controller, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("controller deadline exceeded: " + json.dumps(controller.state))
            active = controller.tick()
            if not active and not predicate():
                self.fail("controller stopped unexpectedly: " + json.dumps(controller.state))
            time.sleep(0.01)

    def finish(self, controller):
        self.until(controller, lambda: controller.state["status"] in ("completed", "blocked"))

    def trace(self):
        path = self.root / "invocations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def evaluations(self, role=None):
        return [item for item in self.trace() if item["phase"] == "evaluate"
                and (role is None or item["role"] == role)]

    def cli_environment(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        return environment

    def cli(self, command):
        return subprocess.run([sys.executable, "-m", "rsi_service", command, "--config", str(self.config_path)],
                              env=self.cli_environment(), capture_output=True, text=True, timeout=5)

    def start_service(self):
        process = subprocess.Popen([sys.executable, "-m", "rsi_service", "serve", "--config", str(self.config_path)],
                                   env=self.cli_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.services.append(process)
        return process

    def wait_baseline_worker(self, service):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if service.poll() is not None:
                self.fail("service exited before evaluation: " + str(service.returncode))
            state_path = self.state_dir / "state.json"
            if state_path.exists():
                state = read_json(state_path)
                current = None if state["current"] is None else state["cycles"][state["current"]]
                if current and current["phase"] == "evaluate_baseline" and current.get("job"):
                    job_dir = Path(current["job"]["dir"])
                    if worker_busy(job_dir) and len(self.evaluations("baseline")) == 1:
                        return job_dir
            time.sleep(0.02)
        self.fail("long evaluation never started")

    def test_promoted_skill_is_used_by_next_run_and_baseline_is_reused(self):
        controller = self.controller()
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["status"], "completed")
        self.assertEqual([cycle["status"] for cycle in controller.state["cycles"]], ["promoted", "promoted"])
        runs = [item for item in self.trace() if item["phase"] == "run"]
        self.assertEqual([item["level"] for item in runs], [0, 1])
        self.assertEqual(runs[1]["digest"], controller.state["cycles"][0]["candidate"])
        self.assertEqual(len(self.evaluations("baseline")), 1)
        self.assertEqual(len(self.evaluations("candidate")), 2)
        self.assertNotEqual(controller.state["active_version"], original)
        self.assertEqual((self.skill / "notes.md").read_text(), "0", "source skill must not be overwritten")
        self.assertEqual((controller.version(controller.state["active_version"]) / "notes.md").read_text(), "2")

    def test_protected_diff_is_rejected_before_any_validation(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(improvement="protected"))
        original = controller.state["active_version"]
        self.finish(controller)
        cycle = controller.state["cycles"][0]
        self.assertEqual(cycle["status"], "rejected")
        self.assertIn("protected", cycle["reason"])
        self.assertEqual(controller.state["active_version"], original)
        self.assertEqual([item["phase"] for item in self.trace()], ["run", "improve"])

    def test_unchanged_candidate_never_spends_validation_budget(self):
        controller = self.controller(self.configure(improvement="none"))
        self.finish(controller)
        self.assertEqual([cycle["status"] for cycle in controller.state["cycles"]], ["no_change", "no_change"])
        self.assertFalse(any(item["phase"] in ("quick", "evaluate") for item in self.trace()))

    def test_quick_failure_prevents_expensive_evaluation(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(quick="fail"))
        self.finish(controller)
        self.assertEqual(controller.state["cycles"][0]["status"], "rejected")
        self.assertEqual(self.evaluations(), [])

    def test_failed_independent_evaluator_cannot_promote(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(evaluation="fail"))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["cycles"][0]["status"], "rejected")
        self.assertEqual(controller.state["active_version"], original)

    def test_candidate_can_repair_a_measured_failing_baseline(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(baseline="fail"))
        original = controller.state["active_version"]
        self.finish(controller)
        cycle = controller.state["cycles"][0]
        self.assertEqual(cycle["status"], "promoted")
        self.assertIs(cycle["baseline_evaluation"]["result"]["passed"], False)
        self.assertIs(cycle["candidate_evaluation"]["result"]["passed"], True)
        self.assertNotEqual(controller.state["active_version"], original)

    def test_failing_baseline_still_requires_measurement_evidence(self):
        controller = self.controller(self.configure(baseline="no_evidence"))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("evidence", controller.state["reason"])
        self.assertEqual(controller.state["active_version"], original)
        self.assertEqual(self.evaluations("candidate"), [])

    def test_malformed_baseline_pass_status_is_not_treated_as_measured_failure(self):
        controller = self.controller(self.configure(baseline="malformed"))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertEqual(controller.state["active_version"], original)
        self.assertEqual(self.evaluations("candidate"), [])

    def test_aggregate_gain_does_not_hide_case_regression(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(evaluation="regression"))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["cycles"][0]["status"], "rejected")
        self.assertIn("regressed", controller.state["cycles"][0]["reason"])
        self.assertEqual(controller.state["active_version"], original)

    def test_no_measured_gain_is_rejected_and_baseline_cache_survives_rejection(self):
        controller = self.controller(self.configure(evaluation="no_gain"))
        self.finish(controller)
        self.assertEqual([cycle["status"] for cycle in controller.state["cycles"]], ["rejected", "rejected"])
        self.assertEqual(len(self.evaluations("baseline")), 1)
        self.assertEqual(len(self.evaluations("candidate")), 2)

    def test_missing_case_is_rejected(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(evaluation="missing_case"))
        self.finish(controller)
        self.assertIn("case sets differ", controller.state["cycles"][0]["reason"])
        self.assertEqual(controller.state["cycles"][0]["status"], "rejected")

    def test_input_drift_mid_cycle_blocks_without_dispatching_next_phase(self):
        controller = self.controller()
        self.until(controller, lambda: controller.current() is not None and controller.current()["phase"] == "improve")
        original = controller.state["active_version"]
        self.script.write_text(self.script.read_text() + "\n# evaluator changed\n")
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("inputs changed", controller.state["reason"])
        self.assertEqual(controller.state["active_version"], original)
        self.assertEqual([item["phase"] for item in self.trace()], ["run"])

    def test_changed_workload_between_cycles_invalidates_baseline_cache(self):
        controller = self.controller(self.configure(evaluation="no_gain"))
        self.until(controller, lambda: len(controller.state["cycles"]) == 1 and controller.current() is None)
        self.script.write_text(self.script.read_text() + "\n# new evaluator revision\n")
        self.finish(controller)
        self.assertEqual(controller.state["status"], "completed")
        self.assertEqual(len(self.evaluations("baseline")), 2)

    def test_quick_evidence_tampering_prevents_promotion(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller()
        original = controller.state["active_version"]
        self.until(controller, lambda: controller.current() is not None and
                   controller.current()["phase"] == "evaluate_candidate")
        record = controller.current()["quick_evaluation"]
        evidence = next(Path(path) for path in record["artifacts"] if Path(path).name == "measurement.txt")
        evidence.write_text("tampered measurement")
        self.finish(controller)
        self.assertEqual(controller.state["cycles"][0]["status"], "rejected")
        self.assertIn("evidence changed", controller.state["cycles"][0]["reason"])
        self.assertEqual(controller.state["active_version"], original)

    def test_tampered_cached_baseline_is_remeasured(self):
        controller = self.controller(self.configure(evaluation="no_gain"))
        self.until(controller, lambda: len(controller.state["cycles"]) == 1 and controller.current() is None)
        record = controller.state["cycles"][0]["baseline_evaluation"]
        evidence = next(Path(path) for path in record["artifacts"] if Path(path).name == "measurement.txt")
        evidence.write_text("tampered cached measurement")
        self.finish(controller)
        self.assertEqual(len(self.evaluations("baseline")), 2)

    def test_expired_baseline_is_remeasured(self):
        self.raw_config["validation"]["baseline_cache_ttl_seconds"] = 0.02
        controller = self.controller(self.configure(evaluation="no_gain"))
        self.until(controller, lambda: len(controller.state["cycles"]) == 1 and controller.current() is None)
        time.sleep(0.03)
        self.finish(controller)
        self.assertEqual(len(self.evaluations("baseline")), 2)

    def test_receipt_age_starts_at_worker_completion_not_later_controller_poll(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller()
        self.until(controller, lambda: controller.current() is not None and
                   controller.current()["phase"] == "evaluate_baseline" and controller.current().get("job"))
        job = controller.current()["job"]
        status_path = Path(job["dir"]) / "status.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if status_path.exists() and read_json(status_path)["state"] == "completed":
                break
            time.sleep(0.01)
        else:
            self.fail("baseline evaluator did not finish")
        finished_at = read_json(status_path)["finished_at"]
        time.sleep(0.1)  # No controller ticks while the completed worker awaits collection.
        controller.tick()
        self.assertEqual(controller.current()["baseline_evaluation"]["at"], finished_at)
        self.assertGreater(time.time() - controller.current()["baseline_evaluation"]["at"], 0.09)

    def test_modified_managed_snapshot_blocks_before_next_job(self):
        controller = self.controller()
        self.until(controller, lambda: controller.current() is not None and controller.current()["phase"] == "improve")
        (controller.version(controller.state["active_version"]) / "notes.md").write_text("99")
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("snapshot was modified", controller.state["reason"])
        self.assertEqual([item["phase"] for item in self.trace()], ["run"])

    def test_configuration_cannot_silently_change_an_existing_experiment(self):
        config = self.configure()
        self.controller(config)
        self.raw_config["task"] = "different task"
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.controller(self.configure())

    def test_malformed_run_result_blocks_durably_without_promoting(self):
        controller = self.controller(self.configure(malformed_phase="run"))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("JSON object", controller.state["reason"])
        self.assertEqual(controller.state["active_version"], original)
        restarted = self.controller(controller.config)
        self.assertFalse(restarted.tick())
        self.assertEqual([item["phase"] for item in self.trace()], ["run"])

    def test_malformed_checker_result_blocks_durably_without_promoting(self):
        controller = self.controller(self.configure(malformed_phase="quick"))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("JSON object", controller.state["reason"])
        self.assertEqual(controller.state["active_version"], original)
        restarted = self.controller(controller.config)
        self.assertFalse(restarted.tick())
        self.assertEqual(self.evaluations(), [])

    def test_operational_controls_work_when_source_and_evaluator_are_missing(self):
        controller = self.controller(self.configure(evaluation_delay=5))
        self.until(controller, lambda: controller.current() is not None and
                   controller.current()["phase"] == "evaluate_baseline" and len(self.evaluations("baseline")) == 1)
        job_dir = Path(controller.current()["job"]["dir"])
        self.skill.rename(self.root / "disconnected-skill")
        self.script.unlink()
        for command in ("status", "events", "stop", "cancel"):
            with self.subTest(command=command):
                response = self.cli(command)
                self.assertEqual(response.returncode, 0, response.stderr)
        self.assertTrue((self.state_dir / "stop.request").exists())
        self.assertTrue((job_dir / "cancel").exists())
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn(str(self.script), controller.state["reason"])
        # Recovery likewise uses persisted config and remains possible after the worker exits.
        deadline = time.monotonic() + 5
        while worker_busy(job_dir) and time.monotonic() < deadline:
            time.sleep(0.01)
        response = self.cli("recover")
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(read_json(self.state_dir / "state.json")["cycles"][0]["status"], "abandoned")

    def test_resume_reattaches_without_source_then_blocks_on_missing_evaluation_input(self):
        self.raw_config["limits"]["max_cycles"] = 1
        self.configure(evaluation_delay=1.5)
        first = self.start_service()
        job_dir = self.wait_baseline_worker(first)
        worker_pid = read_json(job_dir / "status.json")["worker_pid"]
        self.skill.rename(self.root / "disconnected-skill")
        self.script.unlink()
        response = self.cli("stop")
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(first.wait(timeout=5), 0)
        self.assertTrue(worker_busy(job_dir))
        second = self.start_service()
        self.assertEqual(second.wait(timeout=10), 2)
        final = read_json(self.state_dir / "state.json")
        self.assertEqual(final["status"], "blocked")
        self.assertIn(str(self.script), final["reason"])
        self.assertEqual(read_json(job_dir / "status.json")["worker_pid"], worker_pid)
        self.assertEqual(final["cycles"][0]["job"]["completion"]["state"], "completed")
        self.assertEqual(len(self.evaluations("baseline")), 1)
        self.assertEqual(len(self.evaluations("candidate")), 0)

    def test_state_directory_cannot_overlap_cli_workspace_or_skill(self):
        for location in (self.workspace, self.workspace / "state", self.skill / "state", self.root):
            with self.subTest(location=location):
                self.raw_config["state_dir"] = str(location)
                with self.assertRaisesRegex(ValueError, "state_dir must be separate"):
                    self.configure()

    def test_controller_lock_prevents_parallel_dispatchers(self):
        with controller_lock(self.state_dir):
            with self.assertRaisesRegex(ValueError, "already running"):
                with controller_lock(self.state_dir):
                    self.fail("second controller acquired the lock")
        with controller_lock(self.state_dir):
            pass

    def test_uncertain_worker_launch_is_not_replayed_after_restart(self):
        config = self.configure()
        controller = self.controller(config)
        controller.tick()  # Create the first cycle without launching a command.
        with mock.patch("rsi_service.controller.subprocess.Popen", side_effect=OSError("launch interrupted")):
            controller.tick()
        self.assertEqual(controller.state["status"], "blocked")
        # Model a controller crash after durable launch intent, before worker startup.
        state = read_json(self.state_dir / "state.json")
        state["status"] = "running"
        state["cycles"][0]["job"]["submitted_at"] = time.time() - 30
        atomic_json(self.state_dir / "state.json", state)
        restarted = self.controller(config)
        with mock.patch("rsi_service.controller.subprocess.Popen") as launch:
            restarted.tick()
            restarted.tick()
            launch.assert_not_called()
        self.assertEqual(restarted.state["status"], "blocked")
        self.assertIn("launch uncertain", restarted.state["reason"])
        self.assertEqual(self.trace(), [])
        restarted.recover()
        self.assertEqual(restarted.state["cycles"][0]["status"], "abandoned")

    def test_service_restart_reattaches_live_expensive_evaluation_once(self):
        self.raw_config["limits"]["max_cycles"] = 1
        self.configure(evaluation_delay=1.5)
        first = self.start_service()
        job_dir = self.wait_baseline_worker(first)
        worker_pid = read_json(job_dir / "status.json")["worker_pid"]
        first.send_signal(signal.SIGTERM)
        self.assertEqual(first.wait(timeout=5), 0)
        paused = read_json(self.state_dir / "state.json")
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(worker_busy(job_dir), "stopping the controller must not kill long validation")
        second = self.start_service()
        self.assertEqual(second.wait(timeout=15), 0)
        final = read_json(self.state_dir / "state.json")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["cycles"][0]["status"], "promoted")
        self.assertEqual(read_json(job_dir / "status.json")["worker_pid"], worker_pid)
        self.assertEqual(len(self.evaluations("baseline")), 1)
        self.assertEqual(len(self.evaluations("candidate")), 1)


if __name__ == "__main__":
    unittest.main()
