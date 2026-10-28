"""Framework-owned checks; optional operator checks never come from the model."""
from __future__ import annotations

import argparse
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .storage import atomic_json, checked_diff, read_json


def _kill_group(process):
    """Signal only the process group this checker created, including descendants."""
    if process is None:
        return False
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False


def _run(argv, workspace, log, timeout, limit, control):
    selector = selectors.DefaultSelector()
    process = None
    read_bytes, written_bytes = 0, 0
    truncated, descendants = False, False
    reason = None
    started = time.monotonic()
    try:
        if control["cancelled"]:
            return {"passed": False, "reason": "cancelled", "signal": control["cancelled"]}
        process = subprocess.Popen(argv, cwd=workspace, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True, bufsize=0)
        control["active"] = process
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        exit_at = None
        while True:
            now = time.monotonic()
            code = process.poll()
            if control["cancelled"] and reason is None:
                reason = "cancelled"
                _kill_group(process)
            elif code is None and now - started >= timeout and reason is None:
                reason = "timeout"
                _kill_group(process)
            if code is not None and exit_at is None:
                exit_at = now
                # Even a successful check may have left grandchildren writing
                # directly to files. Their pipes need not remain open.
                descendants = _kill_group(process)
            for key, _ in selector.select(timeout=0.05):
                try:
                    data = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                read_bytes += len(data)
                available = max(0, limit - written_bytes)
                if available:
                    chunk = data[:available]
                    log.write(chunk)
                    written_bytes += len(chunk)
                if len(data) > available:
                    truncated = True
                    reason = reason or "log_limit"
                    _kill_group(process)
            if process.poll() is not None and not selector.get_map():
                break
            # Escaped sessions are outside the owned group. Do not hang on an
            # inherited pipe; disclose incomplete evidence instead of passing.
            if exit_at is not None and now - exit_at >= 1:
                reason = reason or "pipe_remained_open_after_exit"
                break
        code = process.wait(timeout=1)
        if descendants and reason is None:
            reason = "descendants_outlived_check"
        return {"passed": code == 0 and reason is None and not truncated,
                "returncode": code, "reason": reason,
                "log_truncated": truncated, "output_bytes": read_bytes,
                "log_bytes": written_bytes, "descendant_cleanup": descendants,
                "signal": control["cancelled"] or None}
    except OSError as error:
        return {"passed": False, "reason": str(error), "log_truncated": truncated,
                "output_bytes": read_bytes, "log_bytes": written_bytes}
    finally:
        if process is not None:
            _kill_group(process)
            process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
        control["active"] = None
        selector.close()


def check(context):
    output = Path(context["result_path"]).parent
    if output.is_symlink() or not output.is_dir():
        raise ValueError("check output must be an existing real directory")
    limit = context.get("log_limit_bytes", 64 * 1024 * 1024)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("log_limit_bytes must be a positive integer")
    details = []
    cancelled = 0
    if context.get("check_stage") != "execution":
        changed = checked_diff(context["baseline_dir"], context["candidate_dir"],
                               context["allowed_paths"], context["protected_paths"])
        details.append({"id": "skill_diff", "passed": bool(changed), "changed": changed})
    else:
        control = {"active": None, "cancelled": 0}
        previous = {}

        def cancel(signum, _frame):
            control["cancelled"] = signum
            _kill_group(control["active"])

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, cancel)
        try:
            seen = set()
            for item in context.get("checks", []):
                identity = item["id"]
                if (not isinstance(identity, str) or
                        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identity) is None or identity in seen):
                    raise ValueError("check IDs must be unique safe file names")
                seen.add(identity)
                timeout = item["timeout_seconds"]
                if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                        or not math.isfinite(timeout) or timeout <= 0):
                    raise ValueError("check timeout must be finite and positive")
                replacements = {"{workspace}": context["workspace"], "{python}": sys.executable,
                                "{skill_dir}": context["skill_dir"], "{output_dir}": str(output)}
                if (not isinstance(item["argv"], list) or not item["argv"] or
                        any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in item["argv"])):
                    raise ValueError("check argv must be a nonempty string array")
                argv = [replacements.get(arg, arg) for arg in item["argv"]]
                log = output / (identity + ".log")
                if log.is_symlink():
                    raise ValueError("check logs may not be symlinks")
                with log.open("wb", buffering=0) as stream:
                    result = _run(argv, context["workspace"], stream, timeout, limit, control)
                details.append({"id": identity, "log": log.name, **result})
                if control["cancelled"]:
                    break
        finally:
            _kill_group(control["active"])
            cancelled = control["cancelled"]
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    atomic_json(output / "checks.json", {"checks": details,
                "cancelled_signal": cancelled or None,
                "note": "No independent task checks configured" if not details else "Executed checks"})
    return {"passed": not cancelled and all(item["passed"] for item in details),
            "cancelled_signal": cancelled or None,
            "evidence": ["checks.json"] + [item["log"] for item in details if "log" in item]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    context = read_json(args.context)
    result = check(context)
    atomic_json(context["result_path"], result)
    if result["cancelled_signal"]:
        raise SystemExit(128 + result["cancelled_signal"])


if __name__ == "__main__":
    main()
