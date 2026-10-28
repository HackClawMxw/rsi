#!/usr/bin/env python3
"""Deterministic toy skill runtime, improver, and evaluator for the service demo.

A text recovery rule changes fixture behavior. The measured gain only proves
this fixture's loop; it says nothing about a real Day0 run or model capability.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

RULE = "TEMPORARY_CAPACITY => retry_later"
CASES = {
    "transient_capacity": ("worker: TEMPORARY_CAPACITY; retry available", "retry_later"),
    "fatal_error": ("worker: FATAL_CORRUPT_WEIGHTS", "stop"),
}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def classify(skill: Path, message: str) -> str:
    notes = (skill / "notes.md").read_text(encoding="utf-8")
    if "TEMPORARY_CAPACITY" in message and RULE in notes:
        return "retry_later"
    return "stop"


def main() -> None:
    context = json.loads(Path(os.environ["RSI_CONTEXT_FILE"]).read_text(encoding="utf-8"))
    result_path = Path(os.environ["RSI_RESULT_PATH"])
    output = result_path.parent
    output.mkdir(parents=True, exist_ok=True)
    skill = Path(context["skill_dir"])
    phase = context["phase"]
    print(json.dumps({"event": "fixture.started", "phase": phase,
                      "cycle": context["cycle_id"]}), flush=True)
    if phase == "run":
        message, expected = CASES["transient_capacity"]
        actual = classify(skill, message)
        write_json(output / "run-evidence.json", {
            "input": message, "expected": expected, "actual": actual,
            "skill_digest": context["skill_digest"],
        })
        result = {
            "outcome": "signed" if actual == expected else "aborted",
            "summary": "Fixture recovered from transient capacity failure."
            if actual == expected else "Observed TEMPORARY_CAPACITY was incorrectly treated as fatal.",
            "evidence": ["run-evidence.json"],
        }
    elif phase == "improve":
        candidate = Path(context["candidate_dir"])
        message, expected = CASES["transient_capacity"]
        changed = classify(candidate, message) != expected
        if changed:
            notes_path = candidate / "notes.md"
            notes = notes_path.read_text(encoding="utf-8")
            notes_path.write_text(notes + "\nObserved recovery rule:\n\n" + RULE + "\n", encoding="utf-8")
        result = {"changed": changed, "summary": "Add the reproduced transient-capacity recovery rule."
                  if changed else "The fixture already handles the observed failure; no change."}
    elif phase == "quick":
        notes = (skill / "notes.md").read_text(encoding="utf-8")
        passed = bool(notes.strip()) and len(notes) < 10000 and (skill / "SKILL.md").is_file()
        write_json(output / "quick-evidence.json", {"nonempty": bool(notes.strip()),
                                                   "notes_bytes": len(notes.encode("utf-8")),
                                                   "skill_present": (skill / "SKILL.md").is_file()})
        result = {"passed": passed, "evidence": ["quick-evidence.json"]}
    elif phase == "evaluate":
        # Optional local delay makes stop/restart/reattach easy to observe. It is
        # not an idle timeout and is not needed for ordinary demo execution.
        delay = float(os.environ.get("RSI_DEMO_EVAL_SECONDS", "0"))
        if delay > 0:
            time.sleep(delay)
        actual = {name: classify(skill, message) for name, (message, _) in CASES.items()}
        scores = {name: float(actual[name] == expected) for name, (_, expected) in CASES.items()}
        mandatory_passed = all(answer in ("stop", "retry_later") for answer in actual.values())
        write_json(output / "measurement.json", {
            "kind": "deterministic_fixture", "actual": actual, "cases": scores,
            "mandatory_passed": mandatory_passed, "skill_digest": context["skill_digest"],
            "validation_key": context["validation_key"],
        })
        result = {
            "skill_digest": context["skill_digest"], "validation_key": context["validation_key"],
            "passed": mandatory_passed, "score": sum(scores.values()) / len(scores),
            "cases": scores, "evidence": ["measurement.json"],
        }
    else:
        raise ValueError("Unknown phase: " + str(phase))
    write_json(result_path, result)
    print(json.dumps({"event": "fixture.completed", "phase": phase}), flush=True)


if __name__ == "__main__":
    main()
