"""Fixed objectives, expensive-trial budgets and evidence-based promotion."""
import copy
import math
from pathlib import Path
import tempfile
import unittest

from rsi_service.autonomous_config import normalize_autonomy
from rsi_service.autonomous_policy import apply_measured_metrics, compare_assessments


def objective():
    return {
        "description": "Reduce validation overhead while preserving correctness",
        "gates": [{"id": "correctness", "description": "All fixed correctness checks pass"}],
        "metrics": [
            {"name": "cost", "direction": "minimize", "min_gain": 5,
             "max_regression": 0, "weight": 1, "source": "wall_seconds"},
            {"name": "coverage", "direction": "maximize", "min_gain": 1,
             "max_regression": 0, "weight": 10, "source": "judge"},
        ],
    }


def assessment(cost=100, coverage=10, gate="pass"):
    return {"gates": [{"id": "correctness", "status": gate, "evidence_refs": ["test-summary.json"]}],
            "metrics": [{"name": "cost", "value": cost, "evidence_refs": ["run-summary.json"]},
                        {"name": "coverage", "value": coverage, "evidence_refs": ["test-summary.json"]}]}


class AutonomousPolicyTests(unittest.TestCase):
    def test_success_claim_is_not_a_gate_and_gate_failure_can_be_repaired(self):
        before, after = assessment(gate="fail"), assessment(cost=80)
        before["outcome"], after["outcome"] = "signed", "aborted"
        result = compare_assessments(before, after, objective())
        self.assertTrue(result["promote"])
        self.assertEqual({"cost": 20, "coverage": 0}, result["gains"])
        self.assertEqual(20, result["weighted_gain"])
        after["gates"][0]["status"] = "fail"
        after["outcome"] = "signed"
        self.assertFalse(compare_assessments(before, after, objective())["promote"])

    def test_unknown_baseline_and_missing_or_extra_gate_cannot_promote(self):
        for before, after in ((assessment(gate="unknown"), assessment(cost=80)),
                              (assessment(), assessment(cost=80, gate="unknown"))):
            self.assertFalse(compare_assessments(before, after, objective())["promote"])
        for side in ("baseline", "candidate"):
            for rows in ([], [{"id": "other", "status": "pass"}]):
                before, after = assessment(), assessment(cost=80)
                (before if side == "baseline" else after)["gates"] = rows
                self.assertFalse(compare_assessments(before, after, objective())["promote"])

    def test_positive_gain_must_cross_threshold_without_any_forbidden_regression(self):
        for after in (assessment(cost=100), assessment(cost=96), assessment(cost=80, coverage=9)):
            self.assertFalse(compare_assessments(assessment(), after, objective())["promote"])
        self.assertTrue(compare_assessments(assessment(), assessment(cost=95), objective())["promote"])
        obj = objective()
        for metric in obj["metrics"]:
            metric["min_gain"] = 0
        self.assertFalse(compare_assessments(assessment(), assessment(), obj)["promote"])

    def test_permitted_regression_still_requires_positive_weighted_gain(self):
        obj = objective()
        obj["metrics"][1]["max_regression"] = 2
        self.assertFalse(compare_assessments(assessment(), assessment(cost=90, coverage=9), obj)["promote"])
        self.assertFalse(compare_assessments(assessment(), assessment(cost=85, coverage=8), obj)["promote"])
        self.assertTrue(compare_assessments(assessment(), assessment(cost=85, coverage=9), obj)["promote"])

    def test_metrics_must_match_objective_and_both_must_be_finite(self):
        for value in (None, True, "12", math.nan, math.inf, -math.inf, 10 ** 1000):
            for side in ("baseline", "candidate"):
                with self.subTest(value=repr(value)[:40], side=side):
                    before, after = assessment(), assessment(cost=80)
                    (before if side == "baseline" else after)["metrics"][0]["value"] = value
                    self.assertFalse(compare_assessments(before, after, objective())["promote"])
        for side in ("baseline", "candidate"):
            before, after = assessment(), assessment(cost=80)
            (before if side == "baseline" else after)["metrics"].pop()
            self.assertFalse(compare_assessments(before, after, objective())["promote"])

    def test_duplicate_assessment_identifiers_are_schema_errors(self):
        for key in ("gates", "metrics"):
            after = assessment(cost=80)
            after[key].append(copy.deepcopy(after[key][0]))
            with self.assertRaises(ValueError):
                compare_assessments(assessment(), after, objective())

    def test_finite_input_arithmetic_overflow_cannot_promote(self):
        obj = objective()
        before, after = assessment(cost=1e308), assessment(cost=-1e308)
        self.assertFalse(compare_assessments(before, after, obj)["promote"])
        obj["metrics"][0]["weight"] = 1e308
        result = compare_assessments(assessment(), assessment(cost=80), obj)
        self.assertFalse(result["promote"])
        self.assertIsNone(result["weighted_gain"])

    def test_worker_metrics_override_model_claim_and_are_attributed_to_worker(self):
        original = assessment(cost=0)
        frozen = copy.deepcopy(original)
        measured = apply_measured_metrics(original, objective(), {"wall_seconds": 110})
        self.assertEqual(frozen, original)
        self.assertEqual(110, measured["metrics"][0]["value"])
        self.assertEqual("worker", measured["metrics"][0]["provenance"])
        self.assertEqual(["worker.stats.wall_seconds"], measured["metrics"][0]["evidence_refs"])
        self.assertEqual(["run-summary.json"], measured["metrics"][0]["judge_evidence_refs"])
        self.assertNotIn("refs", measured["metrics"][0])
        self.assertEqual(original["metrics"][1], measured["metrics"][1])
        self.assertFalse(compare_assessments(assessment(), measured, objective())["promote"])

    def test_missing_worker_stats_are_unknown_not_zero_or_model_values(self):
        for stats in ({}, {"wall_seconds": -1}, {"wall_seconds": True}, {"wall_seconds": math.nan}):
            measured = apply_measured_metrics(assessment(cost=0), objective(), stats)
            self.assertIsNone(measured["metrics"][0]["value"])
            self.assertEqual([], measured["metrics"][0]["evidence_refs"])
            self.assertFalse(compare_assessments(assessment(), measured, objective())["promote"])

    def test_worker_tool_counters_require_nonnegative_integers(self):
        obj = objective()
        obj["metrics"][0]["source"] = "tool_calls"
        for count in (False, 1.5, -1, math.inf):
            result = apply_measured_metrics(assessment(), obj, {"tool_calls": count})
            self.assertIsNone(result["metrics"][0]["value"])
        result = apply_measured_metrics(assessment(), obj, {"tool_calls": 0})
        self.assertEqual(0, result["metrics"][0]["value"])


class AutonomousConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rsi-autonomy-config-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = {"task": "Execute the fixed validation task", "allowed_paths": ["reference/**"],
                       "objective": objective(), "autonomy": {"source": {"path": "rollout.jsonl"}}}

    def normalized(self, check_paths=True):
        normalize_autonomy(self.config, self.root, check_paths)
        return self.config

    def test_legacy_is_unchanged_and_modes_cannot_mix(self):
        data = {"task": "legacy"}
        normalize_autonomy(data, self.root)
        self.assertEqual({"task": "legacy"}, data)
        self.config["optimizer"] = None
        with self.assertRaisesRegex(ValueError, "cannot be enabled together"):
            self.normalized()

    def test_defaults_freeze_contract_and_preserve_expensive_validation_budget(self):
        self.normalized()
        self.assertEqual(str(self.root / "rollout.jsonl"), self.config["autonomy"]["source"]["path"])
        self.assertEqual(4, self.config["autonomy"]["max_validation_runs"])
        self.assertEqual("timeline", self.config["autonomy"]["analysis_view"])
        self.assertEqual(120000, self.config["autonomy"]["timeline_chunk_chars"])
        self.assertEqual(86400, self.config["limits"]["timeouts"]["run"])
        self.assertEqual(3600, self.config["limits"]["timeouts"]["checks"])
        self.assertEqual("codex", self.config["steps"]["analyze"]["adapter"])
        self.assertEqual("command", self.config["steps"]["quick"]["adapter"])
        self.assertEqual(0, self.config["validation"]["baseline_cache_ttl_seconds"])
        self.assertIn("SKILL.md", self.config["protected_paths"])
        self.assertIn("scripts/**", self.config["protected_paths"])
        first = copy.deepcopy(self.config)
        self.normalized()
        self.assertEqual(first, self.config)

    def test_source_discovery_is_deferred_and_session_roots_are_absolute(self):
        self.config["autonomy"]["source"] = {"session_id": "abc-123", "roots": ["missing-sessions"]}
        self.normalized()
        self.assertEqual([str(self.root / "missing-sessions")], self.config["autonomy"]["source"]["roots"])
        self.config["autonomy"]["source"]["session_id"] = "../other"
        with self.assertRaises(ValueError):
            self.normalized()

    def test_conflicting_source_forms_and_invalid_formats_are_rejected(self):
        for source in ({}, {"path": "x", "session_id": "y"}, {"path": "x", "format": "markdown"},
                       {"session_id": "x", "roots": []}, {"session_id": "x", "roots": [None]}):
            self.config["autonomy"]["source"] = source
            with self.assertRaises(ValueError):
                self.normalized()

    def test_validation_identity_binds_external_inputs_without_requiring_them_on_reattach(self):
        path = self.root / "hardware.json"
        path.write_text("{}")
        self.config["autonomy"]["evidence_inputs"] = ["hardware.json", "hardware.json"]
        self.config["validation"] = {"key": "fixed-host-model-workload", "baseline_cache_ttl_seconds": 1000}
        self.normalized()
        self.assertEqual([str(path)], self.config["validation"]["inputs"])
        self.assertEqual("fixed-host-model-workload", self.config["validation"]["key"])
        self.assertEqual(0, self.config["validation"]["baseline_cache_ttl_seconds"])
        path.unlink()
        self.normalized(False)
        with self.assertRaisesRegex(ValueError, "existing file"):
            self.normalized()

    def test_objective_is_required_and_contract_fields_are_strict(self):
        original = copy.deepcopy(self.config)
        cases = [None, {}, dict(objective(), metrics=[]), dict(objective(), gates=[]),
                 dict(objective(), description=" "), dict(objective(), model_can_edit=True)]
        for obj in cases:
            self.config = copy.deepcopy(original)
            self.config["objective"] = obj
            with self.assertRaises(ValueError):
                self.normalized()
        for field, value in (("direction", "up"), ("weight", 0), ("weight", math.inf),
                             ("min_gain", math.nan), ("max_regression", -1), ("source", "stdout")):
            self.config = copy.deepcopy(original)
            self.config["objective"]["metrics"][0][field] = value
            with self.assertRaises(ValueError):
                self.normalized()

    def test_optional_metric_description_preserves_operator_scoring_rubric(self):
        self.config["objective"]["metrics"][1]["description"] = "Count fixed protocol cases that passed."
        self.normalized()
        self.assertEqual("Count fixed protocol cases that passed.", self.config["objective"]["metrics"][1]["description"])
        self.config["objective"]["metrics"][1]["description"] = " "
        with self.assertRaises(ValueError):
            self.normalized()

    def test_unsafe_patterns_and_fractional_budgets_are_rejected(self):
        original = copy.deepcopy(self.config)
        for pattern in ("../x", "/abs", "x/../y", "x\\y", "x\x00y", "C:/x", "", "x//y"):
            self.config = copy.deepcopy(original)
            self.config["allowed_paths"] = [pattern]
            with self.assertRaises(ValueError):
                self.normalized()
        for key, value in (("max_model_calls", True), ("max_validation_runs", 1.5),
                           ("max_session_bytes", 0), ("chunk_chars", math.inf),
                           ("max_model_calls", 10 ** 1000),
                           ("baseline_cache_ttl_seconds", math.nan)):
            self.config = copy.deepcopy(original)
            self.config["autonomy"][key] = value
            with self.assertRaises(ValueError):
                self.normalized()

    def test_operator_checks_are_fixed_argv_with_unique_identity(self):
        self.config["autonomy"]["checks"] = [
            {"id": "unit", "argv": ["python3", "check.py", "{workspace}", "{skill_dir}", "{output_dir}"]}]
        self.normalized()
        self.assertEqual(300, self.config["autonomy"]["checks"][0]["timeout_seconds"])
        self.config["autonomy"]["checks"].append(copy.deepcopy(self.config["autonomy"]["checks"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate check"):
            self.normalized()

    def test_check_ids_cannot_escape_the_output_directory(self):
        for check_id in ("../escape", "/abs", "a/b", "a\\b", ".", "..", "a\x00b", "_start", "a" * 129):
            self.config["autonomy"]["checks"] = [{"id": check_id, "argv": ["fixture"]}]
            with self.assertRaises(ValueError):
                self.normalized()

    def test_timeline_view_has_explicit_bounded_preview_and_full_mode(self):
        original = copy.deepcopy(self.config)
        for field, value in (("analysis_view", "summary"), ("timeline_preview_chars", -1),
                             ("timeline_preview_chars", 4001), ("timeline_preview_chars", True),
                             ("timeline_chunk_chars", 0)):
            self.config = copy.deepcopy(original)
            self.config["autonomy"][field] = value
            with self.assertRaises(ValueError):
                self.normalized()
        self.config = copy.deepcopy(original)
        self.config["autonomy"].update(analysis_view="full", timeline_preview_chars=0)
        self.normalized()
        self.assertEqual("full", self.config["autonomy"]["analysis_view"])

    def test_static_and_execution_checks_cannot_use_model_adapter(self):
        for phase in ("quick", "checks"):
            self.config["steps"] = {phase: {"adapter": "codex"}}
            with self.assertRaisesRegex(ValueError, "independent command"):
                self.normalized()

    def test_command_cwd_is_validated_and_can_be_unmounted_on_reattach(self):
        self.config["steps"] = {"analyze": {"adapter": "command", "argv": ["fixture"], "cwd": "missing"}}
        self.normalized(False)
        self.assertEqual(str(self.root / "missing"), self.config["steps"]["analyze"]["cwd"])
        with self.assertRaisesRegex(ValueError, "cwd does not exist"):
            self.normalized()


if __name__ == "__main__":
    unittest.main()
