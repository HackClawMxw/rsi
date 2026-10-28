"""Small, durable-search policies inspired by GEPA's per-example selection.

These are native policies, not an invocation or reimplementation of the GEPA
package.  The selector preserves candidates that are best on at least one
validation example; it is not a full multi-objective non-dominated sorter.
"""
from __future__ import annotations

import json
import math
import random
import re
from pathlib import Path

from .storage import digest


SPLITS = ("train", "validation", "test")
_ID = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(label + " must be an integer >= " + str(minimum))
    return value


def _finite(value, label, minimum=None):
    try:
        finite = math.isfinite(value) if isinstance(value, (int, float)) else False
    except OverflowError:
        finite = False
    if isinstance(value, bool) or not finite:
        raise ValueError(label + " must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(label + " must be >= " + str(minimum))
    return value


def _validate_tasks(data):
    if not isinstance(data, dict) or set(data) != set(SPLITS):
        raise ValueError("dataset must contain exactly train, validation and test")
    seen_ids, content_splits = set(), {}
    for split in SPLITS:
        examples = data[split]
        if not isinstance(examples, list) or not examples:
            raise ValueError("dataset split must be a nonempty list: " + split)
        for example in examples:
            if not isinstance(example, dict) or not {"id", "task"}.issubset(example):
                raise ValueError("each dataset example requires id and task")
            if set(example) - {"id", "task", "input"}:
                raise ValueError("example fields must be id, task, and optional input")
            example_id = example["id"]
            if not isinstance(example_id, str) or example_id in (".", "..") or not _ID.fullmatch(example_id):
                raise ValueError("example id must use only letters, digits, underscore, dot or hyphen")
            if example_id in seen_ids:
                raise ValueError("example IDs must be globally unique: " + example_id)
            seen_ids.add(example_id)
            task = example["task"]
            if not isinstance(task, str) or not task.strip() or "\x00" in task:
                raise ValueError("example task must be nonempty text without NUL")
            try:
                # Treat missing input and explicit null as the same task. Trim
                # outer whitespace so a renamed/copied example is not a split.
                identity = digest({"task": task.strip(), "input": example.get("input")})
            except (TypeError, ValueError) as error:
                raise ValueError("example input must be finite JSON data") from error
            previous_split = content_splits.get(identity)
            if previous_split is not None and previous_split != split:
                raise ValueError("identical task/input appears in different dataset splits")
            content_splits[identity] = split
    return data


def load_tasks(config):
    """Read and validate an operator-authored split manifest, without caching."""
    def reject_constant(value):
        raise ValueError("nonfinite JSON constant in dataset: " + value)

    path = Path(config["optimizer"]["dataset"])
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream, parse_constant=reject_constant)
    return _validate_tasks(data)


def normalize_optimizer(config, config_dir, check_paths=True):
    """Normalize the optional optimizer configuration, preserving legacy data.

    Dependency existence is deferred when reattaching an already-running job.
    The dataset is a fingerprint input, never embedded into model job contexts
    by this helper.  Runtime code chooses only the required split and batch.
    """
    if "optimizer" not in config:
        return
    optimizer = config["optimizer"]
    if not isinstance(optimizer, dict) or optimizer.get("strategy") != "pareto":
        raise ValueError("optimizer.strategy must be pareto")
    path = optimizer.get("dataset")
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("optimizer.dataset must name a dataset JSON file")
    optimizer["dataset"] = str((Path(config_dir) / Path(path).expanduser()).resolve())
    for key, default, minimum in (("seed", 42, 0), ("train_batch_size", 2, 1),
                                  ("max_metric_calls", 60, 1), ("max_full_evaluations", 8, 3)):
        optimizer[key] = _integer(optimizer.get(key, default), "optimizer." + key, minimum)
    optimizer["min_train_gain"] = _finite(optimizer.get("min_train_gain", 0.01),
                                           "optimizer.min_train_gain", 0)
    validation = config.get("validation")
    if not isinstance(validation, dict) or not isinstance(validation.get("inputs"), list):
        raise ValueError("optimizer requires validation.inputs")
    if optimizer["dataset"] not in validation["inputs"]:
        validation["inputs"].append(optimizer["dataset"])
    if check_paths:
        tasks = load_tasks(config)
        required = len(tasks["validation"]) + 2 * len(tasks["test"])
        if optimizer["max_metric_calls"] < required:
            raise ValueError("max_metric_calls must cover seed validation and both final test batches (>= %s)" % required)


def sample_train(tasks, seed, iteration, batch_size):
    """Deterministic epoch-shuffled training batch (last batch may be smaller).

    Each epoch uses every training example exactly once.  Deriving the epoch
    shuffle from seed and iteration makes crash/restart replay deterministic
    without persisting a mutable RNG or repeating items to fill a last batch.
    """
    _integer(seed, "seed")
    _integer(iteration, "iteration")
    _integer(batch_size, "batch_size", 1)
    examples = tasks.get("train") if isinstance(tasks, dict) else None
    if not isinstance(examples, list) or not examples:
        raise ValueError("sampling requires a nonempty train split")
    size = min(batch_size, len(examples))
    batches_per_epoch = (len(examples) + size - 1) // size
    epoch, batch = divmod(iteration, batches_per_epoch)
    ordered = list(examples)
    random.Random("rsi:train:%s:%s" % (seed, epoch)).shuffle(ordered)
    return ordered[batch * size:(batch + 1) * size]


def frontier_weights(pool_scores):
    """Count per-example champion appearances, including exact ties.

    Every candidate must be evaluated on the same nonempty validation set.
    A generally competitive candidate that is never best on a key has weight
    zero; this distinction from full Pareto non-domination is intentional.
    """
    if not isinstance(pool_scores, dict) or not pool_scores:
        raise ValueError("parent selection requires a nonempty candidate pool")
    keys = None
    for candidate, scores in pool_scores.items():
        if not isinstance(candidate, str) or not candidate:
            raise ValueError("candidate identities must be nonempty strings")
        if not isinstance(scores, dict) or not scores or any(not isinstance(k, str) or not k for k in scores):
            raise ValueError("candidate scores require named validation examples")
        if keys is None:
            keys = set(scores)
        elif set(scores) != keys:
            raise ValueError("candidate pool validation example sets differ")
        for value in scores.values():
            _finite(value, "candidate score")
    weights = {candidate: 0 for candidate in sorted(pool_scores)}
    for key in sorted(keys):
        best = max(scores[key] for scores in pool_scores.values())
        for candidate, scores in pool_scores.items():
            if scores[key] == best:
                weights[candidate] += 1
    return weights


def select_parent(pool_scores, seed, iteration):
    """Sample a historical parent by per-example weight, with deterministic RNG."""
    _integer(seed, "seed")
    _integer(iteration, "iteration")
    weights = frontier_weights(pool_scores)
    pick = random.Random("rsi:parent:%s:%s" % (seed, iteration)).randrange(sum(weights.values()))
    for candidate, weight in weights.items():
        if pick < weight:
            return candidate
        pick -= weight
    raise AssertionError("positive frontier weight must select a parent")
