"""Behavioral autonomous-loop tests using real detached workers and commands.

The fixture substitutes deterministic model responses, never the controller or
worker. Its trial commands really fail/succeed, and the skill changes how many
failed commands are attempted. Judge reports read those recorded measurements.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from rsi_service.autonomous_controller import AutonomousController
from rsi_service.config import load_config
from rsi_service.controller import worker_busy
from rsi_service.storage import read_json


FIXTURE = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from rsi_service.autonomous_models import validate_result

root = Path(__file__).parent
control = json.loads((root / "control.json").read_text())
context = json.loads(Path(os.environ["RSI_CONTEXT_FILE"]).read_text())
phase = context["phase"]
role = context.get("trial_role")
cycle = context["cycle_id"]
output = Path(context["result_path"])
level = int((Path(context["skill_dir"]) / "notes.md").read_text())
bundle = context.get("session_bundle", {})
refs = bundle.get("event_ids", [])[-1:]
trace = {"phase": phase, "role": role, "cycle": cycle, "level": level,
         "context_path": os.environ["RSI_CONTEXT_FILE"], "workspace": context["workspace"],
         "source": bundle.get("source", {}).get("sha256"),
         "memory": context.get("memory"), "chunk": None}

if phase == "analyze":
    chunk = context["analysis_chunk"]
    payload = json.loads(Path(chunk["path"]).read_text())
    trace["chunk"] = chunk["id"]
    trace["read_event_ids"] = list(dict.fromkeys(part["event_id"] for part in payload["events"]))
    trace["read_characters"] = sum(len(part.get("text", part.get("preview", ""))) for part in payload["events"])
    trace["analysis_view"] = payload.get("view", "full")
    result = {"summary": "Read all raw event fragments in the assigned chunk.", "incidents": [{
        "id": "incident-" + chunk["id"], "category": "skill_guidance",
        "description": "The trace contains repeated failed preparation attempts.",
        "evidence_refs": refs, "hypothesis": "A recovery note can prevent a failed attempt.",
        "suggested_change": "Improve the notes while retaining correctness checks."}]}
elif phase == "plan":
    result = {"decision": "improve", "summary": "Test one bounded recovery improvement.",
              "hypothesis": "The recovery note reduces avoidable failed commands.",
              "expected_effect": "Fewer failures with unchanged correct final output.",
              "target_paths": ["notes.md"], "evidence_refs": refs,
              "validation_focus": ["Count real failed commands and check the output."]}
elif phase == "improve":
    counterexample = any(m.get("status") == "rejected" for m in context["memory"])
    new_level = level + (2 if counterexample else 1)
    (Path(context["candidate_dir"]) / "notes.md").write_text(str(new_level))
    result = {"summary": "Add a recovery note informed by the observed counterexample." if counterexample else "Add one recovery note.",
              "changed": True}
elif phase == "run":
    time.sleep(control.get("run_delay", 0))
    def emit(value):
        print(json.dumps(value), flush=True)
    emit({"type": "thread.started", "thread_id": cycle + "-" + role})
    failures = max(0, 3 - level)
    if control.get("reject_first") and cycle == "a0001" and role == "candidate":
        failures = 5
    observed = 0
    for index in range(failures + 1):
        status = 1 if index < failures else 0
        command = [sys.executable, "-c", "raise SystemExit(%d)" % status]
        item_id = "command-%d" % index
        emit({"type": "item.started", "item": {"type": "command_execution", "id": item_id,
              "command": "fixture preparation", "status": "in_progress"}})
        child = subprocess.run(command, capture_output=True, text=True)
        observed += int(child.returncode != 0)
        emit({"type": "item.completed", "item": {"type": "command_execution", "id": item_id,
              "command": "fixture preparation", "exit_code": child.returncode,
              "status": "failed" if child.returncode else "completed", "aggregated_output": child.stdout}})
    workspace = Path(context["workspace"])
    (workspace / "measurement.json").write_text(json.dumps({"failed_attempts": observed, "correct": True,
        "skill_digest": context["skill_digest"], "source_input": (workspace / "input.txt").read_text()}))
    (workspace / "trial-only.txt").write_text("This must never appear in the user's original workspace.")
    emit({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}})
    result = {"outcome": "pending" if control.get("pending") == role else "signed",
              "summary": "Executed actual fixture preparation commands and saved measurements.",
              "evidence": ["measurement.json"]}
elif phase == "judge":
    # The transcript now includes independent checks after the execution
    # result. Select records by type; position is not a semantic contract.
    events = [json.loads(line) for line in Path(bundle["events_path"]).read_text().splitlines()]
    executions = [event for event in events if event["record"].get("type") == "rsi.execution_result"]
    checks = [event for event in events if event["record"].get("type") == "rsi.independent_checks"]
    assert len(executions) == len(checks) == 1
    copied = next(item["path"] for item in executions[0]["record"]["evidence"]
                  if Path(item["original_path"]).name == "measurement.json")
    measurement = json.loads(Path(copied).read_text())
    commands = [event for event in events if event["record"].get("type") == "item.completed"
                and event["record"].get("item", {}).get("type") == "command_execution"]
    assert commands
    assert sum(event["record"]["item"]["exit_code"] != 0 for event in commands) == measurement["failed_attempts"]
    refs = [executions[0]["event_id"], checks[0]["event_id"]]
    trace["checks_event_ref"] = checks[0]["event_id"]
    trace["execution_event_ref"] = executions[0]["event_id"]
    gate = "unknown" if control.get("unknown") and role == "candidate" else "pass"
    quality = None if control.get("null_metric") and role == "candidate" else float(measurement["correct"])
    result = {"summary": "Judge read the trial's actual measurements.",
              "gates": [{"id": "correct", "status": gate, "evidence_refs": refs,
                         "reason": "The measurement contains the actual final-output check."}],
              "metrics": [{"name": "failures", "value": measurement["failed_attempts"], "evidence_refs": refs,
                           "reason": "Counted failed child commands."},
                          {"name": "quality", "value": quality, "evidence_refs": refs,
                           "reason": "Recorded final-output correctness, or missing evidence."}],
              "limitations": []}
elif phase == "reflect":
    promoted = context["decision"]["promote"]
    result = {"summary": "Retain the observed outcome and counterexample.",
              "hypothesis_status": "supported" if promoted else "refuted",
              "explanation": "The paired runs changed the measured number of failed commands.",
              "next_direction": "Use this decision and the actual candidate trace in the next hypothesis.",
              "evidence_refs": refs}
else:
    raise RuntimeError("unexpected fixture phase")

validate_result(phase, result, context)
with (root / "invocations.jsonl").open("a") as stream:
    stream.write(json.dumps(trace) + "\n")
output.write_text(json.dumps(result))
'''


OPERATOR_CHECK = r'''
import json
import sys
from pathlib import Path

root = Path(__file__).parent
control = json.loads((root / "control.json").read_text())
workspace = Path.cwd()
measurement = json.loads((workspace / "measurement.json").read_text())
assert measurement["correct"] is True
assert measurement["source_input"] == "fixed input\n"
if control.get("operator_fail") and workspace.parent.name == "candidate":
    print("Independent check rejected this candidate despite the model claiming pass.")
    raise SystemExit(1)
print("Independent output check passed.")
'''


class AutonomousControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rsi-autonomous-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace, self.skill = self.root / "workspace", self.root / "skill"
        self.workspace.mkdir()
        self.skill.mkdir()
        (self.workspace / "input.txt").write_text("fixed input\n")
        (self.skill / "SKILL.md").write_text("Read notes.md. Never weaken the final output check.\n")
        (self.skill / "notes.md").write_text("0")
        self.source = self.root / "seed.jsonl"
        self.write_seed()
        self.script, self.check_script = self.root / "fixture.py", self.root / "operator_check.py"
        self.script.write_text(FIXTURE)
        self.check_script.write_text(OPERATOR_CHECK)
        self.control, self.config_path = self.root / "control.json", self.root / "autonomous.json"
        self.state_dir = self.root / "state"
        self.raw_config = {
            "schema_version": 1, "state_dir": str(self.state_dir), "workspace": str(self.workspace),
            "skill_source": str(self.skill), "task": "Improve preparation while preserving correct fixture output.",
            "allowed_paths": ["notes.md"], "protected_paths": [],
            "objective": {"description": "Reduce real failed attempts without changing correctness.",
                          "gates": [{"id": "correct", "description": "The final output is correct."}],
                          "metrics": [{"name": "failures", "direction": "minimize", "source": "failed_tools",
                                       "min_gain": 1, "max_regression": 0},
                                      {"name": "quality", "direction": "maximize", "source": "judge",
                                       "min_gain": 0.1, "max_regression": 0}]},
            "autonomy": {"source": {"path": str(self.source)}, "chunk_chars": 24000, "analysis_view": "full",
                         "max_model_calls": 100, "max_validation_runs": 8,
                         "evidence_inputs": [str(self.script), str(self.check_script), str(self.control)],
                         "checks": [{"id": "output_check", "argv": [sys.executable, str(self.check_script)],
                                     "timeout_seconds": 5}]},
            "steps": {phase: {"adapter": "command", "argv": ["{python}", str(self.script)]}
                      for phase in ("analyze", "plan", "improve", "run", "judge", "reflect")},
            "limits": {"max_cycles": 2, "max_no_change_cycles": 3, "max_wall_seconds": 60,
                       "poll_seconds": 0.02, "timeouts": {p: 10 for p in
                       ("analyze", "plan", "improve", "run", "judge", "reflect", "quick", "checks")}},
        }
        self.controllers, self.services = [], []
        self.addCleanup(self.cleanup_workers)

    def write_seed(self, content="A prior validation retried a failed preparation command."):
        self.source.write_text("\n".join(json.dumps(row) for row in [
            {"type": "session_meta", "payload": {"id": "historical-seed"}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "historical-call", "arguments": "{}", "name": "exec_command"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "historical-call", "output": '{"exit_code":1}'}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": content}},
        ]) + "\n")

    def configure(self, **control):
        self.control.write_text(json.dumps(control))
        self.config_path.write_text(json.dumps(self.raw_config))
        return load_config(self.config_path)

    def controller(self, config=None):
        controller = AutonomousController(config or self.configure())
        self.controllers.append(controller)
        return controller

    def until(self, controller, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("controller timeout: " + json.dumps({k: controller.state.get(k) for k in ("status", "reason")}))
            active = controller.tick()
            if not active and not predicate():
                self.fail("controller stopped: " + json.dumps({k: controller.state.get(k) for k in ("status", "reason")}))
            time.sleep(0.008)

    def finish(self, controller):
        self.until(controller, lambda: controller.state["status"] in ("completed", "blocked"))

    def trace(self):
        path = self.root / "invocations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def cleanup_workers(self):
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
            time.sleep(0.02)
        for controller in self.controllers:
            for process in controller.children:
                process.wait(timeout=5)

    def test_two_complete_cycles_start_with_seed_analysis_then_learn_from_actual_candidate(self):
        controller = self.controller()
        self.finish(controller)
        self.assertEqual(controller.state["status"], "completed", controller.state.get("reason"))
        first, second = controller.state["cycles"]
        self.assertEqual([first["status"], second["status"]], ["promoted", "promoted"])
        phases = [job["phase"] for job in first["jobs"]]
        self.assertEqual(phases, ["analyze", "plan", "improve", "quick", "baseline_run", "baseline_checks",
                                 "baseline_judge", "candidate_run", "candidate_checks", "candidate_judge", "reflect"])
        self.assertEqual(second["source_session"]["source"]["sha256"], first["trials"]["candidate"]["session"]["source"]["sha256"])
        self.assertNotEqual(second["source_session"]["source"]["sha256"], controller.autonomous["initial_session"]["source"]["sha256"])
        later_analysis = next(row for row in self.trace() if row["cycle"] == second["id"] and row["phase"] == "analyze")
        self.assertTrue(later_analysis["memory"][0]["decision"]["promote"])
        self.assertEqual(later_analysis["memory"][0]["reflection"]["hypothesis_status"], "supported")
        self.assertEqual(controller.state["active_version"], second["candidate"])
        self.assertEqual((controller.version(second["candidate"]) / "notes.md").read_text(), "2")
        self.assertEqual(controller.autonomous["validation_runs"], 4)
        self.assertEqual(len([row for row in self.trace() if row["phase"] == "run"]), 4)
        self.assertEqual((self.skill / "notes.md").read_text(), "0")
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), ["input.txt"])
        self.assertEqual((self.workspace / "input.txt").read_text(), "fixed input\n")
        for run in [row for row in self.trace() if row["phase"] == "run"]:
            context = read_json(run["context_path"])
            self.assertEqual(context["session_bundle"], {})
            self.assertEqual(context["analyses"], [])
            self.assertEqual(context["memory"], [])
            self.assertEqual(context["memory_archive"], [])
            self.assertIsNone(context["hypothesis"])
            self.assertIsNone(context["analysis_chunk"])
            self.assertEqual(context["candidate_dir"], context["skill_dir"])
            self.assertEqual(context["baseline_dir"], context["skill_dir"])
        for judge in [row for row in self.trace() if row["phase"] == "judge"]:
            context = read_json(judge["context_path"])
            result = read_json(context["result_path"])
            self.assertIn(judge["checks_event_ref"], context["session_bundle"]["event_ids"])
            self.assertIn(judge["checks_event_ref"], result["gates"][0]["evidence_refs"])
            self.assertNotEqual(judge["checks_event_ref"], judge["execution_event_ref"])

    def test_rejected_candidate_feedback_preserves_active_and_guides_next_attempt(self):
        controller = self.controller(self.configure(reject_first=True))
        original = controller.state["active_version"]
        self.until(controller, lambda: len(controller.autonomous["memory"]) == 1)
        first = controller.state["cycles"][0]
        self.assertEqual(first["status"], "rejected")
        self.assertEqual(controller.state["active_version"], original)
        self.assertFalse(first["decision"]["promote"])
        self.assertEqual(first["reflection"]["hypothesis_status"], "refuted")
        self.finish(controller)
        second = controller.state["cycles"][1]
        self.assertEqual(second["baseline"], original)
        self.assertEqual(second["source_session"]["source"]["sha256"], first["trials"]["candidate"]["session"]["source"]["sha256"])
        self.assertEqual(second["status"], "promoted")
        improvement = next(row for row in self.trace() if row["phase"] == "improve" and row["cycle"] == "a0002")
        self.assertEqual(improvement["memory"][0]["status"], "rejected")

    def test_unknown_gate_never_promotes(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(unknown=True))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["cycles"][0]["status"], "rejected")
        self.assertEqual(controller.state["active_version"], original)

    def test_null_measurement_never_promotes(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(null_metric=True))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["cycles"][0]["status"], "rejected")
        self.assertEqual(controller.state["active_version"], original)

    def test_validation_budget_does_not_start_half_a_comparison(self):
        self.raw_config["autonomy"]["max_validation_runs"] = 1
        controller = self.controller()
        self.finish(controller)
        self.assertEqual(controller.autonomous["validation_runs"], 0)
        self.assertFalse([row for row in self.trace() if row["phase"] == "run"])

    def test_model_budget_does_not_start_half_a_comparison(self):
        self.raw_config["autonomy"]["max_model_calls"] = 4
        controller = self.controller()
        self.finish(controller)
        self.assertEqual(controller.autonomous["validation_runs"], 0)
        self.assertFalse([row for row in self.trace() if row["phase"] == "run"])
        self.assertLessEqual(controller.autonomous["model_calls"], 4)

    def test_every_source_chunk_is_read_before_planning(self):
        self.raw_config["limits"]["max_cycles"] = 1
        self.raw_config["autonomy"]["chunk_chars"] = 1200
        self.write_seed("A long raw trace with preserved details. " * 100)
        controller = self.controller()
        self.finish(controller)
        cycle = controller.state["cycles"][0]
        expected = cycle["source_session"]["chunks"]
        self.assertGreater(len(expected), 2)
        analyzed = [row for row in self.trace() if row["phase"] == "analyze"]
        self.assertEqual([row["chunk"] for row in analyzed], [chunk["id"] for chunk in expected])
        self.assertEqual([row["read_event_ids"] for row in analyzed], [chunk["event_ids"] for chunk in expected])
        self.assertTrue(all(row["read_characters"] for row in analyzed))
        phases = [row["phase"] for row in self.trace()]
        self.assertEqual(phases[:len(expected) + 1], ["analyze"] * len(expected) + ["plan"])

    def test_timeline_covers_two_actual_sessions_and_rejects_tampered_next_source(self):
        self.raw_config["autonomy"].update(analysis_view="timeline", timeline_chunk_chars=1800,
                                            timeline_preview_chars=80)
        # Stop after two completed rounds, then exercise the next-round input
        # guard before any third-round model execution is allowed.
        self.raw_config["limits"]["max_cycles"] = 3
        controller = self.controller()
        self.until(controller, lambda: len(controller.autonomous["memory"]) == 2)
        first, second = controller.state["cycles"]
        self.assertEqual([first["status"], second["status"]], ["promoted", "promoted"])
        for cycle in (first, second):
            self.assertEqual(cycle["session_index"]["view"], "timeline")
            self.assertEqual(cycle["session_index"]["events"], cycle["source_session"]["stats"]["events"])
            analyzed = [row for row in self.trace() if row["cycle"] == cycle["id"] and row["phase"] == "analyze"]
            self.assertTrue(all(row["analysis_view"] == "timeline" for row in analyzed))
            self.assertEqual([row["chunk"] for row in analyzed], [chunk["id"] for chunk in cycle["analysis_chunks"]])
            covered = [event_id for row in analyzed for event_id in row["read_event_ids"]]
            self.assertEqual(covered, cycle["source_session"]["event_ids"])
            self.assertEqual(len(covered), len(set(covered)))
        self.assertEqual(second["source_session"]["source"]["sha256"], first["trials"]["candidate"]["session"]["source"]["sha256"])
        self.assertNotEqual(first["source_session"]["source"]["sha256"], second["source_session"]["source"]["sha256"])
        active = controller.state["active_version"]
        calls_before = controller.autonomous["model_calls"]
        with Path(controller.autonomous["latest_session"]["events_path"]).open("a") as stream:
            stream.write('{"injected":"not an actual execution event"}\n')
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("session evidence changed", controller.state["reason"])
        self.assertEqual(controller.state["active_version"], active)
        self.assertEqual(controller.autonomous["model_calls"], calls_before)
        self.assertEqual(len(controller.state["cycles"]), 2)

    def test_insufficient_analysis_and_planning_budget_spends_no_model_calls(self):
        self.raw_config["autonomy"].update(analysis_view="full", chunk_chars=1000, max_model_calls=2)
        self.write_seed("Detailed historical evidence requires many full-content chunks. " * 150)
        controller = self.controller()
        self.assertGreater(len(controller.autonomous["initial_session"]["chunks"]), 2)
        self.finish(controller)
        self.assertEqual(controller.state["status"], "completed")
        self.assertIn("analysis requires", controller.state["reason"])
        self.assertEqual(controller.autonomous["model_calls"], 0)
        self.assertEqual(controller.autonomous["validation_runs"], 0)
        self.assertEqual(self.trace(), [])

    def test_tampered_job_context_blocks_before_it_can_change_the_objective(self):
        controller = self.controller()
        original = controller.state["active_version"]
        fixed_objective = json.dumps(controller.config["objective"], sort_keys=True)
        objective_digest = controller.autonomous["objective_digest"]
        self.until(controller, lambda: bool(controller.current()) and bool(controller.current()["job"]))
        job = controller.current()["job"]
        deadline = time.monotonic() + 10
        while True:
            status_path = Path(job["dir"]) / "status.json"
            if status_path.exists() and read_json(status_path).get("state") == "completed":
                break
            if time.monotonic() >= deadline:
                self.fail("analysis worker did not complete")
            time.sleep(0.01)
        context_path = Path(job["dir"]) / "context.json"
        context = read_json(context_path)
        context["objective"]["metrics"][0]["min_gain"] = 0
        context["objective"]["gates"][0]["description"] = "Forged weaker gate after dispatch."
        context_path.write_text(json.dumps(context))
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("job inputs changed", controller.state["reason"])
        self.assertEqual(controller.state["active_version"], original)
        self.assertEqual(controller.autonomous["objective_digest"], objective_digest)
        self.assertEqual(json.dumps(controller.config["objective"], sort_keys=True), fixed_objective)
        self.assertEqual(controller.autonomous["validation_runs"], 0)

    def test_tampered_frozen_source_is_blocked_before_any_execution(self):
        controller = self.controller()
        bundle = controller.autonomous["initial_session"]
        Path(bundle["chunks"][0]["path"]).write_text("tampered")
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("session", controller.state["reason"])
        self.assertEqual(self.trace(), [])

    def test_tampered_collected_trial_evidence_prevents_promotion(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller()
        original = controller.state["active_version"]
        self.until(controller, lambda: bool(controller.current()) and controller.current()["phase"] == "candidate_judge" and controller.current()["job"] is None)
        trial = controller.current()["trials"]["candidate"]
        copied = Path(trial["evidence"]["evidence"][0]["path"])
        # Keep it parseable so rejection proves immutable evidence checking,
        # rather than merely making the fixture judge crash on invalid JSON.
        measurement = read_json(copied)
        measurement["added_after_collection"] = True
        copied.write_text(json.dumps(measurement))
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertEqual(controller.state["active_version"], original)
        self.assertIn("evidence", controller.state["reason"])

    def test_pending_validation_blocks_without_candidate_execution(self):
        controller = self.controller(self.configure(pending="baseline"))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("needs input", controller.state["reason"])
        self.assertEqual(controller.state["active_version"], original)
        self.assertEqual([row["role"] for row in self.trace() if row["phase"] == "run"], ["baseline"])

    def test_recreated_controller_reattaches_same_durable_worker_without_duplicate_run(self):
        self.raw_config["limits"]["max_cycles"] = 1
        config = self.configure(run_delay=0.5)
        first = self.controller(config)
        self.until(first, lambda: bool(first.current()) and first.current()["phase"] == "baseline_run"
                   and bool(first.current()["job"]) and worker_busy(first.current()["job"]["dir"]))
        job_id = first.current()["job"]["id"]
        replacement = self.controller(config)
        self.assertEqual(replacement.current()["job"]["id"], job_id)
        self.finish(replacement)
        self.assertEqual(replacement.state["cycles"][0]["status"], "promoted")
        runs = [row for row in self.trace() if row["phase"] == "run"]
        self.assertEqual([row["role"] for row in runs], ["baseline", "candidate"])
        self.assertEqual(replacement.autonomous["validation_runs"], 2)
        self.assertEqual(len([job for job in replacement.state["cycles"][0]["jobs"] if job["phase"] == "baseline_run"]), 1)

    def test_operator_failure_cannot_be_overridden_by_passing_judge(self):
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(operator_fail=True))
        original = controller.state["active_version"]
        self.finish(controller)
        cycle = controller.state["cycles"][0]
        self.assertEqual(cycle["trials"]["candidate"]["assessment"]["gates"][0]["status"], "pass")
        self.assertFalse(cycle["trials"]["candidate"]["checks"]["result"]["passed"])
        self.assertEqual(cycle["status"], "rejected")
        self.assertEqual(controller.state["active_version"], original)

    def test_execution_transcript_rejects_changed_collection_inputs(self):
        from rsi_service.trials import execution_transcript
        self.raw_config["limits"]["max_cycles"] = 1
        controller = self.controller()
        self.finish(controller)
        cycle = controller.state["cycles"][0]
        trial = cycle["trials"]["candidate"]
        job = next(job for job in cycle["jobs"] if job["phase"] == "candidate_run")
        unchanged = execution_transcript(job, trial["run_result"], trial["workspace"])
        self.assertEqual(unchanged["path"], trial["evidence"]["path"])
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            execution_transcript(job, dict(trial["run_result"], summary="Altered after collection"), trial["workspace"])


if __name__ == "__main__":
    unittest.main()
