"""Dataset isolation and deterministic historical candidate search policies."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest

from rsi_service.search import frontier_weights, load_tasks, normalize_optimizer, sample_train, select_parent


class SearchPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rsi-search-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tasks = {
            "train": [{"id": "train-%s" % i, "task": "training task %s" % i} for i in range(5)],
            "validation": [{"id": "val-1", "task": "validation task"}],
            "test": [{"id": "test-1", "task": "held-out task"}],
        }
        self.path = self.root / "dataset.json"
        self.config = {"validation": {"inputs": []},
                       "optimizer": {"strategy": "pareto", "dataset": "dataset.json"}}
        self.write_tasks()

    def write_tasks(self):
        self.path.write_text(json.dumps(self.tasks), encoding="utf-8")

    def normalized(self):
        normalize_optimizer(self.config, self.root, True)
        return self.config

    def test_legacy_configuration_is_unchanged(self):
        legacy = {"task": "run", "validation": {"inputs": ["checker.py"]}}
        original = copy.deepcopy(legacy)
        normalize_optimizer(legacy, self.root)
        self.assertEqual(original, legacy)

    def test_dataset_is_fingerprinted_once_without_embedding_contents(self):
        self.normalized()
        self.assertEqual(str(self.path), self.config["optimizer"]["dataset"])
        self.assertEqual([str(self.path)], self.config["validation"]["inputs"])
        self.assertEqual(42, self.config["optimizer"]["seed"])
        normalize_optimizer(self.config, self.root, True)
        self.assertEqual([str(self.path)], self.config["validation"]["inputs"])
        self.assertNotIn("train", self.config["optimizer"])
        self.assertEqual(self.tasks, load_tasks(self.config))

    def test_reattachment_does_not_require_dataset_mount(self):
        self.path.unlink()
        normalize_optimizer(self.config, self.root, False)
        self.assertEqual(str(self.path), self.config["optimizer"]["dataset"])
        with self.assertRaises(OSError):
            load_tasks(self.config)

    def test_dataset_split_ids_are_global_and_safe(self):
        for bad_id in ("train-0", "../escape", ".", "..", "a/b", "with space", "", 7):
            with self.subTest(bad_id=bad_id):
                self.tasks["test"][0]["id"] = bad_id
                self.write_tasks()
                with self.assertRaises(ValueError):
                    self.normalized()

    def test_renamed_duplicate_across_splits_is_rejected(self):
        self.tasks["test"][0]["task"] = " training task 0 "
        self.tasks["test"][0]["input"] = None
        self.write_tasks()
        with self.assertRaisesRegex(ValueError, "different dataset splits"):
            self.normalized()

    def test_same_instruction_with_different_inputs_is_distinct(self):
        self.tasks["validation"][0].update(task="classify", input={"error": "transient"})
        self.tasks["test"][0].update(task="classify", input={"error": "fatal"})
        self.write_tasks()
        self.normalized()

    def test_all_splits_are_required_and_nonempty(self):
        original = copy.deepcopy(self.tasks)
        for split in ("train", "validation", "test"):
            for missing in (False, True):
                with self.subTest(split=split, missing=missing):
                    self.tasks = copy.deepcopy(original)
                    if missing:
                        del self.tasks[split]
                    else:
                        self.tasks[split] = []
                    self.write_tasks()
                    with self.assertRaises(ValueError):
                        self.normalized()

    def test_nonfinite_input_and_nontext_task_are_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.tasks["train"][0]["input"] = {"nested": [value]}
                self.write_tasks()
                with self.assertRaises(ValueError):
                    self.normalized()
        self.tasks["train"][0].pop("input")
        self.tasks["train"][0]["task"] = []
        self.write_tasks()
        with self.assertRaises(ValueError):
            self.normalized()

    def test_numeric_and_strategy_configuration_validation(self):
        defaults = copy.deepcopy(self.config)
        for key, bad in (("seed", True), ("seed", -1), ("train_batch_size", 0),
                         ("max_metric_calls", 2), ("max_full_evaluations", 2),
                         ("max_metric_calls", 3.5), ("min_train_gain", math.nan),
                         ("min_train_gain", -0.01), ("strategy", "random")):
            with self.subTest(key=key, bad=bad):
                self.config = copy.deepcopy(defaults)
                self.config["optimizer"][key] = bad
                with self.assertRaises(ValueError):
                    self.normalized()

    def test_each_epoch_covers_every_task_once_without_duplicate_padding(self):
        original = copy.deepcopy(self.tasks)
        for epoch in range(3):
            batches = [sample_train(self.tasks, 42, epoch * 3 + i, 2) for i in range(3)]
            ids = [row["id"] for batch in batches for row in batch]
            self.assertEqual([2, 2, 1], [len(batch) for batch in batches])
            self.assertEqual(set(row["id"] for row in self.tasks["train"]), set(ids))
            self.assertEqual(5, len(ids))
        self.assertEqual(original, self.tasks)

    def test_sampling_restarts_deterministically_and_caps_oversized_batch(self):
        a = [sample_train(self.tasks, 7, i, 2) for i in range(8)]
        b = [sample_train(self.tasks, 7, i, 2) for i in range(8)]
        self.assertEqual(a, b)
        self.assertEqual(5, len(sample_train(self.tasks, 7, 0, 100)))
        self.assertNotEqual(a, [sample_train(self.tasks, 8, i, 2) for i in range(8)])

    def test_specialists_and_ties_survive_but_never_best_candidate_has_no_weight(self):
        pool = {
            "a": {"one": 1.0, "two": 0.0},
            "b": {"one": 0.0, "two": 1.0},
            "balanced": {"one": 0.8, "two": 0.8},
            "tied-a": {"one": 1.0, "two": 0.0},
        }
        self.assertEqual({"a": 1, "b": 1, "balanced": 0, "tied-a": 1}, frontier_weights(pool))
        selected = {select_parent(pool, 42, i) for i in range(100)}
        self.assertEqual({"a", "b", "tied-a"}, selected)

    def test_parent_sampling_is_order_independent_and_replayable(self):
        pool = {"older": {"x": 1, "y": 0}, "active": {"x": 0, "y": 1}}
        reversed_pool = dict(reversed(list(pool.items())))
        expected = [select_parent(pool, 42, i) for i in range(20)]
        self.assertEqual(expected, [select_parent(reversed_pool, 42, i) for i in range(20)])
        self.assertIn("older", expected)
        self.assertIn("active", expected)

    def test_pool_rejects_partial_or_invalid_scores(self):
        for pool in ({}, {"a": {}}, {"a": {"x": 1}, "b": {"y": 1}},
                     {"a": {"x": True}}, {"a": {"x": math.inf}},
                     {"a": {"x": math.nan}}, {"a": {"x": "1"}}, {"a": {"x": 10 ** 1000}}):
            with self.subTest(pool=pool):
                with self.assertRaises(ValueError):
                    frontier_weights(pool)


if __name__ == "__main__":
    unittest.main()
