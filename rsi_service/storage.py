"""Content identities and crash-safe local persistence."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import shutil
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def read_json(path):
    def reject_constant(value):
        raise ValueError("nonfinite JSON number: " + value)

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("JSON number outside finite float range")
        return parsed

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"),
                          parse_constant=reject_constant, parse_float=finite_float)
    except RecursionError as error:
        raise ValueError("JSON nesting exceeds parser limits") from error
    # Escaped lone surrogates can parse but cannot be persisted as UTF-8. Check
    # before a receipt enters state so a failed save cannot poison recovery.
    pending = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return data


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def manifest(root):
    """Reject links/special files: snapshot identity includes file content and mode."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("snapshot must be a real directory: " + str(root))
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError("symlinks are not allowed in snapshots: " + name)
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("special file in snapshot: " + name)
        result[name] = {"sha256": file_hash(path), "executable": bool(path.stat().st_mode & 0o111)}
    if "SKILL.md" not in result:
        raise ValueError("snapshot requires SKILL.md")
    return result


def snapshot(source, versions):
    files = manifest(source)
    anchor = digest(files)
    target = Path(versions) / anchor
    if target.exists():
        if manifest(target) != files:
            raise ValueError("stored snapshot was modified: " + anchor)
    else:
        temporary = Path(versions) / (anchor + ".building")
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, temporary)
        if manifest(temporary) != files:
            raise ValueError("skill changed while snapshotting")
        temporary.rename(target)
    return anchor


def checked_diff(baseline, candidate, allowed, protected):
    old, new = manifest(baseline), manifest(candidate)
    changed = sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))
    for name in changed:
        if any(fnmatch.fnmatchcase(name, pat) for pat in protected):
            raise ValueError("protected skill path changed: " + name)
        if not any(fnmatch.fnmatchcase(name, pat) for pat in allowed):
            raise ValueError("skill path is not allowlisted: " + name)
    return changed
