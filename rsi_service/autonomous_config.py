"""Operator-authored contracts for autonomous, evidence-driven skill trials."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

from .storage import digest


METRIC_SOURCES = ("judge", "wall_seconds", "failed_tools", "tool_calls")
MODEL_PHASES = ("analyze", "plan", "improve", "run", "judge", "reflect")
PROTECTED_PATHS = ("SKILL.md", "scripts/**", "**/check_*", "check_*")
CHECK_COMMAND = ["{python}", "-m", "rsi_service.autonomous_checks", "--context", "{context_file}"]
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]+\Z")
_CHECK_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(label + " must be nonempty text without NUL")
    return value


def _number(value, label, minimum=0, positive=False):
    try:
        finite = isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if isinstance(value, bool) or not finite or value < minimum or (positive and value <= 0):
        raise ValueError(label + " must be a finite number " + ("> 0" if positive else ">= " + str(minimum)))
    return value


def _integer(value, label, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(label + " must be an integer >= " + str(minimum))
    return _number(value, label, minimum)


def _identifier(value, label):
    _text(value, label)
    if value in (".", "..") or not _IDENTIFIER.fullmatch(value):
        raise ValueError(label + " must use only letters, digits, underscore, dot or hyphen")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must be a JSON object")
    return value


def _path(value, label, config_dir):
    return str((Path(config_dir) / Path(_text(value, label)).expanduser()).resolve())


def _patterns(value, label, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(label + " must contain safe relative path patterns")
    for pattern in value:
        _text(pattern, label)
        if (pattern.startswith(("/", "~")) or "\\" in pattern or ":" in pattern
                or any(part in ("", ".", "..") for part in pattern.split("/"))):
            raise ValueError(label + " must contain safe relative path patterns")
    return list(value)


def normalize_objective(value):
    """Return a validated copy; model proposals never define the objective."""
    objective = copy.deepcopy(_object(value, "objective"))
    if set(objective) - {"description", "metrics", "gates"}:
        raise ValueError("unknown objective field")
    _text(objective.get("description"), "objective.description")
    for name in ("metrics", "gates"):
        if not isinstance(objective.get(name), list) or not objective[name]:
            raise ValueError("objective." + name + " must be a nonempty list")
    seen = set()
    for metric in objective["metrics"]:
        _object(metric, "objective metric")
        if set(metric) - {"name", "description", "direction", "min_gain", "max_regression", "weight", "source"}:
            raise ValueError("unknown objective metric field")
        name = _text(metric.get("name"), "metric.name")
        if "description" in metric:
            _text(metric["description"], "metric.description")
        if name in seen:
            raise ValueError("duplicate objective metric: " + name)
        seen.add(name)
        if metric.get("direction") not in ("minimize", "maximize"):
            raise ValueError("metric.direction must be minimize or maximize")
        for key in ("min_gain", "max_regression"):
            metric[key] = _number(metric.get(key, 0), "metric." + key)
        metric["weight"] = _number(metric.get("weight", 1), "metric.weight", positive=True)
        metric["source"] = metric.get("source", "judge")
        if metric["source"] not in METRIC_SOURCES:
            raise ValueError("unsupported metric.source: " + str(metric["source"]))
    seen = set()
    for gate in objective["gates"]:
        _object(gate, "objective gate")
        if set(gate) - {"id", "description"}:
            raise ValueError("unknown objective gate field")
        name = _text(gate.get("id"), "gate.id")
        _text(gate.get("description"), "gate.description")
        if name in seen:
            raise ValueError("duplicate objective gate: " + name)
        seen.add(name)
    return objective


def _normalize_step(step, phase, config_dir, check_paths):
    _object(step, "step " + phase)
    adapter = step.get("adapter")
    if adapter not in ("codex", "command"):
        raise ValueError("invalid adapter for " + phase)
    if phase in ("quick", "checks") and adapter != "command":
        raise ValueError(phase + " requires an independent command")
    if adapter == "command":
        _argv(step.get("argv"), "step " + phase)
        if "cwd" in step:
            step["cwd"] = _path(step["cwd"], "step cwd", config_dir)
            if check_paths and not Path(step["cwd"]).is_dir():
                raise ValueError("step cwd does not exist: " + step["cwd"])
    else:
        step["binary"] = _text(step.get("binary", "codex"), "codex binary")
        for key in ("model", "profile"):
            if key in step:
                _text(step[key], "codex " + key)
        if "skip_git_repo_check" in step and not isinstance(step["skip_git_repo_check"], bool):
            raise ValueError("skip_git_repo_check must be a boolean")


def _argv(value, label):
    if not isinstance(value, list) or not value:
        raise ValueError(label + " argv must be a nonempty string array")
    for arg in value:
        # Empty arguments are intentionally forbidden, matching command adapters.
        _text(arg, label + " argv element")


def normalize_autonomy(data, config_dir, check_paths=True):
    """Normalize an autonomous config in place, leaving legacy modes alone.

    Source discovery/reading is deferred to ingestion, so detached workers can
    reattach even when the historical rollout mount is no longer available.
    Evidence inputs and explicit command working directories are checked only
    when ``check_paths`` is true.
    """
    if "autonomy" not in data:
        return
    if "optimizer" in data:
        raise ValueError("autonomy and optimizer cannot be enabled together")
    autonomy = _object(data["autonomy"], "autonomy")
    _text(data.get("task"), "task")
    data["objective"] = normalize_objective(data.get("objective"))
    data["allowed_paths"] = _patterns(data.get("allowed_paths"), "allowed_paths", nonempty=True)
    data["protected_paths"] = sorted(set(_patterns(data.get("protected_paths", []), "protected_paths")
                                           + list(PROTECTED_PATHS)))
    source = _object(autonomy.get("source"), "autonomy.source")
    if "path" in source:
        if set(source) - {"path", "format"}:
            raise ValueError("path source accepts only path and format")
        source["path"] = _path(source["path"], "source.path", config_dir)
        source["format"] = source.get("format", "auto")
        if source["format"] not in ("auto", "rollout", "exec"):
            raise ValueError("source.format must be auto, rollout or exec")
    else:
        if set(source) - {"session_id", "roots"}:
            raise ValueError("session source accepts only session_id and roots")
        _identifier(source.get("session_id"), "source.session_id")
        roots = source.get("roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError("source.roots must be a nonempty list")
        source["roots"] = [_path(p, "source root", config_dir) for p in roots]

    for key, default, minimum in (("chunk_chars", 24000, 1), ("max_session_bytes", 536870912, 1),
                                  ("max_model_calls", 200, 1), ("max_validation_runs", 4, 1),
                                  ("timeline_chunk_chars", 120000, 1), ("timeline_preview_chars", 160, 0)):
        autonomy[key] = _integer(autonomy.get(key, default), "autonomy." + key, minimum)
    autonomy["analysis_view"] = autonomy.get("analysis_view", "timeline")
    if autonomy["analysis_view"] not in ("timeline", "full"):
        raise ValueError("autonomy.analysis_view must be timeline or full")
    if autonomy["timeline_preview_chars"] > 4000:
        raise ValueError("autonomy.timeline_preview_chars must be <= 4000")
    autonomy["baseline_cache_ttl_seconds"] = _number(autonomy.get("baseline_cache_ttl_seconds", 0),
                                                        "autonomy.baseline_cache_ttl_seconds")
    autonomy["workspace_excludes"] = _patterns(autonomy.get("workspace_excludes", [
        ".git", ".day0", "__pycache__", ".venv", "node_modules"]), "workspace_excludes")
    checks = autonomy.setdefault("checks", [])
    if not isinstance(checks, list):
        raise ValueError("autonomy.checks must be a list")
    seen = set()
    for check in checks:
        _object(check, "autonomy check")
        if set(check) - {"id", "argv", "timeout_seconds"}:
            raise ValueError("unknown autonomy check field")
        name = _identifier(check.get("id"), "check.id")
        if not _CHECK_IDENTIFIER.fullmatch(name):
            raise ValueError("check.id must start with a letter or digit and contain at most 128 safe characters")
        if name in seen:
            raise ValueError("duplicate check.id: " + name)
        seen.add(name)
        _argv(check.get("argv"), "check " + name)
        check["timeout_seconds"] = _number(check.get("timeout_seconds", 300), "check timeout", 0.1)

    steps = _object(data.setdefault("steps", {}), "steps")
    for phase in MODEL_PHASES:
        step = steps.setdefault(phase, {"adapter": "codex", "binary": "codex"})
        _normalize_step(step, phase, config_dir, check_paths)
    for phase in ("quick", "checks"):
        step = steps.setdefault(phase, {"adapter": "command", "argv": list(CHECK_COMMAND)})
        _normalize_step(step, phase, config_dir, check_paths)

    limits = _object(data.setdefault("limits", {}), "limits")
    for key, default, minimum in (("max_cycles", 3, 1), ("max_no_change_cycles", 2, 1),
                                  ("log_limit_bytes", 67108864, 1024)):
        limits[key] = _integer(limits.get(key, default), key, minimum)
    for key, default, minimum in (("max_wall_seconds", 604800, 1), ("poll_seconds", 1, 0.02)):
        limits[key] = _number(limits.get(key, default), key, minimum)
    timeouts = _object(limits.setdefault("timeouts", {}), "timeouts")
    for phase, default in (("analyze", 1800), ("plan", 1800), ("judge", 3600), ("reflect", 1800),
                            ("run", 86400), ("improve", 3600), ("quick", 300), ("checks", 3600)):
        timeouts[phase] = _number(timeouts.get(phase, default), "timeout " + phase, 0.1)

    inputs = autonomy.setdefault("evidence_inputs", [])
    if not isinstance(inputs, list):
        raise ValueError("autonomy.evidence_inputs must be a list")
    autonomy["evidence_inputs"] = list(dict.fromkeys(_path(p, "evidence input", config_dir) for p in inputs))
    for path in autonomy["evidence_inputs"]:
        if check_paths and not Path(path).is_file():
            raise ValueError("evidence input must be an existing file: " + path)
    validation = _object(data.setdefault("validation", {}), "validation")
    validation["key"] = _text(validation.get("key", digest(data["objective"])), "validation.key")
    validation.update(inputs=list(autonomy["evidence_inputs"]), min_gain=0, max_case_regression=0,
                      baseline_cache_ttl_seconds=0)
