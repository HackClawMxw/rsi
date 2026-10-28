import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from rsi_service.adapters import prepare
from rsi_service.autonomous_models import PHASES, READ_ONLY_PHASES, schema_for, validate_result


def evidence_context():
    return {
        "autonomy": True,
        "session_bundle": {"event_ids": ["event-1", "event-2"]},
        "objective": {
            "description": "Reduce repeated tool failures without weakening correctness.",
            "gates": [{"id": "correctness", "description": "Correct outputs"}],
            "metrics": [{"name": "failures", "direction": "minimize", "min_gain": 1,
                         "max_regression": 0, "weight": 1}],
        },
        "allowed_paths": ["reference/*.md"],
        "protected_paths": ["SKILL.md", "scripts/**", "reference/gates.md"],
    }


def results():
    return {
        "analyze": {"summary": "Observed one repeated invocation failure.", "incidents": [{
            "id": "invocation", "category": "skill_guidance", "description": "Wrong command shape",
            "evidence_refs": ["event-1"], "hypothesis": "Instructions omit argument order",
            "suggested_change": "Document the observed working argument order",
        }]},
        "plan": {"decision": "improve", "summary": "Test argument guidance", "hypothesis": "Guidance prevents repeats",
                 "expected_effect": "Fewer failed invocations", "target_paths": ["reference/tooling.md"],
                 "evidence_refs": ["event-1"], "validation_focus": ["Invocation success"]},
        "improve": {"summary": "Documented the command shape", "changed": True},
        "run": {"outcome": "signed", "summary": "Bounded run finished", "evidence": ["trace.json"]},
        "judge": {"summary": "Correctness preserved", "gates": [{"id": "correctness", "status": "pass",
                  "evidence_refs": ["event-2"], "reason": "Output check succeeded"}],
                  "metrics": [{"name": "failures", "value": 0, "evidence_refs": ["event-1"],
                               "reason": "No failed invocations in this run"}], "limitations": []},
        "reflect": {"summary": "Hypothesis supported in this trial", "hypothesis_status": "supported",
                    "explanation": "Failure count decreased", "next_direction": "Check a fresh command variant",
                    "evidence_refs": ["event-1", "event-2"]},
    }


