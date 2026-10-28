"""Single-flight evolution state machine; execution lives in detached workers."""
from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .adapters import prepare
from .config import validation_identity
from .storage import atomic_json, checked_diff, digest, file_hash, manifest, read_json, snapshot

TERMINAL = {"completed", "failed", "timed_out", "cancelled"}


@contextmanager
def controller_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "controller.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another controller is already running")
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def worker_busy(job_dir):
    """Probe the worker-owned lock, never trust/reuse stored PIDs."""
    with (Path(job_dir) / "worker.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def receipt(job, expected_skill=None, expected_key=None, evaluation=False, require_pass=True):
    """Validate independent checker output and retain content-bound evidence."""
    output = Path(job["dir"]) / "output"
    path = output / "result.json"
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("invalid/oversized checker result")
    result = read_json(path)
    if not isinstance(result, dict):
        raise ValueError("checker result must be a JSON object")
    if not isinstance(result.get("passed"), bool):
        raise ValueError("checker must report a boolean passed result")
    if require_pass and result["passed"] is not True:
        raise ValueError("checker did not pass")
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("checker must supply evidence files")
    artifacts = {str(path): file_hash(path)}
    for name in evidence:
        if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("evidence must use relative paths inside job output")
        item = output / name
        if any(p.is_symlink() for p in [item] + list(item.parents) if p != output.parent):
            raise ValueError("evidence may not use symlinks")
        if not item.is_file() or output.resolve() not in item.resolve().parents:
            raise ValueError("missing/escaping evidence: " + name)
        artifacts[str(item)] = file_hash(item)
    if evaluation:
        if result.get("skill_digest") != expected_skill or result.get("validation_key") != expected_key:
            raise ValueError("evaluation does not identify the selected skill/environment")
        cases = result.get("cases")
        if not isinstance(cases, dict) or not cases or any(not isinstance(k, str) or not k for k in cases):
            raise ValueError("evaluation requires named case scores")
        for value in [result.get("score")] + list(cases.values()):
            try:
                finite = isinstance(value, (int, float)) and math.isfinite(value)
            except OverflowError:
                finite = False
            if isinstance(value, bool) or not finite:
                raise ValueError("evaluation scores must be finite numbers")
    finished_at = job.get("completion", {}).get("finished_at")
    try:
        valid_time = isinstance(finished_at, (int, float)) and math.isfinite(finished_at)
    except OverflowError:
        valid_time = False
    if isinstance(finished_at, bool) or not valid_time:
        raise ValueError("checker receipt requires worker completion time")
    return {"job_id": job["id"], "at": finished_at, "result": result, "artifacts": artifacts}


def verify_receipt(record):
    try:
        return all(not Path(p).is_symlink() and file_hash(p) == h for p, h in record["artifacts"].items())
    except (OSError, KeyError):
        return False


class Controller:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["state_dir"])
        self.path = self.root / "state.json"
        self.children = []
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.state = read_json(self.path)
            if self.state["config_digest"] != digest(config):
                raise ValueError("configuration changed; use a new state_dir for a new experiment")
        else:
            anchor = snapshot(config["skill_source"], self.root / "versions")
            self.state = {"schema_version": 1, "config_digest": digest(config),
                          "active_version": anchor, "started_at": time.time(),
                          "status": "paused", "cycles": [], "current": None,
                          "cache": {}, "no_change_cycles": 0}
            atomic_json(self.root / "config.json", config)
            self.save()
            self.event("initialized", skill_digest=anchor)

    def save(self):
        self.state["updated_at"] = time.time()
        atomic_json(self.path, self.state)
        for cycle in self.state["cycles"][-1:]:
            atomic_json(self.root / "cycles" / cycle["id"] / "cycle.json", cycle)

    def event(self, kind, **fields):
        item = {"at": time.time(), "event": kind, **fields}
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def version(self, anchor):
        return self.root / "versions" / anchor

    def current(self):
        if self.state["current"] is None:
            return None
        return self.state["cycles"][self.state["current"]]

    def remaining(self):
        return self.config["limits"]["max_wall_seconds"] - (time.time() - self.state["started_at"])

    def block(self, reason):
        self.state["status"] = "blocked"
        self.state["reason"] = str(reason)
        self.save()
        self.event("blocked", reason=str(reason))

    def integrity(self, cycle):
        for key in ("baseline", "candidate"):
            anchor = cycle.get(key)
            if anchor and digest(manifest(self.version(anchor))) != anchor:
                raise ValueError("managed skill snapshot was modified: " + anchor)
        if validation_identity(self.config) != cycle["validation_identity"]:
            raise ValueError("evaluator/workload inputs changed during cycle")

    def start_cycle(self):
        index = len(self.state["cycles"])
        limits = self.config["limits"]
        if index >= limits["max_cycles"] or self.remaining() <= 0 or self.state["no_change_cycles"] >= limits["max_no_change_cycles"]:
            self.state["status"] = "completed"
            self.state["reason"] = "cycle, wall-time, or no-progress budget reached"
            self.save()
            self.event("budget_reached")
            return
        cycle_id = "c%04d" % (index + 1)
        directory = self.root / "cycles" / cycle_id
        baseline = self.state["active_version"]
        if digest(manifest(self.version(baseline))) != baseline:
            raise ValueError("active skill snapshot was modified")
        candidate = directory / "candidate"
        if candidate.exists():
            # No job was registered for this cycle; a crash interrupted only copytree.
            shutil.rmtree(candidate)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.version(baseline), candidate)
        cycle = {"id": cycle_id, "baseline": baseline, "phase": "run", "jobs": [],
                 "job": None, "started_at": time.time(), "status": "running",
                 "validation_identity": validation_identity(self.config)}
        self.state["cycles"].append(cycle)
        self.state["current"] = index
        self.state["status"] = "running"
        self.save()
        self.event("cycle_started", cycle=cycle_id, baseline=baseline)

    def launch(self, cycle):
        self.integrity(cycle)
        phase = cycle["phase"]
        stage = "evaluate" if phase.startswith("evaluate_") else phase
        anchor = cycle.get("candidate", cycle["baseline"]) if phase in ("quick", "evaluate_candidate") else cycle["baseline"]
        directory = self.root / "cycles" / cycle["id"]
        selected = directory / "candidate" if phase == "improve" else self.version(anchor)
        job_id = cycle["id"] + "-" + phase
        job_dir = self.root / "jobs" / job_id
        if job_dir.exists():
            raise ValueError("unregistered job directory already exists; inspect before recovery: " + job_id)
        context = {"cycle_id": cycle["id"], "workspace": self.config["workspace"],
                   "skill_dir": str(selected), "skill_digest": anchor,
                   "baseline_dir": str(self.version(cycle["baseline"])),
                   "candidate_dir": str(directory / "candidate"), "run_dir": str(directory),
                   "result_path": str(job_dir / "output" / "result.json"),
                   "validation_key": self.config["validation"]["key"],
                   "validation_identity": cycle["validation_identity"],
                   "evaluation_role": phase.removeprefix("evaluate_") if stage == "evaluate" else None,
                   "previous_result": cycle.get("run_result"), "run_job": cycle.get("run_job")}
        self.submit(cycle, job_id, stage, anchor, context)

    def submit(self, cycle, job_id, stage, anchor, context):
        """Persist dispatch intent and launch one worker (shared by search mode)."""
        job_dir = self.root / "jobs" / job_id
        if job_dir.exists():
            raise ValueError("unregistered job directory already exists; inspect before recovery: " + job_id)
        request = prepare(self.config, stage, context, job_dir)
        request["env"]["PYTHONDONTWRITEBYTECODE"] = "1"
        request.update(timeout_seconds=min(self.remaining(), self.config["limits"]["timeouts"][stage]),
                       log_limit_bytes=self.config["limits"]["log_limit_bytes"])
        if request["timeout_seconds"] <= 0:
            raise ValueError("wall-time budget exhausted before job launch")
        atomic_json(job_dir / "request.json", request)
        job = {"id": job_id, "dir": str(job_dir), "phase": cycle["phase"], "skill_digest": anchor,
               "submitted_at": time.time()}
        cycle["job"] = job
        cycle["jobs"].append(job)
        self.save()  # commit intent before Popen; uncertain launches are never replayed
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        with (job_dir / "worker.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "rsi_service.worker", str(job_dir)],
                                       env=env, cwd=str(self.root), stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
        self.children.append(process)
        self.event("job_submitted", cycle=cycle["id"], job=job_id, phase=cycle["phase"])

    def finish(self, cycle, status, reason):
        cycle.update(status=status, reason=reason, finished_at=time.time(), job=None)
        self.state["current"] = None
        self.state["no_change_cycles"] = 0 if status == "promoted" else self.state["no_change_cycles"] + 1
        self.save()
        self.event("cycle_finished", cycle=cycle["id"], status=status, reason=reason,
                   active_version=self.state["active_version"])

    def cache_key(self, cycle):
        return digest([cycle["baseline"], cycle["validation_identity"]])

    def select_baseline(self, cycle):
        cached = self.state["cache"].get(self.cache_key(cycle))
        ttl = self.config["validation"]["baseline_cache_ttl_seconds"]
        if cached and ttl > 0 and time.time() - cached["at"] <= ttl and verify_receipt(cached):
            cycle["baseline_evaluation"] = cached
            cycle["phase"] = "evaluate_candidate"
            self.event("baseline_reused", cycle=cycle["id"], job=cached["job_id"])
        else:
            cycle["phase"] = "evaluate_baseline"

    def complete_job(self, cycle, job, status):
        job["completion"] = status
        self.integrity(cycle)
        phase = cycle["phase"]
        if status["state"] != "completed" or status.get("returncode") != 0:
            if phase in ("quick", "evaluate_candidate") and status["state"] == "failed":
                self.finish(cycle, "rejected", phase + " command failed; see job logs")
            else:
                self.block(phase + ": " + status["state"] + "; inspect job logs before recovery")
            return
        output = Path(job["dir"]) / "output" / "result.json"
        if output.is_symlink() or output.stat().st_size > 1024 * 1024:
            raise ValueError("invalid or oversized final result")
        result = read_json(output)
        if not isinstance(result, dict):
            raise ValueError("final result must be a JSON object")
        if phase == "run":
            if result.get("outcome") not in ("signed", "pending", "aborted") or not isinstance(result.get("summary"), str) or not isinstance(result.get("evidence"), list):
                raise ValueError("invalid run result")
            cycle["run_result"], cycle["run_job"] = result, job
            if result["outcome"] == "pending":
                self.block("Day0 needs inputs/authorization; inspect run result before recovery")
                return
            cycle["phase"] = "improve"
        elif phase == "improve":
            if not isinstance(result.get("changed"), bool) or not isinstance(result.get("summary"), str):
                raise ValueError("invalid improvement result")
            candidate = self.root / "cycles" / cycle["id"] / "candidate"
            try:
                changes = checked_diff(self.version(cycle["baseline"]), candidate,
                                       self.config["allowed_paths"], self.config["protected_paths"])
            except ValueError as error:
                self.finish(cycle, "rejected", str(error))
                return
            cycle["changes"] = changes
            if not changes:
                self.finish(cycle, "no_change", "candidate content unchanged; skipped validation")
                return
            cycle["candidate"] = snapshot(candidate, self.root / "versions")
            # Validate the exact stored content; the editable proposal may have
            # changed between the initial diff and snapshot creation.
            try:
                cycle["changes"] = checked_diff(self.version(cycle["baseline"]), self.version(cycle["candidate"]),
                                                self.config["allowed_paths"], self.config["protected_paths"])
            except ValueError as error:
                self.finish(cycle, "rejected", str(error))
                return
            cycle["phase"] = "quick"
        elif phase == "quick":
            try:
                cycle["quick_evaluation"] = receipt(job)
            except (ValueError, OSError) as error:
                self.finish(cycle, "rejected", "quick gate: " + str(error))
                return
            self.select_baseline(cycle)
        elif phase == "evaluate_baseline":
            # A valid measured failure is a useful baseline for fixing a skill.
            # Infrastructure failures/malformed evidence still block the cycle.
            record = receipt(job, cycle["baseline"], self.config["validation"]["key"],
                             evaluation=True, require_pass=False)
            cycle["baseline_evaluation"] = record
            self.state["cache"][self.cache_key(cycle)] = record
            cycle["phase"] = "evaluate_candidate"
        elif phase == "evaluate_candidate":
            try:
                candidate = receipt(job, cycle["candidate"], self.config["validation"]["key"], True)
                baseline = cycle["baseline_evaluation"]
                if not verify_receipt(baseline) or not verify_receipt(cycle["quick_evaluation"]):
                    raise ValueError("baseline/quick evidence changed after validation")
                a, b = baseline["result"], candidate["result"]
                policy = self.config["validation"]
                if a["cases"].keys() != b["cases"].keys():
                    raise ValueError("baseline/candidate case sets differ")
                if any(b["cases"][k] < a["cases"][k] - policy["max_case_regression"] for k in a["cases"]):
                    raise ValueError("candidate regressed on a comparison case")
                if b["score"] < a["score"] + policy["min_gain"]:
                    raise ValueError("candidate did not meet minimum measured gain")
                cycle["candidate_evaluation"] = candidate
                self.integrity(cycle)
            except (ValueError, OSError) as error:
                self.finish(cycle, "rejected", "full gate: " + str(error))
                return
            self.state["active_version"] = cycle["candidate"]
            self.state["cache"][digest([cycle["candidate"], cycle["validation_identity"]])] = candidate
            self.finish(cycle, "promoted", "independent comparison passed; selected for next cycle")
            return
        cycle["job"] = None
        self.save()
        self.event("phase_completed", cycle=cycle["id"], phase=phase, next_phase=cycle["phase"])

    def tick(self):
        self.children = [p for p in self.children if p.poll() is None]
        if self.state["status"] in ("completed", "blocked"):
            return False
        try:
            cycle = self.current()
            if cycle is None:
                self.start_cycle()
            elif cycle["job"] is None:
                self.launch(cycle)
            else:
                job = cycle["job"]
                status_path = Path(job["dir"]) / "status.json"
                status = read_json(status_path) if status_path.exists() else None
                if status and status.get("state") in TERMINAL:
                    self.complete_job(cycle, job, status)
                elif time.time() - job["submitted_at"] > 10 and not worker_busy(job["dir"]):
                    self.block("worker lost or launch uncertain; do not replay; inspect local/remote execution before recover")
        except (ValueError, OSError, KeyError, TypeError) as error:
            self.block(str(error))
        return self.state["status"] not in ("completed", "blocked")

    def recover(self):
        if self.state["status"] != "blocked":
            raise ValueError("recover requires blocked state")
        cycle = self.current()
        if cycle and cycle.get("job") and worker_busy(cycle["job"]["dir"]):
            raise ValueError("worker is still running; cancel it or wait before recovery")
        if cycle:
            self.finish(cycle, "abandoned", "operator reconciled execution; never promoted")
        self.state["status"] = "paused"
        self.state.pop("reason", None)
        self.save()
        self.event("recovered")


def status_summary(config):
    root = Path(config["state_dir"])
    if not (root / "state.json").exists():
        return {"status": "not_initialized"}
    state = read_json(root / "state.json")
    summary = {k: state.get(k) for k in ("status", "reason", "active_version", "started_at", "updated_at")}
    summary["cycles"] = [{k: c.get(k) for k in ("id", "phase", "status", "baseline", "candidate", "reason")} for c in state["cycles"]]
    if "search" in state:
        search = state["search"]
        summary["search"] = {k: search.get(k) for k in ("algorithm", "metric_calls", "full_evaluations", "finalizing", "finalist", "result")}
        summary["search"]["pool"] = [{"skill_digest": a, "parents": item["parents"],
                                      "validation_score": item["validation"]["result"]["score"]}
                                     for a, item in search["pool"].items()]
    if state["current"] is not None:
        job = state["cycles"][state["current"]].get("job")
        if job:
            summary["job"] = job
            p = Path(job["dir"]) / "status.json"
            summary["worker"] = read_json(p) if p.exists() else {"state": "launching"}
            summary["worker"]["lock_held"] = worker_busy(job["dir"])
    return summary
