"""Complete event coverage with bounded timeline previews and exact raw lookup.

Every normalized event receives one timeline row. A preview is a routing aid,
not evidence that a model has read the full payload. The immutable normalized
JSONL remains authoritative and is addressed by byte offset and byte length.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .sessions import _absolute, _loads, _no_links, _open_regular, _unchanged, _sync_directory


NOTE = "previews are routing; read original details before claims"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _read(path):
    path = _absolute(path)
    stream, before = _open_regular(path, maximum=None)
    with stream:
        data = stream.read()
        _unchanged(path, stream, before)
    return data


def _hash(path):
    path = _absolute(path)
    stream, before = _open_regular(path, maximum=None)
    value = hashlib.sha256()
    with stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            value.update(block)
        _unchanged(path, stream, before)
    return value.hexdigest(), before.st_size


def _write(path, data):
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(data).hexdigest()


def _positive(value):
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _text_preview(record):
    if not isinstance(record, dict):
        return _json(record)
    inner = record.get("payload", record.get("item", record))
    if not isinstance(inner, dict):
        inner = record
    for name in ("message", "text", "command", "arguments", "output", "aggregated_output", "content", "result", "instructions"):
        value = inner.get(name)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            text = [item["text"] for item in value if isinstance(item, dict) and isinstance(item.get("text"), str)]
            if text:
                return "\n".join(text)
        if value is not None:
            return _json(value)
    return _json(inner)


def _metadata(record):
    if not isinstance(record, dict):
        return {}
    inner = record.get("payload", record.get("item", record))
    if not isinstance(inner, dict):
        inner = record
    fields = {}
    for name in ("type", "role", "status"):
        value = inner.get(name, record.get(name))
        if isinstance(value, str):
            fields[name] = value[:128]
    tool = inner.get("name", inner.get("tool_name", inner.get("tool")))
    if isinstance(tool, str):
        fields["tool"] = tool[:128]
    # Tool-result output can itself be serialized JSON. Inspect structured
    # status only; do not infer failure from words appearing in prose.
    for value in (inner, inner.get("result"), inner.get("output")):
        if isinstance(value, str):
            try:
                value = _loads(value)
            except (ValueError, UnicodeError):
                continue
        if not isinstance(value, dict):
            continue
        for candidate in (value, value.get("metadata")):
            if not isinstance(candidate, dict):
                continue
            code = candidate.get("exit_code", candidate.get("returncode"))
            if isinstance(code, int) and not isinstance(code, bool):
                fields["exit"] = code
            if isinstance(candidate.get("status"), str):
                fields["status"] = candidate["status"][:128]
    return fields


def _row(event, encoded, offset, preview_chars, seen_content):
    if (not isinstance(event, dict) or not isinstance(event.get("event_id"), str)
            or not event["event_id"] or not isinstance(event.get("kind"), str)
            or not _positive(event.get("source_line")) or "record" not in event):
        raise ValueError("invalid normalized event")
    record = event["record"]
    text = _text_preview(record)
    truncated = len(text) > preview_chars
    content_hash = hashlib.sha256(_json(record).encode("utf-8")).hexdigest()
    row = {"event_id": event["event_id"], "kind": event["kind"], "source_line": event["source_line"],
           "events_offset": offset, "events_bytes": len(encoded), "full_chars": len(encoded.decode("utf-8").rstrip("\r\n")),
           "preview": text[:preview_chars] + (" …[truncated]" if truncated else ""),
           "preview_truncated": truncated, "content_sha256": content_hash, **_metadata(record)}
    duplicate = seen_content.get(content_hash)
    if duplicate:
        row["duplicate_of"] = duplicate
        if isinstance(record, dict) and record.get("type") in ("turn_context", "session_meta"):
            row["preview"] = "[duplicate metadata; read " + duplicate + "]"
            row["preview_truncated"] = True
    else:
        seen_content[content_hash] = event["event_id"]
    return row


def _attach_manifest(record, manifest_data, checksum_data):
    value = dict(record, artifacts=dict(record["artifacts"]))
    path = Path(record["manifest_path"])
    value["artifacts"][str(path)] = hashlib.sha256(manifest_data).hexdigest()
    value["artifacts"][str(path.with_name("manifest.sha256"))] = hashlib.sha256(checksum_data).hexdigest()
    return value


def _load_index(manifest_path):
    path = _absolute(manifest_path)
    data = _read(path)
    checksum = _read(path.with_name("manifest.sha256"))
    if checksum != (hashlib.sha256(data).hexdigest() + "\n").encode("ascii"):
        raise ValueError("index manifest checksum mismatch")
    record = _loads(data.decode("utf-8"))
    if not isinstance(record, dict) or record.get("manifest_path") != str(path) or not isinstance(record.get("artifacts"), dict):
        raise ValueError("invalid index manifest")
    if (_json(record) + "\n").encode("utf-8") != data:
        raise ValueError("index manifest is not canonical")
    return _attach_manifest(record, data, checksum)


def _verify_index(index, raw_open=None):
    try:
        if (not isinstance(index, dict) or index.get("schema_version") != 1
                or index.get("view") != "timeline" or not _positive(index.get("events"))
                or not _positive(index.get("chunk_chars"))
                or isinstance(index.get("preview_chars"), bool)
                or not isinstance(index.get("preview_chars"), int) or index["preview_chars"] < 0):
            return False
        if _load_index(index["manifest_path"]) != index:
            return False
        raw = index["raw_events"]
        if not isinstance(raw, dict) or not _positive(raw.get("bytes")):
            return False
        root = Path(index["manifest_path"]).parent
        expected_paths = {str(root / "manifest.json"), str(root / "manifest.sha256"), raw["path"]}
        if index.get("source_manifest"):
            expected_paths.add(index["source_manifest"]["path"])
        count, offset, seen = 0, 0, set()
        for chunk_number, chunk in enumerate(index["chunks"], 1):
            expected_path = str(root / "chunks" / (chunk["id"] + ".json"))
            if chunk["path"] != expected_path or chunk["id"] != "t%08d" % chunk_number:
                return False
            if chunk["path"] in expected_paths:
                return False
            expected_paths.add(chunk["path"])
            data = _read(chunk["path"])
            hashed = hashlib.sha256(data).hexdigest()
            if hashed != chunk["sha256"] or hashed != index["artifacts"].get(chunk["path"]):
                return False
            content = _loads(data.decode("utf-8"))
            if (content.get("schema_version") != 1 or content.get("id") != chunk["id"]
                    or content.get("view") != "timeline" or content.get("raw_events_path") != raw["path"]
                    or content.get("note") != NOTE or not isinstance(content.get("events"), list) or not content["events"]):
                return False
            if [row["event_id"] for row in content["events"]] != chunk["event_ids"]:
                return False
            for row in content["events"]:
                if (not isinstance(row, dict) or not isinstance(row.get("event_id"), str)
                        or row["event_id"] in seen or row.get("events_offset") != offset
                        or not _positive(row.get("events_bytes")) or row.get("source_line") != count + 1):
                    return False
                seen.add(row["event_id"])
                offset += row["events_bytes"]
                count += 1
        if count != index["events"] or offset != raw["bytes"] or set(index["artifacts"]) != expected_paths:
            return False
        if raw_open is None:
            raw_hash, raw_size = _hash(raw["path"])
        else:
            stream, before = raw_open
            stream.seek(0)
            value = hashlib.sha256()
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                value.update(block)
            _unchanged(_absolute(raw["path"]), stream, before)
            raw_hash, raw_size = value.hexdigest(), before.st_size
        if raw_hash != raw["sha256"] or raw_size != raw["bytes"] or index["artifacts"][raw["path"]] != raw_hash:
            return False
        if index.get("source_manifest"):
            source = index["source_manifest"]
            if _hash(source["path"])[0] != source["sha256"] or index["artifacts"][source["path"]] != source["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError, OverflowError):
        return False


def verify_index(index):
    """Verify immutable index identity, source hash and complete byte coverage."""
    return _verify_index(index)


def build_index(bundle: dict, destination: Path, chunk_chars=60000, preview_chars=240) -> dict:
    """Publish an immutable timeline without dropping any normalized event."""
    if not _positive(chunk_chars) or isinstance(preview_chars, bool) or not isinstance(preview_chars, int) or preview_chars < 0:
        raise ValueError("chunk_chars must be positive and preview_chars nonnegative integers")
    source = _absolute(bundle["events_path"])
    destination = _absolute(destination)
    _no_links(destination)
    if destination == source or destination in source.parents:
        raise ValueError("index destination must not contain the original events")
    expected_hash = bundle["artifacts"].get(str(source))
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("session bundle must bind its normalized events hash")
    expected_ids = bundle.get("event_ids")
    if (not isinstance(expected_ids, list) or not expected_ids
            or any(not isinstance(item, str) or not item for item in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)):
        raise ValueError("session bundle must contain unique event IDs")
    if destination.exists():
        index = _load_index(destination / "manifest.json")
        if (not verify_index(index) or index["raw_events"]["path"] != str(source)
                or index["raw_events"]["sha256"] != expected_hash
                or index["chunk_chars"] != chunk_chars or index["preview_chars"] != preview_chars
                or [event for chunk in index["chunks"] for event in chunk["event_ids"]] != expected_ids):
            raise ValueError("existing timeline differs from the requested immutable index")
        return index
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="." + destination.name + "-", dir=destination.parent))
    try:
        (temporary / "chunks").mkdir()
        chunks, artifacts, pending, seen_content = [], {}, [], {}
        count, offset, pending_chars = 0, 0, 0
        source_hash = hashlib.sha256()

        def flush():
            nonlocal pending, pending_chars
            if not pending:
                return
            identity = "t%08d" % (len(chunks) + 1)
            relative = Path("chunks") / (identity + ".json")
            content = {"schema_version": 1, "id": identity, "view": "timeline", "events": pending,
                       "raw_events_path": str(source), "note": NOTE}
            hashed = _write(temporary / relative, (_json(content) + "\n").encode("utf-8"))
            path = str(destination / relative)
            chunks.append({"id": identity, "path": path, "sha256": hashed,
                           "event_ids": [row["event_id"] for row in pending]})
            artifacts[path] = hashed
            pending, pending_chars = [], 0

        stream, before = _open_regular(source, maximum=None)
        with stream:
            for encoded in stream:
                event = _loads(encoded.decode("utf-8"))
                row = _row(event, encoded, offset, preview_chars, seen_content)
                if count >= len(expected_ids) or row["event_id"] != expected_ids[count] or row["source_line"] != count + 1:
                    raise ValueError("normalized events do not match the session coverage manifest")
                row_chars = len(_json(row))
                if pending and pending_chars + row_chars > chunk_chars:
                    flush()
                pending.append(row)
                pending_chars += row_chars
                source_hash.update(encoded)
                count += 1
                offset += len(encoded)
            _unchanged(source, stream, before)
        if count != len(expected_ids) or source_hash.hexdigest() != expected_hash:
            raise ValueError("normalized events changed or coverage is incomplete")
        flush()
        artifacts[str(source)] = expected_hash
        source_manifest = None
        if bundle.get("manifest_path"):
            path = str(_absolute(bundle["manifest_path"]))
            hashed, _ = _hash(path)
            if bundle["artifacts"].get(path) != hashed:
                raise ValueError("source session manifest changed")
            source_manifest = {"path": path, "sha256": hashed}
            artifacts[path] = hashed
        record = {"schema_version": 1, "view": "timeline", "events": count,
                  "chunk_chars": chunk_chars, "preview_chars": preview_chars,
                  "raw_events": {"path": str(source), "sha256": expected_hash, "bytes": offset},
                  "source_manifest": source_manifest, "chunks": chunks, "artifacts": artifacts,
                  "manifest_path": str(destination / "manifest.json")}
        manifest_data = (_json(record) + "\n").encode("utf-8")
        hashed = _write(temporary / "manifest.json", manifest_data)
        checksum = (hashed + "\n").encode("ascii")
        _write(temporary / "manifest.sha256", checksum)
        _sync_directory(temporary / "chunks")
        _sync_directory(temporary)
        try:
            os.rename(temporary, destination)
        except OSError:
            if destination.exists():
                return build_index(bundle, destination, chunk_chars, preview_chars)
            raise
        _sync_directory(destination.parent)
        return _attach_manifest(record, manifest_data, checksum)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def read_events(index, event_ids):
    """Return full normalized event bytes, in requested order, after verification."""
    if not isinstance(event_ids, list) or not event_ids or any(not isinstance(item, str) or not item for item in event_ids):
        raise ValueError("select at least one nonempty event ID")
    path = _absolute(index["raw_events"]["path"])
    stream, before = _open_regular(path, maximum=None)
    records = []
    with stream:
        # Verify the large raw file exactly once for the entire lookup, using
        # the same descriptor for reads; never rehash it per event or chunk.
        if not _verify_index(index, (stream, before)):
            raise ValueError("timeline or original events failed integrity verification")
        wanted, found = set(event_ids), {}
        for chunk in index["chunks"]:
            if wanted.intersection(chunk["event_ids"]):
                data = _read(chunk["path"])
                if hashlib.sha256(data).hexdigest() != chunk["sha256"]:
                    raise ValueError("index chunk changed before lookup")
                for row in _loads(data.decode("utf-8"))["events"]:
                    if row["event_id"] in wanted:
                        found[row["event_id"]] = row
        if set(found) != wanted:
            raise ValueError("requested event ID is not present in this index")
        for identity in event_ids:
            row = found[identity]
            stream.seek(row["events_offset"])
            encoded = stream.read(row["events_bytes"])
            record = _loads(encoded.decode("utf-8"))
            if len(encoded) != row["events_bytes"] or record.get("event_id") != identity:
                raise ValueError("index offset does not identify the requested event")
            records.append(encoded if encoded.endswith(b"\n") else encoded + b"\n")
        _unchanged(path, stream, before)
    return records


def main():
    parser = argparse.ArgumentParser(description="Read selected complete events from a verified session timeline")
    parser.add_argument("command", choices=["read"])
    parser.add_argument("--index", required=True)
    parser.add_argument("--event", action="append", required=True)
    args = parser.parse_args()
    try:
        index = _load_index(args.index)
        records = read_events(index, args.event)
        for record in records:
            sys.stdout.buffer.write(record)
        return 0
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
        print("session-index: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
