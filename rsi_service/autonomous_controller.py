"""Session-driven, model-assisted evolution with a durable paired-trial loop.

The model proposes and explains. The controller owns immutable objectives,
budgets, evidence identities and promotion. A rejected trial still supplies the
next iteration's raw session and learning record; a successful trial is never
repeated merely to deploy the already-tested skill.
"""
from __future__ import annotations

import copy
import shutil
import time
from pathlib import Path

from .autonomous_models import validate_result
from .autonomous_policy import apply_measured_metrics, compare_assessments
from .config import validation_identity
from .controller import Controller, receipt, verify_receipt
from .sessions import ingest_session, verify_session
from .session_index import build_index, verify_index
from .storage import atomic_json, checked_diff, digest, file_hash, manifest, read_json, snapshot
from .trials import checked_transcript, execution_transcript, freeze_workspace, materialize_workspace, tree_manifest


class AutonomousController(Controller):
    def __init__(self, config):
        super().__init__(config)
        if "autonomous" not in self.state:
            options = config["autonomy"]
            bundle = self.ingest(options["source"], self.root / "sessions" / "initial")
            workspace = freeze_workspace(config["workspace"], self.root / "workspace_seed",
                                         options["workspace_excludes"])
            self.state["autonomous"] = {
                "objective_digest": digest(config["objective"]),
                "latest_session": bundle, "initial_session": bundle,
                "workspace": workspace, "model_calls": 0, "validation_runs": 0,
                "reservations": {}, "memory": [], "baseline_cache": {}}
            self.save()
            self.event("session_imported", source_sha256=bundle["source"]["sha256"],
                       chunks=len(bundle["chunks"]), events=bundle["stats"]["events"])

    @property
    def autonomous(self):
        return self.state["autonomous"]

    def ingest(self, source, target):
        return ingest_session(source, target, chunk_chars=self.config["autonomy"]["chunk_chars"],
                              max_bytes=self.config["autonomy"]["max_session_bytes"])

    def integrity(self, cycle):
        super().integrity(cycle)
        if cycle.get("candidate"):
            candidate = self.root / "cycles" / cycle["id"] / "candidate"
            if manifest(candidate) != manifest(self.version(cycle["candidate"])):
                raise ValueError("candidate working copy changed after being sealed")

    def stop_budget(self, reason):
        cycle = self.current()
        if cycle:
            self.finish(cycle, "unvalidated", reason)
        self.state.update(status="completed", reason=reason)
        self.save()
        self.event("autonomous_budget_reached", reason=reason)

    def start_cycle(self):
        options, limits = self.config["autonomy"], self.config["limits"]
        if (len(self.state["cycles"]) >= limits["max_cycles"] or self.remaining() <= 0 or
                self.state["no_change_cycles"] >= limits["max_no_change_cycles"] or
                self.autonomous["model_calls"] >= options["max_model_calls"]):
            self.stop_budget("cycle, wall-time, model-call, or no-progress budget reached")
            return
        bundle = self.autonomous["latest_session"]
        if not verify_session(bundle):
            raise ValueError("source session evidence changed")
        cycle_id = "a%04d" % (len(self.state["cycles"]) + 1)
        directory = self.root / "cycles" / cycle_id
        timeline = None
        if options["analysis_view"] == "timeline":
            timeline = build_index(bundle, directory / "timeline", options["timeline_chunk_chars"],
                                   options["timeline_preview_chars"])
        chunks = timeline["chunks"] if timeline else bundle["chunks"]
        if self.autonomous["model_calls"] + len(chunks) + 1 > options["max_model_calls"]:
            self.stop_budget("analysis requires %d chunk jobs plus planning; increase max_model_calls in a new experiment" % len(chunks))
            return
        candidate = directory / "candidate"
        if candidate.exists():
            shutil.rmtree(candidate)  # Only an unregistered copy from interrupted initialization.
        candidate.parent.mkdir(parents=True, exist_ok=True)
        baseline = self.state["active_version"]
        shutil.copytree(self.version(baseline), candidate)
        cycle = {"id": cycle_id, "baseline": baseline, "phase": "analyze", "analysis_index": 0,
                 "analyses": [], "analysis_chunks": chunks, "session_index": timeline,
                 "source_session": bundle, "trials": {}, "jobs": [], "job": None,
                 "model_records": [], "status": "running", "started_at": time.time(),
                 "validation_identity": validation_identity(self.config)}
        self.state["cycles"].append(cycle)
        self.state["current"] = len(self.state["cycles"]) - 1
        self.save()
        self.event("autonomous_cycle_started", cycle=cycle_id, baseline=baseline,
                   session_sha256=bundle["source"]["sha256"])

    def cache_key(self, cycle, anchor=None):
        return digest([anchor or cycle["baseline"], cycle["validation_identity"],
                       self.autonomous["workspace"]["digest"], self.config["objective"]])

    def reusable_baseline(self, cycle):
        record = self.autonomous["baseline_cache"].get(self.cache_key(cycle))
        ttl = self.config["autonomy"]["baseline_cache_ttl_seconds"]
        if (record and ttl > 0 and time.time() - record["finished_at"] <= ttl and
                self.valid_trial(record)):
            return copy.deepcopy(record)
        return None

    def context(self, cycle, job_id, stage, role=None):
        directory = self.root / "cycles" / cycle["id"]
        anchor = cycle.get("candidate", cycle["baseline"]) if role == "candidate" or stage == "quick" else cycle["baseline"]
        workspace = self.config["workspace"]
        if role:
            workspace = materialize_workspace(self.autonomous["workspace"], directory / "trials" / role / "workspace")
        bundle = cycle["source_session"]
        if stage == "judge":
            bundle = cycle["trials"][role]["session"]
        elif stage == "reflect" and cycle["trials"].get("candidate"):
            bundle = cycle["trials"]["candidate"]["session"]
        chunk = cycle["analysis_chunks"][cycle["analysis_index"]] if stage == "analyze" else None
        view = {k: bundle[k] for k in ("manifest_path", "events_path", "source", "stats")}
        view["chunks"] = [chunk] if chunk else bundle["chunks"]
        view["event_ids"] = chunk["event_ids"] if chunk else bundle["event_ids"]
        if chunk and file_hash(chunk["path"]) != chunk["sha256"]:
            raise ValueError("analysis chunk changed")
        context = {
            "autonomy": True, "cycle_id": cycle["id"], "workspace": workspace,
            "skill_dir": str(directory / "candidate") if stage == "improve" else str(self.version(anchor)),
            "skill_digest": anchor, "baseline_dir": str(self.version(cycle["baseline"])),
            "candidate_dir": str(directory / "candidate"), "run_dir": str(directory),
            "result_path": str(self.root / "jobs" / job_id / "output" / "result.json"),
            "validation_key": self.config["validation"]["key"],
            "validation_identity": cycle["validation_identity"], "objective": self.config["objective"],
            "session_bundle": view, "analysis_chunk": chunk, "analyses": cycle["analyses"],
            "analysis_view": self.config["autonomy"]["analysis_view"],
            "session_index": ({k: cycle["session_index"][k] for k in ("manifest_path", "view", "events", "raw_events")}
                              if cycle["session_index"] else None),
            "memory": self.autonomous["memory"][-8:], "hypothesis": cycle.get("plan"),
            "memory_archive": [str(self.root / "cycles" / item["cycle_id"] / "learning.json")
                               for item in self.autonomous["memory"]],
            "trial_role": role, "allowed_paths": self.config["allowed_paths"],
            "protected_paths": self.config["protected_paths"], "checks": self.config["autonomy"]["checks"],
            "log_limit_bytes": self.config["limits"]["log_limit_bytes"],
            "check_stage": "execution" if stage == "checks" else "skill",
        }
        if role and role in cycle["trials"]:
            trial = cycle["trials"][role]
            context.update(run_result=trial["run_result"], execution_evidence=trial["evidence"],
                           measured_stats=trial["stats"], independent_checks=trial.get("checks"))
        if stage == "reflect":
            bundles = {"source": cycle["source_session"],
                       **{name: trial["session"] for name, trial in cycle["trials"].items()}}
            context.update(baseline_assessment=cycle["trials"].get("baseline", {}).get("assessment"),
                           candidate_assessment=cycle["trials"].get("candidate", {}).get("assessment"),
                           decision=cycle.get("decision"), evidence_bundles=bundles,
                           evidence_event_ids=sorted({ref for item in bundles.values() for ref in item["event_ids"]}))
        if stage in ("run", "judge"):
            # The experimental treatment is the skill, not a leaked proposed fix.
            context.update(analyses=[], memory=[], memory_archive=[], hypothesis=None, session_index=None)
        if stage == "analyze":
            context["analyses"] = []
        if stage == "run":
            context.update(session_bundle={}, analysis_chunk=None,
                           run_dir=str(Path(workspace).parent),
                           baseline_dir=context["skill_dir"], candidate_dir=context["skill_dir"])
        return context

    def launch(self, cycle):
        self.integrity(cycle)
        phase = cycle["phase"]
        role, stage = phase.split("_", 1) if phase.startswith(("baseline_", "candidate_")) else (None, phase)
        suffix = "-%04d" % cycle["analysis_index"] if stage == "analyze" else ""
        job_id = cycle["id"] + "-" + phase + suffix
        counts = self.autonomous
        if job_id not in counts["reservations"]:
            model = stage not in ("quick", "checks")
            if model and counts["model_calls"] >= self.config["autonomy"]["max_model_calls"]:
                self.stop_budget("model-call budget reached; candidate not promoted")
                return
            if stage == "run" and counts["validation_runs"] >= self.config["autonomy"]["max_validation_runs"]:
                self.stop_budget("validation-run budget reached; candidate not promoted")
                return
            counts["reservations"][job_id] = {"model": model, "run": stage == "run"}
            counts["model_calls"] += int(model)
            counts["validation_runs"] += int(stage == "run")
            self.save()
        context = self.context(cycle, job_id, stage, role)
        self.submit(cycle, job_id, stage, context["skill_digest"], context)

    def record_result(self, cycle, job, stage):
        path = Path(job["dir"]) / "output" / "result.json"
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("invalid or oversized model result")
        context = read_json(Path(job["dir"]) / "context.json")
        result = validate_result(stage, read_json(path), context)
        context_path = Path(job["dir"]) / "context.json"
        cycle["model_records"].append({"artifacts": {str(path): file_hash(path), str(context_path): file_hash(context_path)}})
        return result

    def collect_trial(self, cycle, job, role, result):
        context = read_json(Path(job["dir"]) / "context.json")
        evidence = execution_transcript(job, result, context["workspace"])
        bundle = self.ingest({"path": evidence["path"], "format": "exec"},
                             self.root / "sessions" / job["id"])
        completion = job["completion"]
        stats = dict(bundle["stats"], wall_seconds=max(0, completion["finished_at"] - completion["started_at"]))
        cycle["trials"][role] = {
            "skill_digest": job["skill_digest"], "run_result": result, "session": bundle,
            "stats": stats, "evidence": evidence, "worker": completion,
            "finished_at": completion["finished_at"], "workspace": context["workspace"]}
        self.event("trial_collected", cycle=cycle["id"], role=role, session_sha256=bundle["source"]["sha256"])

    def valid_trial(self, trial):
        try:
            return (verify_session(trial["session"]) and verify_receipt(trial["checks"]) and
                    verify_receipt(trial["judge_record"]) and verify_receipt(trial["evidence"]))
        except (KeyError, OSError, ValueError):
            return False

    def decide(self, cycle):
        if tree_manifest(self.autonomous["workspace"]["path"]) != self.autonomous["workspace"]["files"]:
            raise ValueError("frozen workspace changed during validation")
        for record in cycle["model_records"]:
            if not verify_receipt(record):
                raise ValueError("model result evidence changed")
        for job in cycle["jobs"]:
            if not verify_receipt({"artifacts": job["input_artifacts"]}):
                raise ValueError("frozen job inputs changed")
        if not verify_session(cycle["source_session"]) or not verify_receipt(cycle["quick"]):
            raise ValueError("analysis or quick-check evidence changed")
        if cycle.get("session_index") and not verify_index(cycle["session_index"]):
            raise ValueError("analysis timeline evidence changed")
        for trial in cycle["trials"].values():
            if not self.valid_trial(trial):
                raise ValueError("trial evidence changed")
        baseline, candidate = cycle["trials"]["baseline"], cycle["trials"]["candidate"]
        decision = compare_assessments(baseline["assessment"], candidate["assessment"], self.config["objective"])
        if (candidate["run_result"]["outcome"] != "signed" or
                not candidate["checks"]["result"]["passed"] or
                candidate["worker"].get("log_truncated") or candidate["evidence"]["missing_evidence"]):
            decision.update(promote=False, reason="candidate execution incomplete, failed independent checks, or evidence incomplete")
        if baseline["worker"].get("log_truncated") or baseline["evidence"]["missing_evidence"]:
            decision.update(promote=False, reason="baseline evidence incomplete; comparison is inconclusive")
        return decision

    def complete_job(self, cycle, job, status):
        job["completion"] = status
        self.integrity(cycle)
        if not verify_receipt({"artifacts": job["input_artifacts"]}):
            raise ValueError("frozen job inputs changed")
        phase = cycle["phase"]
        role, stage = phase.split("_", 1) if phase.startswith(("baseline_", "candidate_")) else (None, phase)
        successful = status["state"] == "completed" and status.get("returncode") == 0
        if not successful:
            if stage == "run" and status["state"] == "failed":
                result = {"outcome": "aborted", "summary": "CLI failed: " + status.get("reason", ""), "evidence": []}
                self.collect_trial(cycle, job, role, result)
                cycle["phase"] = role + "_checks"
            elif stage == "quick" and status["state"] == "failed":
                self.finish(cycle, "rejected", "quick check command failed")
                return
            else:
                self.block(stage + ": " + status["state"] + "; inspect execution before recovery")
                return
        elif stage in ("quick", "checks"):
            record = receipt(job, require_pass=False)
            if stage == "quick":
                cycle["quick"] = record
                if not record["result"]["passed"]:
                    self.finish(cycle, "rejected", "quick check rejected candidate")
                    return
                cached = self.reusable_baseline(cycle)
                needed = 1 if cached else 2
                if self.autonomous["validation_runs"] + needed > self.config["autonomy"]["max_validation_runs"]:
                    self.stop_budget("insufficient validation budget for a complete comparison")
                    return
                model_jobs = 3 if cached else 5  # paired execution/judges plus reflection
                if self.autonomous["model_calls"] + model_jobs > self.config["autonomy"]["max_model_calls"]:
                    self.stop_budget("insufficient model-call budget for a complete comparison")
                    return
                if cached:
                    cycle["trials"]["baseline"] = cached
                    cycle["phase"] = "candidate_run"
                    self.event("autonomous_baseline_reused", cycle=cycle["id"])
                else:
                    cycle["phase"] = "baseline_run"
            else:
                trial = cycle["trials"][role]
                trial["checks"] = record
                transcript = checked_transcript(trial["evidence"], record, job["dir"])
                trial["session"] = self.ingest({"path": transcript, "format": "exec"},
                                                self.root / "sessions" / job["id"])
                cycle["phase"] = role + "_judge"
        else:
            result = self.record_result(cycle, job, stage)
            if stage == "analyze":
                cycle["analyses"].append(result)
                cycle["analysis_index"] += 1
                if cycle["analysis_index"] == len(cycle["analysis_chunks"]):
                    cycle["phase"] = "plan"
            elif stage == "plan":
                cycle["plan"] = result
                if result["decision"] == "needs_input":
                    self.block("analysis needs input: " + result["summary"])
                    return
                if result["decision"] == "no_change":
                    self.finish(cycle, "no_change", result["summary"])
                    return
                cycle["phase"] = "improve"
            elif stage == "improve":
                path = self.root / "cycles" / cycle["id"] / "candidate"
                try:
                    changes = checked_diff(self.version(cycle["baseline"]), path,
                                           self.config["allowed_paths"], self.config["protected_paths"])
                except ValueError as error:
                    self.finish(cycle, "rejected", str(error))
                    return
                if not changes:
                    self.finish(cycle, "no_change", "model made no skill changes")
                    return
                if set(changes) - set(cycle["plan"]["target_paths"]):
                    self.finish(cycle, "rejected", "candidate changed files outside the evidence-grounded plan")
                    return
                anchor = snapshot(path, self.root / "versions")
                sealed_changes = checked_diff(self.version(cycle["baseline"]), self.version(anchor),
                                              self.config["allowed_paths"], self.config["protected_paths"])
                if sealed_changes != changes:
                    raise ValueError("candidate changed while sealing")
                cycle.update(candidate=anchor, changes=changes, phase="quick")
            elif stage == "run":
                self.collect_trial(cycle, job, role, result)
                if result["outcome"] == "pending":
                    self.block("validation needs input: " + result["summary"])
                    return
                cycle["phase"] = role + "_checks"
            elif stage == "judge":
                trial = cycle["trials"][role]
                trial["assessment"] = apply_measured_metrics(result, self.config["objective"], trial["stats"])
                trial["judge_record"] = cycle["model_records"][-1]
                if role == "baseline":
                    self.autonomous["baseline_cache"][self.cache_key(cycle)] = copy.deepcopy(trial)
                    cycle["phase"] = "candidate_run"
                else:
                    cycle["decision"] = self.decide(cycle)
                    cycle["phase"] = "reflect"
            elif stage == "reflect":
                cycle["reflection"] = result
                decision = self.decide(cycle)  # Recheck immutable evidence before promotion.
                cycle["decision"] = decision
                candidate = cycle["trials"]["candidate"]
                self.autonomous["latest_session"] = candidate["session"]
                if decision["promote"]:
                    self.state["active_version"] = cycle["candidate"]
                    self.autonomous["baseline_cache"][self.cache_key(cycle, cycle["candidate"])] = copy.deepcopy(candidate)
                self.finish(cycle, "promoted" if decision["promote"] else "rejected", decision["reason"])
                return
        cycle["job"] = None
        self.save()
        self.event("phase_completed", cycle=cycle["id"], phase=phase, next_phase=cycle["phase"])

    def finish(self, cycle, status, reason):
        memory = {
            "cycle_id": cycle["id"], "status": status, "reason": str(reason),
            "baseline": cycle["baseline"], "candidate": cycle.get("candidate"),
            "plan": cycle.get("plan"), "decision": cycle.get("decision"),
            "reflection": cycle.get("reflection"),
            "assessments": {role: trial.get("assessment") for role, trial in cycle.get("trials", {}).items()},
            "source_manifest": cycle["source_session"]["manifest_path"],
            "result_manifest": cycle.get("trials", {}).get("candidate", {}).get("session", {}).get("manifest_path"),
        }
        if not any(item["cycle_id"] == cycle["id"] for item in self.autonomous["memory"]):
            self.autonomous["memory"].append(memory)
        atomic_json(self.root / "cycles" / cycle["id"] / "learning.json", memory)
        super().finish(cycle, status, reason)
