"""Frozen local workspaces and immutable execution evidence for paired trials.

Remote environments are not cloned by this module. Their identity and reset
protocol belong in the task and operator checks; baseline reuse is off by default.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
from pathlib import Path

from .storage import atomic_json, digest, file_hash, read_json


def tree_manifest(root, excludes=()):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("workspace must be a real directory")
    files = {}
    for parent, dirs, names in os.walk(root):
        relative = Path(parent).relative_to(root)
        def excluded(name):
            path = (relative / name).as_posix()
            return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(name, p) for p in excludes)
        dirs[:] = sorted(d for d in dirs if not excluded(d))
        for name in dirs + sorted(n for n in names if not excluded(n)):
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError("workspace symlink requires an explicit exclusion: " + str(path))
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("workspace contains a special file: " + str(path))
            files[path.relative_to(root).as_posix()] = {
                "sha256": file_hash(path), "executable": bool(path.stat().st_mode & 0o111)}
    return files


def freeze_workspace(source, destination, excludes=()):
    source, destination = Path(source), Path(destination)
    receipt_path = destination.parent / (destination.name + "-manifest.json")
    if destination.exists():
        record = read_json(receipt_path)
        if tree_manifest(destination) != record["files"]:
            raise ValueError("frozen workspace was modified")
        return record
    files = tree_manifest(source, excludes)
    temporary = destination.with_name(destination.name + ".building")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    for name in files:
        target = temporary / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    if tree_manifest(temporary) != files or tree_manifest(source, excludes) != files:
        raise ValueError("workspace changed while freezing inputs")
    record = {"path": str(destination), "digest": digest(files), "files": files}
    atomic_json(receipt_path, record)
    temporary.rename(destination)
    return record


def materialize_workspace(record, destination):
    if tree_manifest(record["path"]) != record["files"]:
        raise ValueError("frozen workspace was modified")
    destination = Path(destination)
    if not destination.exists():
        temporary = destination.with_name(destination.name + ".building")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(record["path"], temporary)
        if tree_manifest(temporary) != record["files"]:
            raise ValueError("trial workspace copy mismatch")
        temporary.rename(destination)
    # Existing trial directories may contain completed work. Never reset them.
    return str(destination)


def execution_transcript(job, result, workspace):
    """Append the actual result and copied artifact identities to raw CLI events.

The output is JSONL even when stderr or a provider emits plain text. Nothing
from the process log is interpreted as a command or silently truncated here.
"""
    root = Path(job["dir"])
    target = root / "execution.jsonl"
    metadata_path = root / "execution-manifest.json"
    input_digest = digest({"result": result, "completion": job["completion"], "workspace": str(workspace)})
    if target.exists():
        record = read_json(metadata_path)
        if record.get("input_digest") != input_digest:
            raise ValueError("execution transcript inputs changed after collection")
        if any(file_hash(p) != h for p, h in record["artifacts"].items()):
            raise ValueError("execution evidence changed")
        return record
    evidence = []
    missing = []
    artifacts = {}
    for index, name in enumerate(result.get("evidence", [])):
        path = Path(name)
        if not path.is_absolute():
            path = Path(workspace) / path
        resolved = path.resolve()
        roots = [Path(workspace).resolve(), (root / "output").resolve()]
        if (path.is_symlink() or not path.is_file() or
                not any(r == resolved or r in resolved.parents for r in roots)):
            missing.append(str(path))
            continue
        destination = root / "evidence" / ("%04d-" % index + path.name)
        destination.parent.mkdir(exist_ok=True)
        before = file_hash(path)
        shutil.copy2(path, destination)
        if before != file_hash(destination) or before != file_hash(path):
            raise ValueError("execution artifact changed while collecting")
        artifacts[str(destination)] = before
        evidence.append({"original_path": str(path), "path": str(destination), "sha256": before})
    temporary = target.with_suffix(".building")
    with temporary.open("w", encoding="utf-8") as output:
        def emit(value):
            output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        for stream_name in ("stdout.log", "stderr.log"):
            stream = root / stream_name
            if not stream.exists():
                continue
            artifacts[str(stream)] = file_hash(stream)
            with stream.open(encoding="utf-8", errors="replace") as incoming:
                for line in incoming:
                    try:
                        value = json.loads(line)
                        # Reject nonfinite JSON so ingestion cannot inherit NaN.
                        json.dumps(value, allow_nan=False)
                    except (ValueError, TypeError):
                        value = {"type": "rsi.unstructured", "stream": stream_name, "text": line}
                    emit(value)
        emit({"type": "rsi.execution_result", "result": result,
              "worker": job["completion"], "evidence": evidence, "missing_evidence": missing})
        output.flush()
        os.fsync(output.fileno())
    artifacts[str(target)] = file_hash(temporary)
    record = {"path": str(target), "artifacts": artifacts, "input_digest": input_digest,
              "evidence": evidence, "missing_evidence": missing}
    atomic_json(metadata_path, record)
    temporary.rename(target)
    return record


def checked_transcript(evidence, checks, job_dir):
    """Add independently produced check receipts as separately citable events."""
    target = Path(job_dir) / "checked-execution.jsonl"
    expected = digest({"execution": evidence, "checks": checks})
    receipt_path = Path(job_dir) / "checked-execution-manifest.json"
    if target.exists():
        record = read_json(receipt_path)
        if record["input_digest"] != expected or file_hash(target) != record["sha256"]:
            raise ValueError("checked transcript changed")
        return str(target)
    temporary = target.with_suffix(".building")
    with temporary.open("wb") as output, Path(evidence["path"]).open("rb") as incoming:
        shutil.copyfileobj(incoming, output)
        event = {"type": "rsi.independent_checks", "receipt": checks}
        output.write((json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode())
        output.flush()
        os.fsync(output.fileno())
    atomic_json(receipt_path, {"input_digest": expected, "sha256": file_hash(temporary)})
    temporary.rename(target)
    return str(target)
