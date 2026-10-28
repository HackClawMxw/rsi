"""Explicit configuration for unattended work; no shell expansion."""
from __future__ import annotations

import math
from pathlib import Path
from .storage import read_json, digest, file_hash


def number(value, name, minimum=0):
    try:
        finite = isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if isinstance(value, bool) or not finite or value < minimum:
        raise ValueError(name + " must be a finite number >= " + str(minimum))
    return value


def load_config(path, check_paths=True):
    path = Path(path).resolve()
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    if data.get("schema_version") != 1:
        raise ValueError("config schema_version must be 1")
    for key in ("state_dir", "workspace", "skill_source"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError("missing " + key)
        data[key] = str((path.parent / Path(data[key]).expanduser()).resolve())
    for key in ("workspace", "skill_source"):
        if check_paths and not Path(data[key]).is_dir():
            raise ValueError(key + " does not exist: " + data[key])
        a, b = Path(data["state_dir"]), Path(data[key])
        if a == b or a in b.parents or b in a.parents:
            raise ValueError("state_dir must be separate from " + key)
    if "autonomy" in data:
        from .autonomous_config import normalize_autonomy
        normalize_autonomy(data, path.parent, check_paths)
        return data
    if not isinstance(data.get("task"), str) or not data["task"].strip():
        raise ValueError("task must be nonempty")
    for name in ("allowed_paths", "protected_paths"):
        patterns = data.get(name, [])
        if not isinstance(patterns, list) or any(not isinstance(p, str) or not p or p.startswith("/") or ".." in p.split("/") for p in patterns):
            raise ValueError(name + " must contain safe relative path patterns")
        data[name] = patterns
    if not data["allowed_paths"]:
        raise ValueError("allowed_paths must explicitly declare editable skill files")
    data["protected_paths"] = sorted(set(data["protected_paths"] + ["SKILL.md", "scripts/**", "**/check_*", "check_*"]))
    steps = data.get("steps", {})
    if not isinstance(steps, dict):
        raise ValueError("steps must be a JSON object")
    for phase in ("run", "improve", "quick", "evaluate"):
        step = steps.get(phase)
        if not isinstance(step, dict) or step.get("adapter") not in ("command", "codex"):
            raise ValueError("missing/invalid step " + phase)
        if phase in ("quick", "evaluate") and step["adapter"] != "command":
            raise ValueError("validation must use an independent command")
        if step["adapter"] == "command":
            if not isinstance(step.get("argv"), list) or not step["argv"] or any(not isinstance(v, str) or not v or "\x00" in v for v in step["argv"]):
                raise ValueError("argv must be a nonempty string array")
            if "cwd" in step:
                step["cwd"] = str((path.parent / step["cwd"]).resolve())
                if check_paths and not Path(step["cwd"]).is_dir():
                    raise ValueError("step cwd does not exist")
    limits = data.setdefault("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("limits must be a JSON object")
    for key, default, minimum in (("max_cycles", 3, 1), ("max_wall_seconds", 604800, 1),
                                  ("max_no_change_cycles", 2, 1), ("poll_seconds", 1, 0.02),
                                  ("log_limit_bytes", 67108864, 1024)):
        limits[key] = number(limits.get(key, default), key, minimum)
    for key in ("max_cycles", "max_no_change_cycles", "log_limit_bytes"):
        if not isinstance(limits[key], int):
            raise ValueError(key + " must be an integer")
    timeouts = limits.setdefault("timeouts", {})
    if not isinstance(timeouts, dict):
        raise ValueError("timeouts must be a JSON object")
    for phase, default in (("run", 86400), ("improve", 3600), ("quick", 300), ("evaluate", 172800)):
        timeouts[phase] = number(timeouts.get(phase, default), "timeout " + phase, 0.1)
    validation = data.setdefault("validation", {})
    if not isinstance(validation, dict):
        raise ValueError("validation must be a JSON object")
    if not isinstance(validation.get("key"), str) or not validation["key"].strip():
        raise ValueError("validation.key must identify workload, target revision and environment")
    inputs = validation.get("inputs")
    if not isinstance(inputs, list) or not inputs or any(not isinstance(p, str) for p in inputs):
        raise ValueError("validation.inputs must name evaluator/workload files to fingerprint")
    validation["inputs"] = [str((path.parent / p).resolve()) for p in inputs]
    for p in validation["inputs"]:
        if check_paths and not Path(p).is_file():
            raise ValueError("validation input must be an existing file: " + p)
    for key, default in (("baseline_cache_ttl_seconds", 0), ("min_gain", 0.01), ("max_case_regression", 0)):
        validation[key] = number(validation.get(key, default), key)
    from .search import normalize_optimizer
    normalize_optimizer(data, path.parent, check_paths)
    return data


def validation_identity(config):
    return digest({"config": config, "inputs": {p: file_hash(p) for p in config["validation"]["inputs"]}})
