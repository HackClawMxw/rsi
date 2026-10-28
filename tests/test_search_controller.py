"""Search integration regressions using actual worker processes, without LLMs."""
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

from rsi_service.adapters import prepare
from rsi_service.config import load_config
from rsi_service.controller import worker_busy
from rsi_service.search_controller import SearchController
from rsi_service.storage import read_json


HOOK = r'''
import json
import os
import time
from pathlib import Path

root = Path(__file__).parent
context = json.loads(Path(os.environ["RSI_CONTEXT_FILE"]).read_text())
control = json.loads((root / "control.json").read_text())
phase = context["phase"]
split = context.get("split")
role = context.get("evaluation_role")
level = int((Path(context["skill_dir"]) / "notes.md").read_text())
examples = context.get("examples", [])
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"phase": phase, "split": split, "role": role,
        "cycle": context["cycle_id"], "level": level, "digest": context["skill_digest"],
        "examples": [e["id"] for e in examples], "batch": context.get("batch_digest")}) + "\n")
output = Path(os.environ["RSI_RESULT_PATH"])
output.parent.mkdir(parents=True, exist_ok=True)
if phase == "improve":
    mode = control.get("improvement", "increment")
    candidate = Path(context["candidate_dir"])
    if mode == "protected":
        (candidate / "SKILL.md").write_text("weakened gate")
    elif mode == "duplicate":
        (candidate / "notes.md").write_text("0")
    elif mode != "none":
        (candidate / "notes.md").write_text(str(level + 1))
    result = {"summary": "fixture improvement", "changed": mode != "none"}
elif phase == "run":
    result = {"outcome": control.get("run_outcome", "signed"),
              "summary": "ran selected fixture version", "evidence": []}
else:
    (output.parent / "measurement.txt").write_text("measured level=%s split=%s" % (level, split))
    result = {"passed": not (phase == "quick" and control.get("quick_fail")),
              "evidence": ["measurement.txt"]}
    if phase == "evaluate":
        if split == control.get("delay_split") and role == control.get("delay_role"):
            time.sleep(control.get("delay_seconds", 0))
        value = level
        if split == "train" and role == "candidate" and control.get("screen_out"):
            value -= 1
        cases = {example["id"]: float(value) for example in examples}
        if split == "test" and role == "finalist" and control.get("test_fail"):
            result["passed"] = False
        if split == "validation" and role == "candidate" and control.get("specialist"):
            cases[examples[-1]["id"]] = -1.0
        result.update(skill_digest=context["skill_digest"],
                      validation_key=context["validation_key"], split=split,
                      batch_digest=context["batch_digest"], cases=cases,
                      score=sum(cases.values()) / len(cases))
        if split == "train":
            result["side_info"] = {e["id"]: {"Input": e["task"],
                "Feedback": "Measured failure needs improved notes", "Trace": "level=%s" % level}
                for e in examples}
        if split == control.get("bad_split", "train") and role == control.get("bad_role", "parent"):
            bad = control.get("bad")
            if bad == "case":
                result["cases"]["UNREQUESTED"] = result["cases"].pop(examples[0]["id"])
            elif bad == "side_info":
                result.pop("side_info", None)
            elif bad == "batch":
                result["batch_digest"] = "wrong-batch"
            elif bad == "score":
                result["score"] = 999
            elif bad == "oversized_integer":
                result["score"] = 10 ** 1000
            elif bad == "nonfinite_side_info":
                result["side_info"][examples[0]["id"]]["untrusted_detail"] = {"ratio": float("nan")}
            elif bad == "nonobject":
                result = []
output.write_text(json.dumps(result))
'''


class SearchControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rsi-search-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "target"
        self.skill = self.root / "skill"
        self.state_dir = self.root / "state"
        self.workspace.mkdir()
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text("fixed gate")
        (self.skill / "notes.md").write_text("0")
        self.script = self.root / "hook.py"
        self.script.write_text(HOOK)
        self.control = self.root / "control.json"
        self.control.write_text("{}")
        self.task_path = self.root / "tasks.json"
        self.tasks = {
            "train": [{"id": "train-%s" % i, "task": "training failure %s" % i,
                       "input": {"case_number": i, "trace": "input only available in task manifest"}}
                      for i in range(3)],
            "validation": [{"id": "val-%s" % i, "task": "validation example %s" % i} for i in range(2)],
            "test": [{"id": "HELDOUT-%s" % i, "task": "secret test task %s" % i} for i in range(2)],
        }
        self.task_path.write_text(json.dumps(self.tasks))
        self.config_path = self.root / "search.json"
        self.raw = {
            "schema_version": 1, "workspace": str(self.workspace), "skill_source": str(self.skill),
            "state_dir": str(self.state_dir), "task": "Learn from the selected training observations.",
            "allowed_paths": ["notes.md", "SKILL.md"], "protected_paths": [],
            "steps": {phase: {"adapter": "command", "argv": ["{python}", str(self.script)]}
                      for phase in ("run", "improve", "quick", "evaluate")},
            "validation": {"key": "fixed-machine-and-task-identity", "inputs": [str(self.script), str(self.control)],
                           "baseline_cache_ttl_seconds": 120, "min_gain": 0.1, "max_case_regression": 0},
            "limits": {"max_cycles": 2, "max_no_change_cycles": 3, "max_wall_seconds": 90,
                       "poll_seconds": 0.02,
                       "timeouts": {phase: 10 for phase in ("run", "improve", "quick", "evaluate")}},
            "optimizer": {"strategy": "pareto", "dataset": str(self.task_path), "seed": 42,
                          "train_batch_size": 2, "max_metric_calls": 60,
                          "max_full_evaluations": 12, "min_train_gain": 0.1},
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
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and any(worker_busy(job) for job in jobs):
            time.sleep(0.03)
        for controller in self.controllers:
            for process in controller.children:
                process.wait(timeout=5)

    def configure(self, **control):
        self.control.write_text(json.dumps(control))
        self.config_path.write_text(json.dumps(self.raw))
        return load_config(self.config_path)

    def controller(self, config=None):
        controller = SearchController(config or self.configure())
        self.controllers.append(controller)
        return controller

    def until(self, controller, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("controller deadline: " + json.dumps(controller.state))
            alive = controller.tick()
            if not alive and not predicate():
                self.fail("controller stopped: " + json.dumps(controller.state))
            time.sleep(0.01)

    def finish(self, controller):
        self.until(controller, lambda: controller.state["status"] in ("completed", "blocked"))

    def cycles(self, controller):
        return [c for c in controller.state["cycles"] if c["kind"] == "search"]

    def trace(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def evaluations(self, split=None, role=None):
        return [item for item in self.trace() if item["phase"] == "evaluate"
                and (split is None or item["split"] == split) and (role is None or item["role"] == role)]

    def stage_pending(self, controller, phase):
        cycle = controller.current()
        return cycle is not None and cycle["phase"] == phase and cycle["job"] is None

    def test_historical_parent_search_does_not_deploy_until_final_test(self):
        controller = self.controller()
        original = controller.state["active_version"]
        self.until(controller, lambda: len(self.cycles(controller)) == 2)
        first, second = self.cycles(controller)
        self.assertEqual(first["status"], "pooled")
        self.assertEqual(second["baseline"], first["candidate"])
        self.assertNotEqual(second["baseline"], original)
        self.assertEqual(controller.state["active_version"], original)
        self.assertEqual(self.evaluations("test"), [])
        self.finish(controller)
        self.assertEqual(controller.search["result"]["status"], "promoted")
        self.assertEqual(len(controller.search["pool"]), 3)
        self.assertEqual((controller.version(controller.state["active_version"]) / "notes.md").read_text(), "2")
        runs = [row for row in self.trace() if row["phase"] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["level"], 2)
        self.assertEqual(runs[0]["digest"], controller.search["finalist"])
        self.assertEqual((self.skill / "notes.md").read_text(), "0")
        calls = self.evaluations()
        self.assertEqual(controller.search["metric_calls"], sum(len(row["examples"]) for row in calls))
        self.assertEqual(controller.search["full_evaluations"], sum(row["split"] != "train" for row in calls))

    def test_training_screen_compares_same_batch_and_skips_full_candidate_evaluation(self):
        self.raw["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(screen_out=True))
        self.finish(controller)
        self.assertEqual(self.cycles(controller)[0]["status"], "screened_out")
        parent, candidate = self.evaluations("train")
        self.assertEqual(parent["examples"], candidate["examples"])
        self.assertEqual(parent["batch"], candidate["batch"])
        self.assertEqual(self.evaluations("validation", "candidate"), [])
        self.assertEqual(self.evaluations("test"), [])

    def test_reflection_context_and_prompt_do_not_contain_validation_or_test_examples(self):
        controller = self.controller()
        self.until(controller, lambda: self.stage_pending(controller, "improve"))
        cycle = controller.current()
        controller.tick()
        job = cycle["job"]
        context = read_json(Path(job["dir"]) / "context.json")
        payload = json.dumps(context)
        for example in self.tasks["validation"] + self.tasks["test"]:
            self.assertNotIn(example["id"], payload)
            self.assertNotIn(example["task"], payload)
        self.assertEqual({row["example_id"] for row in context["reflection_data"]},
                         {row["id"] for row in cycle["train_batch"]})
        selected = {row["id"]: row for row in cycle["train_batch"]}
        for row in context["reflection_data"]:
            self.assertEqual(row["input"], selected[row["example_id"]]["input"])
        reflection_dir = Path(context["run_dir"])
        self.assertEqual(reflection_dir.name, "reflection")
        self.assertEqual([p.name for p in reflection_dir.iterdir()], ["feedback.json"])
        self.assertNotIn("previous_result", context)
        codex_config = dict(controller.config, steps={"improve": {"adapter": "codex"}})
        synthetic_job = self.root / "prompt-check"
        context["result_path"] = str(synthetic_job / "output" / "result.json")
        prompt = prepare(codex_config, "improve", context, synthetic_job)["stdin"]
        self.assertIn("Do not inspect validation or held-out test feedback", prompt)
        self.assertNotIn("HELDOUT-", prompt)
        self.assertNotIn("secret test task", prompt)

    def test_holdout_failure_closes_search_without_retry_or_feedback_training(self):
        controller = self.controller(self.configure(test_fail=True))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.search["result"]["status"], "test_rejected")
        self.assertEqual(controller.state["active_version"], original)
        events = self.trace()
        first_test = next(i for i, row in enumerate(events) if row["split"] == "test")
        self.assertFalse(any(row["phase"] in ("improve", "run") for row in events[first_test:]))
        before = len(events)
        restarted = self.controller(controller.config)
        self.assertFalse(restarted.tick())
        self.assertEqual(len(self.trace()), before)
        with self.assertRaises(ValueError):
            restarted.recover()

    def test_specialist_stays_in_pool_without_satisfying_deployment_gate(self):
        self.raw["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(specialist=True))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(len(controller.search["pool"]), 2)
        self.assertEqual(self.cycles(controller)[0]["status"], "pooled")
        self.assertEqual(controller.search["result"]["status"], "no_finalist")
        self.assertEqual(controller.state["active_version"], original)
        self.assertEqual(self.evaluations("test"), [])

    def test_same_parent_batch_reuses_cache_without_metric_charge(self):
        self.raw["optimizer"]["train_batch_size"] = 3
        controller = self.controller(self.configure(improvement="none"))
        self.finish(controller)
        self.assertEqual(len(self.evaluations("train", "parent")), 1)
        self.assertEqual(controller.search["metric_calls"], 5)
        self.assertEqual(controller.search["full_evaluations"], 1)
        self.assertEqual([c["status"] for c in self.cycles(controller)], ["no_change", "no_change"])

    def test_different_training_batches_do_not_share_cache(self):
        controller = self.controller(self.configure(improvement="none"))
        self.finish(controller)
        batches = self.evaluations("train", "parent")
        self.assertEqual(len(batches), 2)
        self.assertNotEqual(batches[0]["batch"], batches[1]["batch"])
        self.assertTrue(set(batches[0]["examples"]).isdisjoint(batches[1]["examples"]))
        self.assertEqual(controller.search["metric_calls"], 5)

    def _second_no_change_cycle(self):
        self.raw["optimizer"]["train_batch_size"] = 3
        controller = self.controller(self.configure(improvement="none"))
        self.until(controller, lambda: len(self.cycles(controller)) == 2)
        first = self.cycles(controller)[0]["receipts"]["parent_train"]
        return controller, first

    def test_expired_training_receipt_is_remeasured(self):
        controller, record = self._second_no_change_cycle()
        record["at"] -= 1000
        controller.save()
        self.finish(controller)
        self.assertEqual(len(self.evaluations("train", "parent")), 2)
        self.assertEqual(controller.search["metric_calls"], 8)

    def test_tampered_training_cache_evidence_is_remeasured(self):
        controller, record = self._second_no_change_cycle()
        evidence = next(Path(path) for path in record["artifacts"] if path.endswith("measurement.txt"))
        evidence.write_text("tampered")
        self.finish(controller)
        self.assertEqual(len(self.evaluations("train", "parent")), 2)
        self.assertEqual(controller.search["metric_calls"], 8)

    def test_validation_and_test_for_same_skill_are_distinct_evaluations(self):
        self.raw["limits"]["max_cycles"] = 1
        controller = self.controller()
        original = controller.state["active_version"]
        self.finish(controller)
        original_evals = [row for row in self.evaluations() if row["digest"] == original]
        self.assertEqual({row["split"] for row in original_evals}, {"train", "validation", "test"})
        self.assertEqual(len(original_evals), 3)

    def test_sample_budget_is_reserved_for_one_final_comparison(self):
        self.raw["limits"]["max_cycles"] = 5
        self.raw["optimizer"]["max_metric_calls"] = 12
        controller = self.controller()
        self.finish(controller)
        self.assertEqual(controller.search["result"]["status"], "promoted")
        self.assertEqual(controller.search["metric_calls"], 12)
        self.assertEqual(len(self.evaluations("test")), 2)
        self.assertEqual(len(self.evaluations("validation", "candidate")), 1)
        self.assertEqual((controller.version(controller.state["active_version"]) / "notes.md").read_text(), "1")

    def test_full_evaluation_budget_is_reserved_for_final_test(self):
        self.raw["limits"]["max_cycles"] = 5
        self.raw["optimizer"]["max_full_evaluations"] = 4
        controller = self.controller()
        self.finish(controller)
        self.assertEqual(controller.search["result"]["status"], "promoted")
        self.assertEqual(controller.search["full_evaluations"], 4)
        self.assertEqual(len(self.evaluations("test")), 2)
        self.assertEqual(len(self.evaluations("validation", "candidate")), 1)

    def test_failed_bootstrap_cannot_leave_budget_exhaustion_recovery_loop(self):
        self.raw["optimizer"]["max_metric_calls"] = 6
        self.raw["optimizer"]["max_full_evaluations"] = 3
        controller = self.controller(self.configure(bad="nonobject", bad_split="validation", bad_role="seed"))
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        controller.recover()
        self.finish(controller)
        self.assertEqual(controller.state["status"], "completed")
        self.assertIn(controller.search["result"]["status"], ("budget_exhausted", "no_finalist"))
        self.assertEqual(controller.search["metric_calls"], 2)
        self.assertEqual(len(self.evaluations()), 1)

    def test_reservation_survives_crash_before_submission_without_double_charge(self):
        controller = self.controller()
        self.until(controller, lambda: self.stage_pending(controller, "parent_train"))
        before = controller.search["metric_calls"]
        with mock.patch.object(controller, "submit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                controller.tick()
        self.assertEqual(controller.search["metric_calls"], before + 2)
        restarted = self.controller(controller.config)
        self.finish(restarted)
        calls = self.evaluations()
        self.assertEqual(restarted.search["metric_calls"], sum(len(row["examples"]) for row in calls))
        self.assertEqual(len([row for row in calls if row["cycle"] == "s0001" and row["role"] == "parent"]), 1)

    def test_restart_reattaches_long_validation_once_without_double_budget_charge(self):
        self.raw["limits"]["max_cycles"] = 1
        self.configure(delay_split="validation", delay_role="candidate", delay_seconds=1.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

        def start():
            process = subprocess.Popen([sys.executable, "-m", "rsi_service", "serve", "--config", str(self.config_path)],
                                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.services.append(process)
            return process

        service = start()
        deadline = time.monotonic() + 15
        job_dir = None
        while time.monotonic() < deadline:
            state_path = self.state_dir / "state.json"
            if state_path.exists():
                state = read_json(state_path)
                current = state["cycles"][state["current"]] if state["current"] is not None else None
                if current and current["phase"] == "candidate_validation" and current["job"]:
                    directory = Path(current["job"]["dir"])
                    if worker_busy(directory) and self.evaluations("validation", "candidate"):
                        job_dir = directory
                        break
            self.assertIsNone(service.poll(), "service exited before long evaluation")
            time.sleep(0.02)
        self.assertIsNotNone(job_dir, "long evaluation did not start")
        worker_pid = read_json(job_dir / "status.json")["worker_pid"]
        charged = state["search"]["metric_calls"]
        service.send_signal(signal.SIGTERM)
        self.assertEqual(service.wait(timeout=5), 0)
        self.assertTrue(worker_busy(job_dir))
        resumed = start()
        time.sleep(0.1)
        self.assertEqual(read_json(job_dir / "status.json")["worker_pid"], worker_pid)
        self.assertEqual(read_json(self.state_dir / "state.json")["search"]["metric_calls"], charged)
        self.assertEqual(resumed.wait(timeout=15), 0)
        state = read_json(self.state_dir / "state.json")
        self.assertEqual(state["search"]["result"]["status"], "promoted")
        self.assertEqual(len(self.evaluations("validation", "candidate")), 1)
        self.assertEqual(state["search"]["metric_calls"], sum(len(row["examples"]) for row in self.evaluations()))

    def test_changed_dataset_blocks_search_without_charging_next_job(self):
        controller = self.controller()
        self.until(controller, lambda: self.stage_pending(controller, "parent_train"))
        before = controller.search["metric_calls"]
        self.tasks["train"][0]["task"] = "changed training contract"
        self.task_path.write_text(json.dumps(self.tasks))
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertEqual(controller.search["metric_calls"], before)
        self.assertEqual(self.evaluations("train"), [])

    def test_pool_artifact_tampering_blocks_before_parent_selection(self):
        controller = self.controller()
        self.until(controller, lambda: bool(controller.search["pool"]) and controller.current() is None)
        record = next(iter(controller.search["pool"].values()))["validation"]
        next(Path(p) for p in record["artifacts"] if p.endswith("measurement.txt")).write_text("forged")
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertIn("candidate-pool evidence", controller.state["reason"])
        self.assertEqual(self.evaluations("train"), [])

    def _assert_bad_result_blocks(self, mode):
        controller = self.controller(self.configure(bad=mode))
        original = controller.state["active_version"]
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        self.assertEqual(controller.state["active_version"], original)
        self.assertFalse(any(row["phase"] == "improve" for row in self.trace()))

    def test_unrequested_cases_cannot_be_used_for_search(self):
        self._assert_bad_result_blocks("case")

    def test_wrong_batch_identity_cannot_be_used_for_search(self):
        self._assert_bad_result_blocks("batch")

    def test_training_without_per_example_feedback_blocks(self):
        self._assert_bad_result_blocks("side_info")

    def test_inconsistent_aggregate_score_blocks(self):
        self._assert_bad_result_blocks("score")

    def test_oversized_integer_score_fails_closed(self):
        self._assert_bad_result_blocks("oversized_integer")

    def test_nested_nonfinite_feedback_is_blocked_without_poisoning_durable_state(self):
        controller = self.controller(self.configure(bad="nonfinite_side_info"))
        self.finish(controller)
        self.assertEqual(controller.state["status"], "blocked")
        persisted = read_json(self.state_dir / "state.json")
        self.assertEqual(persisted["status"], "blocked")
        self.assertNotIn("reflection_data", controller.current())
        restarted = self.controller(controller.config)
        self.assertFalse(restarted.tick())

    def test_nonobject_evaluation_blocks(self):
        self._assert_bad_result_blocks("nonobject")

    def test_quick_failure_skips_candidate_training_and_validation(self):
        self.raw["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(quick_fail=True))
        self.finish(controller)
        self.assertEqual(self.cycles(controller)[0]["status"], "rejected")
        self.assertEqual(self.evaluations("train", "candidate"), [])
        self.assertEqual(self.evaluations("validation", "candidate"), [])

    def test_protected_change_is_rejected_before_candidate_evaluation(self):
        self.raw["limits"]["max_cycles"] = 1
        controller = self.controller(self.configure(improvement="protected"))
        self.finish(controller)
        self.assertEqual(self.cycles(controller)[0]["status"], "rejected")
        self.assertIn("protected", self.cycles(controller)[0]["reason"])
        self.assertEqual(self.evaluations("train", "candidate"), [])

    def test_existing_candidate_content_is_not_evaluated_twice(self):
        controller = self.controller()
        self.until(controller, lambda: len(self.cycles(controller)) == 2 and self.stage_pending(controller, "improve"))
        cycle = controller.current()
        proposal = self.state_dir / "cycles" / cycle["id"] / "candidate" / "notes.md"
        # Simulate an improver proposing the original known version; all source
        # evaluator inputs stay immutable. Complete the job with a deterministic
        # output so the test exercises candidate admission rather than an LLM.
        controller.tick()
        job = cycle["job"]
        deadline = time.monotonic() + 5
        while worker_busy(job["dir"]) or not (Path(job["dir"]) / "status.json").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        proposal.write_text("0")
        controller.tick()
        self.assertEqual(cycle["status"], "duplicate")
        self.assertFalse(any(row["cycle"] == cycle["id"] and row["role"] == "candidate" for row in self.evaluations()))


if __name__ == "__main__":
    unittest.main()
