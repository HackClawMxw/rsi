"""Lossless, immutable ingestion of Codex rollout and ``exec --json`` streams.

Only one raw record is held in memory at a time. Normalized records retain the
complete decoded JSON value and byte offsets into an exact raw copy. Chunk
``events`` contain ``event_id``, zero-based ``part_index``, ``part_count`` and
``text``; concatenate an event's parts to recover its normalized JSON. Splitting
is by character count, not truncation. Chunk envelopes add a little overhead.

No session content is printed, interpreted as instructions, or sent to a model.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path


DEFAULT_MAX_BYTES = 536870912
_ROLLOUT_TYPES = {"session_meta", "response_item", "event_msg", "turn_context"}
_EXEC_TYPES = {"thread.started", "turn.started", "turn.completed", "turn.failed",
               "item.started", "item.updated", "item.completed"}
_TOOL_TYPES = {"function_call", "custom_tool_call", "local_shell_call",
               "command_execution", "mcp_tool_call", "web_search", "file_change"}
_RESULT_TYPES = {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _loads(text):
    def constant(value):
        raise ValueError("nonfinite JSON number")

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON number")
        return result

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(text, parse_constant=constant, parse_float=finite,
                           object_pairs_hook=unique)
        _json(value).encode("utf-8")  # reject escaped lone surrogates as well
        return value
    except (UnicodeError, RecursionError) as error:
        raise ValueError("invalid JSON encoding or nesting") from error


def _absolute(path):
    if not isinstance(path, (str, Path)) or not str(path) or "\x00" in str(path):
        raise ValueError("path must be nonempty")
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _no_links(path):
    for current in (path, *path.parents):
        if current.is_symlink():
            raise ValueError("symlink paths are not allowed")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_regular(path, maximum=DEFAULT_MAX_BYTES):
    _no_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("source/artifact must be a regular file")
    if maximum is not None and before.st_size > maximum:
        raise ValueError("session exceeds max_bytes")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(before):
            raise ValueError("source changed before reading")
        return os.fdopen(descriptor, "rb"), current
    except BaseException:
        os.close(descriptor)
        raise


def _unchanged(path, stream, before):
    _no_links(path)
    if _identity(os.fstat(stream.fileno())) != _identity(before) or _identity(path.stat()) != _identity(before):
        raise ValueError("source changed while reading")


def _hash_file(path, maximum=DEFAULT_MAX_BYTES):
    stream, before = _open_regular(path, maximum)
    result, size = hashlib.sha256(), 0
    with stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > maximum:
                raise ValueError("session exceeds max_bytes")
            result.update(block)
        _unchanged(path, stream, before)
    return result.hexdigest(), size


def _session_id(record):
    if not isinstance(record, dict):
        return None
    if record.get("type") == "session_meta":
        payload = record.get("payload")
        value = payload.get("id") if isinstance(payload, dict) else record.get("id")
    elif record.get("type") == "thread.started":
        value = record.get("thread_id")
    else:
        return None
    return value if isinstance(value, str) and value else None


def _format(source):
    value = source.get("format", "auto")
    if value not in ("auto", "rollout", "exec"):
        raise ValueError("format must be auto, rollout, or exec")
    return value


class _SessionIdentity:
    """The first metadata record owns a rollout; declared ancestors are history.

    Codex forks prepend their own session_meta (with forked_from_id) to copied
    ancestral history, which can contain further session_meta records. Treating
    every metadata ID as the owner incorrectly resolves the fork as its parent.
    Only an explicit, acyclic fork chain permits a different metadata ID.
    """
    def __init__(self):
        self.owner = None
        self.parents = {}
        self.expected_ancestors = set()

    def observe(self, record):
        identity = _session_id(record)
        if identity is None:
            return False
        payload = record.get("payload", record)
        parent = payload.get("forked_from_id") if isinstance(payload, dict) else None
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise ValueError("invalid fork lineage metadata")
        if identity in self.parents:
            if self.parents[identity] != parent:
                raise ValueError("session contains conflicting fork lineage metadata")
            return True
        if self.owner is None:
            self.owner = identity
        elif identity not in self.expected_ancestors or record.get("type") != "session_meta":
            raise ValueError("session contains conflicting session IDs without declared fork lineage")
        if parent == identity or parent in self.parents:
            raise ValueError("session contains cyclic fork lineage")
        self.parents[identity] = parent
        if parent:
            self.expected_ancestors.add(parent)
        return True


def _discover_ids(path, wanted=None):
    """Read ownership metadata, not entire historical/tool-output bodies.

    A matching file's consecutive metadata header is checked here; ingestion
    validates all later metadata and all JSON. Unrelated files return as soon
    as their own identity is known, regardless of copied ancestor identities.
    """
    stream, before = _open_regular(path)
    identities, size = _SessionIdentity(), 0
    with stream:
        while True:
            line = stream.readline(DEFAULT_MAX_BYTES - size + 1)
            if not line:
                break
            size += len(line)
            if size > DEFAULT_MAX_BYTES:
                raise ValueError("session exceeds max_bytes")
            try:
                record = _loads(line.decode("utf-8"))
            except (ValueError, UnicodeError):
                if identities.owner is not None:
                    break
                continue
            metadata = identities.observe(record)
            if identities.owner is not None and (not metadata or (wanted is not None and identities.owner != wanted)):
                break
        _unchanged(path, stream, before)
    return {identities.owner} if identities.owner is not None else set()


def resolve_source(source: dict) -> Path:
    """Resolve one file or one exact session ID; never match filename substrings."""
    if not isinstance(source, dict):
        raise ValueError("session source must be an object")
    _format(source)
    if ("path" in source) == ("session_id" in source):
        raise ValueError("provide exactly one of path or session_id")
    if "path" in source:
        path = _absolute(source["path"])
        stream, _ = _open_regular(path, maximum=None)
        stream.close()
        return path
    wanted = source["session_id"]
    roots = source.get("roots")
    if not isinstance(wanted, str) or not wanted or not isinstance(roots, list) or not roots:
        raise ValueError("session_id requires a nonempty ID and roots list")
    matches, seen, candidates = [], set(), []
    for value in roots:
        root = _absolute(value)
        _no_links(root)
        if not root.is_dir():
            raise ValueError("session roots must be existing directories")
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(name for name in names if not (Path(directory) / name).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix not in (".jsonl", ".ndjson") or path in seen:
                    continue
                seen.add(path)
                if path.is_symlink():
                    continue
                candidates.append(path)
    # Exact terminal ID tokens prioritize native filenames, but metadata must
    # verify them. All other filenames still receive the same ownership check,
    # so a custom-named duplicate cannot silently resolve an ambiguous source.
    suffix = re.compile(r"(?:^|-)" + re.escape(wanted) + r"\.(?:jsonl|ndjson)$")
    for path in sorted(candidates, key=lambda p: (not bool(suffix.search(p.name)), str(p))):
        if wanted in _discover_ids(path, wanted):
            matches.append(path)
    if not matches:
        raise ValueError("no session with the exact requested ID was found")
    if len(matches) != 1:
        raise ValueError("multiple sessions have the exact requested ID")
    return matches[0]


def _record_format(record):
    kind = record.get("type") if isinstance(record, dict) else None
    if not isinstance(kind, str):
        return None
    if kind in _ROLLOUT_TYPES:
        return "rollout"
    if kind in _EXEC_TYPES:
        return "exec"
    return None


def _tool_details(record, event_id):
    if not isinstance(record, dict):
        return "unknown", None, False
    outer = record.get("type", "unknown")
    if not isinstance(outer, str):
        outer = "unknown"
    payload = record.get("payload") if outer == "response_item" else record.get("item")
    item = payload if isinstance(payload, dict) else record
    kind = item.get("type", outer)
    if not isinstance(kind, str):
        kind = "unknown"
    if kind not in _TOOL_TYPES | _RESULT_TYPES:
        return str(kind), None, False
    identity = str(item.get("call_id") or item.get("id") or event_id)
    failed = item.get("status") in ("failed", "error") or item.get("isError") is True
    for data in (item, item.get("result"), item.get("output")):
        if isinstance(data, str):
            try:
                data = _loads(data)
            except ValueError:
                continue
        if isinstance(data, dict):
            code = data.get("exit_code", data.get("returncode"))
            failed = failed or (isinstance(code, int) and not isinstance(code, bool) and code != 0)
            failed = failed or data.get("isError") is True or data.get("status") in ("failed", "error")
            metadata = data.get("metadata")
            if isinstance(metadata, dict):
                code = metadata.get("exit_code")
                failed = failed or (isinstance(code, int) and not isinstance(code, bool) and code != 0)
    return "tool_result" if kind in _RESULT_TYPES else "tool_call", identity, failed


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _existing(destination, source_path, maximum):
    _no_links(destination)
    if not destination.is_dir():
        raise ValueError("destination must be a bundle directory")
    manifest_path = destination / "manifest.json"
    manifest_hash, _ = _hash_file(manifest_path)
    bundle = _loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or not isinstance(bundle.get("artifacts"), dict):
        raise ValueError("invalid existing session manifest")
    if hashlib.sha256((_json(bundle) + "\n").encode("utf-8")).hexdigest() != manifest_hash:
        raise ValueError("existing manifest is not the canonical immutable artifact")
    bundle["artifacts"][str(manifest_path)] = manifest_hash
    if not verify_session(bundle):
        raise ValueError("existing session bundle failed verification")
    source_hash, size = _hash_file(source_path, maximum)
    if bundle["source"] != dict(bundle["source"], path=str(source_path), sha256=source_hash, size=size):
        raise ValueError("source differs from the existing immutable session bundle")
    return bundle


def ingest_session(source: dict, destination: Path, chunk_chars=24000, max_bytes=DEFAULT_MAX_BYTES) -> dict:
    """Create an atomic immutable bundle, or verify and return the same bundle.

    ``auto`` infers protocol from recognizable records. Non-JSON stdout is
    retained only for exec streams. JSON-looking malformed/truncated records
    always fail closed, even for exec. An unknown all-JSON protocol is retained.
    """
    for value, label in ((chunk_chars, "chunk_chars"), (max_bytes, "max_bytes")):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(label + " must be a positive integer")
    source_path, requested = resolve_source(source), _format(source)
    destination = _absolute(destination)
    _no_links(destination)
    if destination == source_path or destination in source_path.parents:
        raise ValueError("destination must not contain the source session")
    if destination.exists():
        existing = _existing(destination, source_path, max_bytes)
        if requested != "auto" and existing["stats"]["format"] not in ("auto", requested):
            raise ValueError("requested format conflicts with the existing session bundle")
        return existing
    # Evidence references must be unambiguous when source, baseline and
    # candidate bundles are supplied together to a later reflection job.
    source_digest, source_size = _hash_file(source_path, max_bytes)
    event_prefix = "s" + source_digest[:16] + ":e"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="." + destination.name + "-", dir=destination.parent))
    try:
        (temporary / "chunks").mkdir()
        artifacts, chunks, event_ids = {}, [], []
        session_identity = _SessionIdentity()
        calls, failures = set(), set()
        raw_hash, offset, detected, unstructured = hashlib.sha256(), 0, None, 0
        pending, pending_chars = [], 0

        def close_chunk():
            nonlocal pending, pending_chars
            if not pending:
                return
            identity = "c%08d" % (len(chunks) + 1)
            relative = Path("chunks") / (identity + ".json")
            _write_json(temporary / relative, {"schema_version": 1, "id": identity, "events": pending})
            digest, _ = _hash_file(temporary / relative, max_bytes * 16 + 1048576)
            final_path = str(destination / relative)
            chunks.append({"id": identity, "path": final_path, "sha256": digest,
                           "event_ids": list(dict.fromkeys(row["event_id"] for row in pending))})
            artifacts[final_path] = digest
            pending, pending_chars = [], 0

        stream, before = _open_regular(source_path, max_bytes)
        with stream, (temporary / "raw.jsonl").open("xb") as raw, (temporary / "events.jsonl").open("x", encoding="utf-8") as events:
            while True:
                line = stream.readline(max_bytes - offset + 1)
                if not line:
                    break
                if offset + len(line) > max_bytes:
                    raise ValueError("session exceeds max_bytes")
                try:
                    text = line.decode("utf-8")
                except UnicodeError as error:
                    raise ValueError("session contains invalid UTF-8") from error
                event_id = event_prefix + "%08d" % (len(event_ids) + 1)
                try:
                    record = _loads(text)
                    record_format = _record_format(record)
                    if record_format:
                        if (detected and detected != record_format) or (requested != "auto" and requested != record_format):
                            raise ValueError("session contains conflicting stream formats")
                        detected = record_format
                    kind, call, failed = _tool_details(record, event_id)
                except json.JSONDecodeError as error:
                    if text.lstrip().startswith(("{", "[", '"')) or requested == "rollout" or detected == "rollout":
                        raise ValueError("malformed JSON at session line %d" % (len(event_ids) + 1)) from error
                    record, kind, call, failed = {"text": text}, "unstructured", None, False
                    unstructured += 1
                if (requested == "rollout" or detected == "rollout") and unstructured:
                    raise ValueError("rollout contains non-JSON records")
                session_identity.observe(record)
                if kind == "tool_call":
                    calls.add(call)
                if call and failed:
                    failures.add(call)
                normalized = {"event_id": event_id, "source_line": len(event_ids) + 1,
                              "raw_start": offset, "raw_end": offset + len(line),
                              "kind": kind, "record": record}
                encoded = _json(normalized)
                events.write(encoded + "\n")
                parts = max(1, (len(encoded) + chunk_chars - 1) // chunk_chars)
                for part in range(parts):
                    fragment = encoded[part * chunk_chars:(part + 1) * chunk_chars]
                    if pending and pending_chars + len(fragment) > chunk_chars:
                        close_chunk()
                    pending.append({"event_id": event_id, "part_index": part, "part_count": parts, "text": fragment})
                    pending_chars += len(fragment)
                raw.write(line)
                raw_hash.update(line)
                offset += len(line)
                event_ids.append(event_id)
            _unchanged(source_path, stream, before)
            for output in (raw, events):
                output.flush()
                os.fsync(output.fileno())
        if not event_ids:
            raise ValueError("session is empty")
        if raw_hash.hexdigest() != source_digest or offset != source_size:
            raise ValueError("source changed between identity hashing and ingestion")
        if unstructured and (requested != "exec" and detected != "exec"):
            raise ValueError("non-JSON records require an exec stream")
        session_id = session_identity.owner
        if "session_id" in source and session_id != source["session_id"]:
            raise ValueError("resolved session ID changed before ingestion")
        close_chunk()
        events_path, manifest_path = destination / "events.jsonl", destination / "manifest.json"
        artifacts[str(destination / "raw.jsonl")] = raw_hash.hexdigest()
        artifacts[str(events_path)] = _hash_file(temporary / "events.jsonl", max_bytes * 16 + 1048576)[0]
        bundle = {"schema_version": 1,
                  "source": {"path": str(source_path), "sha256": raw_hash.hexdigest(), "size": offset, "session_id": session_id},
                  "events_path": str(events_path), "manifest_path": str(manifest_path),
                  "chunks": chunks, "event_ids": event_ids,
                  "stats": {"events": len(event_ids), "tool_calls": len(calls), "failed_tools": len(failures),
                            "unstructured": unstructured, "format": detected or requested,
                            "warnings": ["exec stream contains non-JSON stdout records"] if unstructured else []},
                  "artifacts": artifacts}
        _write_json(temporary / "manifest.json", bundle)
        manifest_hash = _hash_file(temporary / "manifest.json", max_bytes * 16 + 1048576)[0]
        _sync_directory(temporary / "chunks")
        _sync_directory(temporary)
        # Publishing an entire prepared directory prevents readers seeing half a bundle.
        try:
            os.rename(temporary, destination)
        except OSError:
            if destination.exists():
                return _existing(destination, source_path, max_bytes)
            raise
        bundle["artifacts"][str(manifest_path)] = manifest_hash
        _sync_directory(destination.parent)
        return bundle
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify_session(bundle: dict) -> bool:
    """Verify immutable copied artifacts; the original source may be offline."""
    try:
        if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
            return False
        manifest_path = _absolute(bundle["manifest_path"])
        root = manifest_path.parent
        if manifest_path.name != "manifest.json" or str(manifest_path) != bundle["manifest_path"]:
            return False
        ids, chunks, artifacts = bundle["event_ids"], bundle["chunks"], bundle["artifacts"]
        source = bundle["source"]
        if (not isinstance(source, dict) or not isinstance(source["size"], int)
                or isinstance(source["size"], bool) or source["size"] < 1
                or str(_absolute(source["path"])) != source["path"]
                or (source["session_id"] is not None and (not isinstance(source["session_id"], str) or not source["session_id"]))):
            return False
        event_prefix = "s" + source["sha256"][:16] + ":e"
        if not isinstance(ids, list) or not ids or ids != [event_prefix + "%08d" % (i + 1) for i in range(len(ids))]:
            return False
        if not isinstance(chunks, list) or not chunks or not isinstance(artifacts, dict):
            return False
        if bundle["events_path"] != str(root / "events.jsonl") or bundle["stats"]["events"] != len(ids):
            return False
        expected = {str(root / "raw.jsonl"), str(root / "events.jsonl"), str(manifest_path)}
        covered, known = set(), set(ids)
        for index, chunk in enumerate(chunks):
            identity = "c%08d" % (index + 1)
            path = str(root / "chunks" / (identity + ".json"))
            if chunk["id"] != identity or chunk["path"] != path or artifacts.get(path) != chunk["sha256"]:
                return False
            if not chunk["event_ids"] or not set(chunk["event_ids"]).issubset(known):
                return False
            covered.update(chunk["event_ids"])
            expected.add(path)
        if covered != known or set(artifacts) != expected:
            return False
        for path, digest in artifacts.items():
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                return False
            if _hash_file(Path(path), max(DEFAULT_MAX_BYTES * 16, bundle["source"]["size"] * 16 + 1048576))[0] != digest:
                return False
        if artifacts[str(root / "raw.jsonl")] != bundle["source"]["sha256"] or (root / "raw.jsonl").stat().st_size != bundle["source"]["size"]:
            return False
        expected_manifest = dict(bundle, artifacts={p: h for p, h in artifacts.items() if p != str(manifest_path)})
        return _loads(manifest_path.read_text(encoding="utf-8")) == expected_manifest
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return False
