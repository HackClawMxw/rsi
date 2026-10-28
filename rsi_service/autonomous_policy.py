"""Deterministic promotion policy; a model's claimed outcome is not a gate."""
from __future__ import annotations

import copy
import math

from .autonomous_config import normalize_objective


def _finite(value):
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


def _rows(assessment, key, identity):
    if not isinstance(assessment, dict) or not isinstance(assessment.get(key), list):
        raise ValueError("assessment." + key + " must be a list")
    rows = {}
    for row in assessment[key]:
        if not isinstance(row, dict) or not isinstance(row.get(identity), str) or not row[identity].strip():
            raise ValueError("invalid assessment " + key + " identity")
        name = row[identity]
        if name in rows:
            raise ValueError("duplicate assessment " + key + " identity: " + name)
        if "evidence_refs" in row and (not isinstance(row["evidence_refs"], list)
                                       or any(not isinstance(ref, str) or not ref.strip() for ref in row["evidence_refs"])):
            raise ValueError("assessment evidence_refs must be a string array")
        rows[name] = row
    return rows


def compare_assessments(baseline, candidate, objective):
    """Require supported gates, bounded regressions and positive weighted gain.

    Deltas and thresholds are in each metric's declared units. The operator's
    weights perform any desired scaling. Missing/null/nonfinite measurements
    are indeterminate and cannot authorize promotion. Baseline gate failures
    are allowed to be repaired, while unknown baseline gates are not evidence.
    """
    objective = normalize_objective(objective)
    result = {"promote": False, "reason": "", "gains": {}, "weighted_gain": None}

    def reject(reason):
        result["reason"] = reason
        return result

    baseline_gates = _rows(baseline, "gates", "id")
    candidate_gates = _rows(candidate, "gates", "id")
    gate_ids = {gate["id"] for gate in objective["gates"]}
    if set(baseline_gates) != gate_ids or set(candidate_gates) != gate_ids:
        return reject("gate set differs from the fixed objective")
    for gate_id in sorted(gate_ids):
        if baseline_gates[gate_id].get("status") not in ("pass", "fail"):
            return reject("baseline gate is unknown: " + gate_id)
        if candidate_gates[gate_id].get("status") != "pass":
            return reject("candidate gate did not pass: " + gate_id)

    baseline_metrics = _rows(baseline, "metrics", "name")
    candidate_metrics = _rows(candidate, "metrics", "name")
    metrics = {metric["name"]: metric for metric in objective["metrics"]}
    if set(baseline_metrics) != set(metrics) or set(candidate_metrics) != set(metrics):
        return reject("metric set differs from the fixed objective")
    for name in sorted(metrics):
        a, b = baseline_metrics[name].get("value"), candidate_metrics[name].get("value")
        if not _finite(a) or not _finite(b):
            return reject("metric is missing or nonfinite: " + name)
        gain = b - a if metrics[name]["direction"] == "maximize" else a - b
        if not _finite(gain):
            return reject("metric delta is nonfinite: " + name)
        result["gains"][name] = gain
    weighted = [metrics[name]["weight"] * gain for name, gain in result["gains"].items()]
    if not all(_finite(value) for value in weighted):
        return reject("weighted gain is nonfinite")
    try:
        result["weighted_gain"] = math.fsum(weighted)
    except (OverflowError, ValueError):
        return reject("weighted gain is nonfinite")
    if not _finite(result["weighted_gain"]):
        result["weighted_gain"] = None
        return reject("weighted gain is nonfinite")
    for name, gain in result["gains"].items():
        if gain < -metrics[name]["max_regression"]:
            return reject("metric exceeded maximum regression: " + name)
    if not any(gain > 0 and gain >= metrics[name]["min_gain"] for name, gain in result["gains"].items()):
        return reject("no metric achieved a positive minimum gain")
    if result["weighted_gain"] <= 0:
        return reject("weighted gain must be positive")
    result.update(promote=True, reason="all gates passed and measured gains satisfy the fixed objective")
    return result


def apply_measured_metrics(assessment, objective, stats):
    """Copy an assessment and replace worker-measured metrics, never impute them.

    Machine references identify worker statistic keys. Judge references remain
    separately available for audit but do not attribute machine values to logs
    or events the model may have invented.
    """
    objective = normalize_objective(objective)
    if not isinstance(stats, dict):
        raise ValueError("worker stats must be an object")
    measured = copy.deepcopy(assessment)
    rows = _rows(measured, "metrics", "name")
    for metric in objective["metrics"]:
        source = metric["source"]
        if source == "judge":
            continue
        name = metric["name"]
        if name not in rows:
            # Absence still prevents comparison; the worker does not supply
            # missing judge schema or silently fill the required metric set.
            continue
        row = rows[name]
        value = stats.get(source)
        valid = _finite(value) and value >= 0
        if source in ("tool_calls", "failed_tools"):
            valid = valid and isinstance(value, int)
        row["value"] = value if valid else None
        row["judge_evidence_refs"] = row.get("evidence_refs", [])
        row["evidence_refs"] = ["worker.stats." + source] if valid else []
        row.pop("refs", None)
        row["provenance"] = "worker"
        row["source"] = source
    return measured
