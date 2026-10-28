"""GEPA-inspired, per-example candidate search on the durable worker protocol.

Search retains historical specialists; deployment remains a separate decision.
Only training evidence reaches the proposer. Test evaluation closes the search.
This is a native controller, not an invocation of the upstream GEPA package.
"""
from __future__ import annotations

import math
import shutil
import time
from pathlib import Path

from .config import validation_identity
from .controller import Controller, receipt, verify_receipt, worker_busy
from .search import frontier_weights, load_tasks, sample_train, select_parent
from .storage import atomic_json, checked_diff, digest, manifest, read_json, snapshot


def compare(candidate, baseline, policy):
    """Deployment gate against the original active version, not the parent."""
    if candidate["passed"] is not True:
        raise ValueError("candidate hard checks failed")
    if candidate["cases"].keys() != baseline["cases"].keys():
        raise ValueError("comparison case sets differ")
    if any(candidate["cases"][k] < baseline["cases"][k] - policy["max_case_regression"] for k in baseline["cases"]):
        raise ValueError("candidate regressed on a comparison case")
    if candidate["score"] < baseline["score"] + policy["min_gain"]:
        raise ValueError("candidate did not meet minimum measured gain")


class SearchController(Controller):
    def __init__(self, config):
        super().__init__(config)
        task_path = self.root / "search-tasks.json"
        if "search" not in self.state:
            tasks = load_tasks(config)
            atomic_json(task_path, tasks)
            self.state["search"] = {
                "algorithm": "native_per_example_pareto", "seed_version": self.state["active_version"],
                "validation_identity": validation_identity(config), "dataset_digest": digest(tasks),
                "pool": {}, "cache": {}, "metric_calls": 0, "full_evaluations": 0,
                "reservations": {}, "finalizing": False, "finalist": None, "result": None,
            }
            self.save()
        # Reattachment uses the frozen manifest, even if a source mount is lost.
        self.tasks = read_json(task_path)
        if digest(self.tasks) != self.search["dataset_digest"]:
            raise ValueError("frozen task manifest changed")

    @property
    def search(self):
        return self.state["search"]

    @property
    def policy(self):
        return self.config["optimizer"]

    def integrity(self, cycle):
        super().integrity(cycle)
        if cycle["validation_identity"] != self.search["validation_identity"]:
            raise ValueError("search evaluator/dataset identity changed; start a new experiment")
        if digest(read_json(self.root / "search-tasks.json")) != self.search["dataset_digest"]:
            raise ValueError("frozen task manifest changed")

    def new_cycle(self, cycle_id, kind, parent, phase, **extra):
        directory = self.root / "cycles" / cycle_id
        candidate = directory / "candidate"
        if digest(manifest(self.version(parent))) != parent:
            raise ValueError("selected parent snapshot changed")
        if candidate.exists():
            shutil.rmtree(candidate)  # only unregistered copy work can reach here
        candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.version(parent), candidate)
        (directory / "reflection").mkdir(exist_ok=True)
        cycle = {"id": cycle_id, "kind": kind, "baseline": parent, "phase": phase,
                 "active_at_start": self.state["active_version"], "jobs": [], "job": None,
                 "receipts": {}, "status": "running", "started_at": time.time(),
                 "validation_identity": self.search["validation_identity"], **extra}
        self.state["cycles"].append(cycle)
        self.state["current"] = len(self.state["cycles"]) - 1
        self.state["status"] = "running"
        self.save()
        self.event("search_cycle_started", cycle=cycle_id, cycle_kind=kind, parent=parent)

    def pool_scores(self):
        scores = {}
        for anchor, item in self.search["pool"].items():
            if not verify_receipt(item["validation"]):
                raise ValueError("candidate-pool evidence changed: " + anchor)
            scores[anchor] = item["validation"]["result"]["cases"]
        return scores

    def start_cycle(self):
        if validation_identity(self.config) != self.search["validation_identity"]:
            raise ValueError("evaluator/dataset changed during search; start a new experiment")
        if self.search["finalizing"]:
            self.start_finalization()
            return
        if not self.search["pool"]:
            self.new_cycle("seed%04d" % len(self.state["cycles"]), "bootstrap",
                           self.search["seed_version"], "seed_validation")
            return
        iteration = sum(c.get("kind") == "search" for c in self.state["cycles"])
        if (iteration >= self.config["limits"]["max_cycles"] or self.remaining() <= 0
                or self.state["no_change_cycles"] >= self.config["limits"]["max_no_change_cycles"]):
            self.search["finalizing"] = True
            self.save()
            return
        scores = self.pool_scores()
        parent = select_parent(scores, self.policy["seed"], iteration)
        batch = sample_train(self.tasks, self.policy["seed"], iteration, self.policy["train_batch_size"])
        self.new_cycle("s%04d" % (iteration + 1), "search", parent, "parent_train",
                       train_batch=batch, parent_weights=frontier_weights(scores))
        self.event("parent_selected", parent=parent, iteration=iteration,
                   weights=frontier_weights(scores), examples=[e["id"] for e in batch])

    def finish(self, cycle, status, reason):
        cycle.update(status=status, reason=reason, finished_at=time.time(), job=None)
        self.state["current"] = None
        if cycle["kind"] == "search":
            progress = status == "pooled" and cycle.get("frontier_changed", False)
            self.state["no_change_cycles"] = 0 if progress else self.state["no_change_cycles"] + 1
        self.save()
        self.event("search_cycle_finished", cycle=cycle["id"], status=status, reason=reason)

    def finish_search(self, status, reason):
        cycle = self.current()
        if cycle:
            cycle.update(status=status, reason=reason, job=None, finished_at=time.time())
        self.state["current"] = None
        self.state["status"] = "completed"
        self.state["reason"] = reason
        self.search["finalizing"] = True
        self.search["result"] = {"status": status, "reason": reason,
                                 "finalist": self.search["finalist"],
                                 "active_version": self.state["active_version"]}
        self.save()
        self.event("search_completed", **self.search["result"])

    def start_finalization(self):
        if self.remaining() <= 0:
            self.finish_search("budget_exhausted", "wall-time budget exhausted; no further evaluation")
            return
        if not self.search["pool"]:
            self.finish_search("no_finalist", "no completed validation baseline is available")
            return
        self.pool_scores()  # verify artifacts before any selection
        seed = self.search["pool"][self.search["seed_version"]]["validation"]["result"]
        eligible = []
        for anchor, item in self.search["pool"].items():
            if anchor == self.search["seed_version"]:
                continue
            try:
                compare(item["validation"]["result"], seed, self.config["validation"])
            except ValueError:
                continue
            if not item.get("quick") or not verify_receipt(item["quick"]):
                raise ValueError("finalist quick-check evidence changed")
            eligible.append(anchor)
        if not eligible:
            self.finish_search("no_finalist", "no candidate meets deployment gates on validation")
            return
        finalist = min(eligible, key=lambda a: (-self.search["pool"][a]["validation"]["result"]["score"], a))
        self.search["finalist"] = finalist
        self.new_cycle("final", "final", self.search["seed_version"], "final_seed",
                       candidate=finalist)
        self.event("finalist_frozen", finalist=finalist, test_feedback_will_not_train=True)

    def budget_allows(self, split, count):
        reserve_calls = 0 if self.search["finalizing"] else 2 * len(self.tasks["test"])
        reserve_full = 0 if self.search["finalizing"] else 2
        return (self.search["metric_calls"] + count + reserve_calls <= self.policy["max_metric_calls"]
                and self.search["full_evaluations"] + int(split != "train") + reserve_full
                <= self.policy["max_full_evaluations"])

    def evaluation_spec(self, cycle):
        phase = cycle["phase"]
        if phase == "seed_validation":
            return cycle["baseline"], "validation", self.tasks["validation"], "seed"
        if phase in ("parent_train", "candidate_train"):
            candidate = phase == "candidate_train"
            return cycle["candidate"] if candidate else cycle["baseline"], "train", cycle["train_batch"], "candidate" if candidate else "parent"
        if phase == "candidate_validation":
            return cycle["candidate"], "validation", self.tasks["validation"], "candidate"
        if phase in ("final_seed", "final_candidate"):
            candidate = phase == "final_candidate"
            return cycle["candidate"] if candidate else cycle["baseline"], "test", self.tasks["test"], "finalist" if candidate else "final_seed"
        return None

    def check_batch(self, record, split, examples, batch_digest):
        result = record["result"]
        keys = {e["id"] for e in examples}
        if result.get("split") != split or result.get("batch_digest") != batch_digest:
            raise ValueError("evaluation split/batch identity mismatch")
        if set(result["cases"]) != keys:
            raise ValueError("evaluation cases do not match the requested batch")
        try:
            mean = math.fsum(v / len(keys) for v in result["cases"].values())
        except (OverflowError, ArithmeticError) as error:
            raise ValueError("case score arithmetic overflow") from error
        if not math.isclose(result["score"], mean, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("search score must be the arithmetic mean of case scores")
        if split == "train":
            info = result.get("side_info")
            if not isinstance(info, dict) or set(info) != keys or any(not isinstance(v, dict) or not v for v in info.values()):
                raise ValueError("training evaluations require per-example diagnostic side_info")

    def launch(self, cycle):
        self.integrity(cycle)
        phase = cycle["phase"]
        spec = self.evaluation_spec(cycle)
        directory = self.root / "cycles" / cycle["id"]
        job_id = cycle["id"] + "-" + phase
        context = {"cycle_id": cycle["id"], "workspace": self.config["workspace"],
                   "baseline_dir": str(self.version(cycle["baseline"])),
                   "candidate_dir": str(directory / "candidate"), "run_dir": str(directory / "reflection"),
                   "result_path": str(self.root / "jobs" / job_id / "output" / "result.json"),
                   "validation_key": self.config["validation"]["key"],
                   "validation_identity": self.search["validation_identity"]}
        if spec:
            anchor, split, examples, role = spec
            batch = digest(examples)
            key = digest([anchor, split, batch, self.search["validation_identity"]])
            cached = self.search["cache"].get(key)
            ttl = self.config["validation"]["baseline_cache_ttl_seconds"]
            if cached and ttl > 0 and time.time() - cached["at"] <= ttl and verify_receipt(cached):
                self.check_batch(cached, split, examples, batch)
                self.event("search_evaluation_reused", cycle=cycle["id"], phase=phase, job=cached["job_id"])
                self.accept_evaluation(cycle, cached)
                return
            reserved = job_id in self.search["reservations"]
            if not reserved and not self.budget_allows(split, len(examples)):
                if self.search["finalizing"]:
                    self.finish_search("budget_exhausted", "held-out evaluation budget unavailable; version unchanged")
                else:
                    self.finish(cycle, "budget_exhausted", "remaining evaluation budget reserved for final test")
                    self.search["finalizing"] = True
                    self.save()
                return
            context.update(split=split, examples=examples, batch_digest=batch, evaluation_role=role)
            # Reserve before durable launch intent; failures consume budget and
            # reattachment cannot charge/execute the same job twice.
            if not reserved:
                self.search["reservations"][job_id] = {"metric_calls": len(examples), "full_evaluations": int(split != "train")}
                self.search["metric_calls"] += len(examples)
                self.search["full_evaluations"] += int(split != "train")
                self.save()
            stage = "evaluate"
        elif phase == "improve":
            anchor, stage = cycle["baseline"], "improve"
            context["reflection_data"] = cycle["reflection_data"]
        elif phase == "quick":
            anchor, stage = cycle["candidate"], "quick"
        elif phase == "deployment_run":
            anchor, stage = cycle["candidate"], "run"
        else:
            raise ValueError("unknown search phase: " + phase)
        context["skill_digest"] = anchor
        context["skill_dir"] = str(directory / "candidate" if phase == "improve" else self.version(anchor))
        self.submit(cycle, job_id, stage, anchor, context)

    def accept_evaluation(self, cycle, record):
        phase = cycle["phase"]
        cycle["receipts"][phase] = record
        if phase == "seed_validation":
            self.search["pool"][cycle["baseline"]] = {"parents": [], "cycle": cycle["id"], "validation": record, "quick": None}
            self.finish(cycle, "seeded", "original skill evaluated on validation split")
            return
        if phase == "parent_train":
            result = record["result"]
            feedback = [{"example_id": e["id"], "task": e["task"], "input": e.get("input"),
                         "score": result["cases"][e["id"]],
                         "side_info": result["side_info"][e["id"]], "evidence_job": record["job_id"]}
                        for e in cycle["train_batch"]]
            cycle["reflection_data"] = feedback
            atomic_json(self.root / "cycles" / cycle["id"] / "reflection" / "feedback.json", feedback)
            cycle["phase"] = "improve"
        elif phase == "candidate_train":
            before = cycle["receipts"]["parent_train"]
            if not verify_receipt(before):
                raise ValueError("parent training evidence changed")
            if record["result"]["score"] < before["result"]["score"] + self.policy["min_train_gain"]:
                self.finish(cycle, "screened_out", "candidate did not improve the same training minibatch; skipped validation")
                return
            cycle["phase"] = "candidate_validation"
        elif phase == "candidate_validation":
            self.pool_scores()
            self.search["pool"][cycle["candidate"]] = {
                "parents": [cycle["baseline"]], "cycle": cycle["id"], "validation": record,
                "quick": cycle["quick_evaluation"],
            }
            new = self.pool_scores()
            weights = frontier_weights(new)
            cycle["frontier_changed"] = weights[cycle["candidate"]] > 0
            self.finish(cycle, "pooled", "candidate archived with validation scores; deployment unchanged")
            return
        elif phase == "final_seed":
            cycle["phase"] = "final_candidate"
        elif phase == "final_candidate":
            baseline = cycle["receipts"]["final_seed"]
            finalist = self.search["pool"][cycle["candidate"]]
            seed = self.search["pool"][cycle["baseline"]]
            if not all(verify_receipt(r) for r in (baseline, finalist["validation"], finalist["quick"], seed["validation"])):
                raise ValueError("finalization evidence changed")
            try:
                compare(record["result"], baseline["result"], self.config["validation"])
            except ValueError as error:
                self.finish_search("test_rejected", "held-out test failed: " + str(error))
                return
            self.integrity(cycle)
            self.state["active_version"] = cycle["candidate"]
            cycle["phase"] = "deployment_run"
        cycle["job"] = None
        self.save()
        if phase == "final_candidate":
            self.event("search_version_promoted", skill_digest=cycle["candidate"])

    def complete_job(self, cycle, job, status):
        job["completion"] = status
        self.integrity(cycle)
        if status["state"] != "completed" or status.get("returncode") != 0:
            self.block(cycle["phase"] + ": " + status["state"] + "; inspect execution before recovery")
            return
        spec = self.evaluation_spec(cycle)
        if spec:
            anchor, split, examples, _ = spec
            record = receipt(job, anchor, self.config["validation"]["key"], evaluation=True, require_pass=False)
            batch = digest(examples)
            self.check_batch(record, split, examples, batch)
            self.search["cache"][digest([anchor, split, batch, self.search["validation_identity"]])] = record
            self.accept_evaluation(cycle, record)
            return
        path = Path(job["dir"]) / "output" / "result.json"
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("invalid or oversized result")
        result = read_json(path)
        if not isinstance(result, dict):
            raise ValueError("job result must be a JSON object")
        if cycle["phase"] == "improve":
            if not isinstance(result.get("changed"), bool) or not isinstance(result.get("summary"), str):
                raise ValueError("invalid improvement result")
            proposal = self.root / "cycles" / cycle["id"] / "candidate"
            try:
                changes = checked_diff(self.version(cycle["baseline"]), proposal,
                                       self.config["allowed_paths"], self.config["protected_paths"])
                if not changes:
                    self.finish(cycle, "no_change", "no candidate changes; skipped evaluation")
                    return
                anchor = snapshot(proposal, self.root / "versions")
                changes = checked_diff(self.version(cycle["baseline"]), self.version(anchor),
                                       self.config["allowed_paths"], self.config["protected_paths"])
            except ValueError as error:
                self.finish(cycle, "rejected", str(error))
                return
            if anchor in self.search["pool"]:
                self.finish(cycle, "duplicate", "candidate already in pool; skipped evaluation")
                return
            cycle.update(candidate=anchor, changes=changes, phase="quick")
        elif cycle["phase"] == "quick":
            try:
                cycle["quick_evaluation"] = receipt(job)
            except (ValueError, OSError) as error:
                self.finish(cycle, "rejected", "quick gate: " + str(error))
                return
            cycle["phase"] = "candidate_train"
        elif cycle["phase"] == "deployment_run":
            if result.get("outcome") not in ("signed", "pending", "aborted") or not isinstance(result.get("summary"), str) or not isinstance(result.get("evidence"), list):
                raise ValueError("invalid deployment run result")
            cycle["run_result"] = result
            if result["outcome"] != "signed":
                self.block("selected version run is " + result["outcome"] + "; search will not consume held-out feedback")
                return
            self.finish_search("promoted", "held-out comparison passed; next CLI run used the selected skill")
            return
        cycle["job"] = None
        self.save()

    def recover(self):
        cycle = self.current()
        if self.state["status"] == "blocked" and self.search["finalizing"]:
            if cycle and cycle.get("job") and worker_busy(cycle["job"]["dir"]):
                raise ValueError("worker is still running; cancel it or wait before recovery")
            self.finish_search("abandoned", "operator abandoned finalization; no further search on test feedback")
        else:
            super().recover()
