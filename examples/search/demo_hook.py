#!/usr/bin/env python3
"""Deterministic fixture for training-feedback skill evolution.

The evaluator executes the supplied skill's text rules on supplied examples.
The improver sees only the controller's training reflection_data and adds one
observed recovery rule per proposal. No model, network, or hardware is used.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

RULES = {
    "TEMPORARY_CAPACITY": "retry_later",
    "GRAPH_CAPTURE_UNSUPPORTED": "eager_fallback",
}
ACTIONS = {"stop", "retry_later", "eager_fallback"}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def classify(skill, message):
    notes = (Path(skill) / "notes.md").read_text(encoding="utf-8")
    if "FATAL_CORRUPT_WEIGHTS" in message:
        return "stop"
    for condition, action in RULES.items():
        if condition in message and condition + " => " + action in notes:
            return action
    return "stop"


def main():
    context = json.loads(Path(os.environ["RSI_CONTEXT_FILE"]).read_text(encoding="utf-8"))
    result_path = Path(os.environ["RSI_RESULT_PATH"])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    phase = context["phase"]
    skill = Path(context["skill_dir"])
    print(json.dumps({"event": "fixture.started", "phase": phase,
                      "split": context.get("split"), "skill_digest": context["skill_digest"]}), flush=True)
    if phase == "run":
        checks = {
            "capacity": ("deployment fresh-worker: TEMPORARY_CAPACITY", "retry_later"),
            "graph": ("deployment fresh-capture: GRAPH_CAPTURE_UNSUPPORTED", "eager_fallback"),
            "fatal": ("deployment checkpoint: FATAL_CORRUPT_WEIGHTS", "stop"),
        }
        actual = {name: classify(skill, message) for name, (message, _) in checks.items()}
        passed = all(actual[name] == expected for name, (_, expected) in checks.items())
        write_json(result_path.parent / "deployment-run.json", {
            "skill_digest": context["skill_digest"], "actual": actual,
        })
        result = {"outcome": "signed" if passed else "aborted",
                  "summary": "Executed a fresh fixture run using the selected skill version.",
                  "evidence": ["deployment-run.json"]}
    elif phase == "evaluate":
        delay = float(os.environ.get("RSI_DEMO_EVAL_SECONDS", "0"))
        if delay > 0:
            time.sleep(delay)
        examples = context["examples"]
        cases, details = {}, {}
        for example in examples:
            data = example["input"]
            actual = classify(skill, data["message"])
            expected = data["expected"]
            cases[example["id"]] = float(actual == expected)
            details[example["id"]] = {
                "input": data["message"], "actual": actual, "expected": expected,
                "feedback": "Correct action." if actual == expected else "Observed failure class needs a reviewed recovery rule.",
            }
        mandatory = all(row["actual"] in ACTIONS for row in details.values())
        write_json(result_path.parent / "measurements.json", {
            "kind": "deterministic_fixture", "skill_digest": context["skill_digest"],
            "split": context["split"], "batch_digest": context["batch_digest"],
            "measurements": details,
        })
        result = {
            "skill_digest": context["skill_digest"], "validation_key": context["validation_key"],
            "split": context["split"], "batch_digest": context["batch_digest"],
            "passed": mandatory, "score": sum(cases.values()) / len(cases), "cases": cases,
            "evidence": ["measurements.json"],
        }
        if context["split"] == "train":
            result["side_info"] = details
    elif phase == "improve":
        notes_path = Path(context["candidate_dir"]) / "notes.md"
        notes = notes_path.read_text(encoding="utf-8")
        change = None
        # Only measured training feedback may propose an edit. No dataset file
        # or validation/test result is opened by this improver.
        for feedback in context["reflection_data"]:
            row = feedback["side_info"]
            if row["actual"] == row["expected"]:
                continue
            for condition, action in RULES.items():
                rule = condition + " => " + action
                if condition in row["input"] and action == row["expected"] and rule not in notes:
                    change = rule
                    break
            if change:
                break
        if change:
            notes_path.write_text(notes + "\n" + change + "\n", encoding="utf-8")
        result = {"changed": change is not None,
                  "summary": "Add observed rule: " + change if change else "No unsupported training recovery remains in this batch."}
    elif phase == "quick":
        notes = (skill / "notes.md").read_text(encoding="utf-8")
        passed = bool(notes.strip()) and len(notes.encode("utf-8")) < 10000
        write_json(result_path.parent / "quick-report.json", {
            "nonempty_notes": bool(notes.strip()), "notes_bytes": len(notes.encode("utf-8")),
            "skill_present": (skill / "SKILL.md").is_file(),
        })
        result = {"passed": passed, "evidence": ["quick-report.json"]}
    else:
        raise ValueError("Unexpected search hook phase: " + phase)
    write_json(result_path, result)
    print(json.dumps({"event": "fixture.completed", "phase": phase}), flush=True)


if __name__ == "__main__":
    main()