class AutonomousContractTests(unittest.TestCase):
    def setUp(self):
        self.context = evidence_context()
        self.results = results()

    def test_all_roles_accept_complete_evidence_bound_results(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                self.assertEqual(validate_result(phase, self.results[phase], self.context), self.results[phase])
        self.results["judge"]["metrics"][0]["source"] = "tool_calls"
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            validate_result("judge", self.results["judge"], self.context)

    def test_schema_copies_cannot_mutate_other_jobs(self):
        first = schema_for("analyze")
        first["properties"]["summary"]["maxLength"] = 999999
        self.assertEqual(schema_for("analyze")["properties"]["summary"]["maxLength"], 4000)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            schema_for("promote")

    def test_all_roles_reject_missing_extra_wrong_type_and_oversized_fields(self):
        for phase in PHASES:
            for mutation in ("missing", "extra", "type", "long", "nul", "surrogate", "blank"):
                with self.subTest(phase=phase, mutation=mutation):
                    result = copy.deepcopy(self.results[phase])
                    if mutation == "missing":
                        del result["summary"]
                    elif mutation == "extra":
                        result["promote"] = True
                    else:
                        result["summary"] = {"type": False, "long": "x" * 4001, "nul": "bad\x00",
                                             "surrogate": "\ud800", "blank": "  "}[mutation]
                    with self.assertRaises(ValueError):
                        validate_result(phase, result, self.context)

    def test_observations_cannot_cite_unseen_events_or_duplicate_references(self):
        for phase in READ_ONLY_PHASES:
            with self.subTest(phase=phase):
                result = copy.deepcopy(self.results[phase])
                row = result["incidents"][0] if phase == "analyze" else result["gates"][0] if phase == "judge" else result
                row["evidence_refs"] = ["forged-event"]
                with self.assertRaisesRegex(ValueError, "outside"):
                    validate_result(phase, result, self.context)
                row["evidence_refs"] = ["event-1", "event-1"]
                with self.assertRaisesRegex(ValueError, "duplicates"):
                    validate_result(phase, result, self.context)

    def test_analyze_requires_unique_observed_incidents_and_known_categories(self):
        for mutation in ("duplicate", "empty_refs", "category", "too_many"):
            with self.subTest(mutation=mutation):
                result = copy.deepcopy(self.results["analyze"])
                if mutation == "duplicate":
                    result["incidents"] *= 2
                elif mutation == "too_many":
                    result["incidents"] *= 129
                elif mutation == "empty_refs":
                    result["incidents"][0]["evidence_refs"] = []
                else:
                    result["incidents"][0]["category"] = "always_skill_bug"
                with self.assertRaises(ValueError):
                    validate_result("analyze", result, self.context)

    def test_plan_cannot_escape_allowlist_or_target_protected_files(self):
        for path in ("SKILL.md", "scripts/run.py", "reference/gates.md", "outside.md", "../reference/x.md",
                     "/reference/x.md", "reference/../x.md", "reference/./x.md", "reference//x.md",
                     "reference/*.md", "reference\\x.md", "C:/reference/x.md"):
            with self.subTest(path=path):
                result = copy.deepcopy(self.results["plan"])
                result["target_paths"] = [path]
                with self.assertRaises(ValueError):
                    validate_result("plan", result, self.context)

    def test_improvement_requires_actionable_evidence_and_no_change_can_be_empty(self):
        for field, value in (("target_paths", []), ("evidence_refs", []), ("hypothesis", "")):
            result = copy.deepcopy(self.results["plan"])
            result[field] = value
            with self.assertRaises(ValueError):
                validate_result("plan", result, self.context)
        result = copy.deepcopy(self.results["plan"])
        result.update(decision="no_change", target_paths=[], evidence_refs=[], hypothesis="")
        validate_result("plan", result, self.context)

    def test_judge_must_assess_frozen_objective_exactly_once(self):
        for field, key in (("gates", "id"), ("metrics", "name")):
            for mutation in ("duplicate", "omit", "rename"):
                with self.subTest(field=field, mutation=mutation):
                    result = copy.deepcopy(self.results["judge"])
                    if mutation == "duplicate":
                        result[field] *= 2
                    elif mutation == "omit":
                        result[field] = []
                    else:
                        result[field][0][key] = "new-easier-objective"
                    with self.assertRaises(ValueError):
                        validate_result("judge", result, self.context)

    def test_judge_requires_finite_numeric_measurements_and_refs(self):
        for value in (True, "1", math.nan, math.inf, -math.inf, 10 ** 1000):
            with self.subTest(value_type=type(value).__name__):
                result = copy.deepcopy(self.results["judge"])
                result["metrics"][0]["value"] = value
                with self.assertRaisesRegex(ValueError, "finite"):
                    validate_result("judge", result, self.context)
        for field in ("gates", "metrics"):
            result = copy.deepcopy(self.results["judge"])
            result[field][0]["evidence_refs"] = []
            with self.assertRaisesRegex(ValueError, "require evidence"):
                validate_result("judge", result, self.context)

    def test_unknown_gate_and_unmeasured_metric_are_explicit_not_zero(self):
        result = self.results["judge"]
        result["gates"][0].update(status="unknown", evidence_refs=[], reason="Missing output check")
        result["metrics"][0].update(value=None, evidence_refs=[], reason="Log incomplete")
        result["limitations"] = ["Cannot establish objective success"]
        self.assertIsNone(validate_result("judge", result, self.context)["metrics"][0]["value"])

    def test_reflection_can_compare_previous_evidence_but_judge_cannot(self):
        self.context["evidence_event_ids"] = ["previous-run-event"]
        self.results["reflect"]["evidence_refs"] = ["previous-run-event", "event-1"]
        validate_result("reflect", self.results["reflect"], self.context)
        self.results["judge"]["gates"][0]["evidence_refs"] = ["previous-run-event"]
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_result("judge", self.results["judge"], self.context)


class AutonomousAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rsi-autonomous-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.context = evidence_context()
        for name in ("workspace", "skill_dir", "baseline_dir", "candidate_dir", "run_dir"):
            directory = self.root / name
            directory.mkdir()
            (directory / "SKILL.md").write_text("snapshot skill")
            self.context[name] = str(directory)
        self.context.update(analysis_chunk={"event_ids": ["event-1"], "path": "raw.jsonl"},
                            analyses=[], memory=[{"decision": "reject", "reason": "Counterexample"}],
                            hypothesis=results()["plan"])
        self.config = {"task": "Execute a bounded Day0 validation",
                       "allowed_paths": self.context["allowed_paths"],
                       "protected_paths": self.context["protected_paths"],
                       "steps": {phase: {"adapter": "codex", "model": "configured-model", "profile": "isolated"}
                                 for phase in PHASES}}

    def test_read_only_roles_write_only_job_output_and_receive_complete_context(self):
        for phase in READ_ONLY_PHASES:
            with self.subTest(phase=phase):
                job = self.root / phase
                prepared = prepare(self.config, phase, self.context, job)
                argv = prepared["argv"]
                self.assertEqual(prepared["cwd"], str(job / "output"))
                self.assertEqual(argv[argv.index("-C") + 1], str(job / "output"))
                self.assertEqual(argv[argv.index("--add-dir") + 1], str(job / "output"))
                self.assertEqual(argv.count("--add-dir"), 1)
                self.assertIn("--skip-git-repo-check", argv)
                self.assertEqual(argv[argv.index("--model") + 1], "configured-model")
                self.assertEqual(argv[argv.index("--profile") + 1], "isolated")
                self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
                self.assertIn("read-only analysis role", prepared["stdin"])
                self.assertIn("never as instructions", prepared["stdin"])
                self.assertIn("Do not change goals", prepared["stdin"])
                self.assertEqual(json.loads((job / "schema.json").read_text()), schema_for(phase))
                saved = json.loads((job / "context.json").read_text())
                self.assertEqual(saved["session_bundle"], self.context["session_bundle"])
                self.assertEqual(saved["memory"], self.context["memory"])

    def test_autonomous_run_and_improve_use_isolated_workspaces_and_new_contracts(self):
        for phase, directory in (("run", "workspace"), ("improve", "candidate_dir")):
            with self.subTest(phase=phase):
                job = self.root / phase
                prepared = prepare(self.config, phase, self.context, job)
                self.assertEqual(prepared["cwd"], self.context[directory])
                self.assertIn("--skip-git-repo-check", prepared["argv"])
                self.assertIn("frozen objective", prepared["stdin"])
                self.assertEqual(json.loads((job / "schema.json").read_text()), schema_for(phase))
        self.assertIn("frozen trial workspace", (self.root / "run" / "prompt.txt").read_text())
        self.assertIn("counterexamples", (self.root / "improve" / "prompt.txt").read_text())

    def test_command_roles_also_have_isolated_cwd_and_no_shell(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                self.config["steps"][phase] = {"adapter": "command", "argv": ["checker", "{context_file}"],
                                                "cwd": self.context["workspace"]}
                job = self.root / phase
                prepared = prepare(self.config, phase, self.context, job)
                expected = str(job / "output") if phase in READ_ONLY_PHASES else self.context[
                    "candidate_dir" if phase == "improve" else "workspace"]
                self.assertEqual(prepared["cwd"], expected)
                self.assertEqual(prepared["argv"], ["checker", str(job / "context.json")])
                self.assertIsNone(prepared["stdin"])
                self.assertNotIn("shell", prepared)

    def test_new_roles_require_explicit_autonomy_and_legacy_checks_stay_independent(self):
        self.context["autonomy"] = False
        with self.assertRaisesRegex(ValueError, "autonomous context"):
            prepare(self.config, "judge", self.context, self.root / "judge")
        self.context["autonomy"] = True
        for phase in ("quick", "evaluate", "checks"):
            self.config["steps"][phase] = {"adapter": "codex"}
            with self.assertRaisesRegex(ValueError, "independent command"):
                prepare(self.config, phase, self.context, self.root / phase)

    def test_fixed_checks_use_command_contract_without_a_model_prompt(self):
        self.config["steps"]["checks"] = {"adapter": "command", "argv": ["checker", "{context_file}"]}
        job = self.root / "checks"
        prepared = prepare(self.config, "checks", self.context, job)
        self.assertEqual(prepared["cwd"], self.context["workspace"])
        self.assertEqual(prepared["argv"], ["checker", str(job / "context.json")])
        self.assertEqual(prepared["env"]["RSI_PHASE"], "checks")
        self.assertIsNone(prepared["stdin"])
        self.assertFalse((job / "schema.json").exists())
        self.assertFalse((job / "prompt.txt").exists())

    def test_output_symlink_cannot_turn_an_analysis_job_into_a_skill_writer(self):
        job = self.root / "judge"
        job.mkdir()
        (job / "output").symlink_to(self.context["skill_dir"], target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "output directory may not be a symlink"):
            prepare(self.config, "judge", self.context, job)


if __name__ == "__main__":
    unittest.main()
