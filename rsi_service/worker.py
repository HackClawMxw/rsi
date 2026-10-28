"""One durable, detached command supervisor (Python 3.9+, POSIX).

The controller writes request.json before launching this module.  A worker owns
one job for its entire lifetime and keeps writing heartbeat/status files even
when the controller is stopped.  An existing status file is never retried.
"""

import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time


TERMINAL_STATES = frozenset(("completed", "failed", "cancelled", "timed_out"))


def _save_status(job_dir, status):
    status["updated_at"] = time.time()
    temporary = job_dir / (".status-%s.tmp" % os.getpid())
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(status, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(job_dir / "status.json"))


def _positive_number(request, key, default=None):
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a positive finite number" % key)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("%s must be a positive finite number" % key)
    return value


def _validate_request(request):
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    argv = request.get("argv")
    if not isinstance(argv, list) or not argv or any(
        not isinstance(arg, str) or "\0" in arg for arg in argv
    ):
        raise ValueError("argv must be a nonempty array of strings")
    cwd = request.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        raise ValueError("cwd must name an existing directory")
    env = request.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise ValueError("env must be a string-to-string object")
    if request.get("stdin") is not None and not isinstance(request["stdin"], str):
        raise ValueError("stdin must be a string or null")
    _positive_number(request, "timeout_seconds")
    _positive_number(request, "kill_grace_seconds", 2.0)
    limit = request.get("log_limit_bytes", 64 * 1024 * 1024)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("log_limit_bytes must be a positive integer")


def _signal_group(process, signum):
    # Only the child session created by this worker is ever signalled.  A new
    # controller must never use persisted PIDs to signal a possibly reused PID.
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _supervise(job_dir, request, status, cancelled):
    environment = os.environ.copy()
    environment.update(request.get("env", {}))
    timeout = request["timeout_seconds"]
    grace = request.get("kill_grace_seconds", 2.0)
    limit = request.get("log_limit_bytes", 64 * 1024 * 1024)
    selector = selectors.DefaultSelector()
    process = None
    inputs = None
    outputs = {}
    started = time.monotonic()
    next_heartbeat = started
    termination = None
    terminate_at = None
    exit_at = None
    cleanup_sent = False
    try:
        # A file avoids blocking the supervision loop while writing a long
        # prompt to a child that has not started reading stdin yet.
        if request.get("stdin") is not None:
            input_path = job_dir / "worker.stdin"
            input_path.write_text(request["stdin"], encoding="utf-8")
            inputs = input_path.open("rb")
        for name in ("stdout", "stderr"):
            outputs[name] = (job_dir / (name + ".log")).open("wb", buffering=0)
            status[name + "_bytes"] = 0
        status["log_truncated"] = False
        if cancelled[0] or (job_dir / "cancel").exists():
            status.update(state="cancelled", reason="cancel requested before launch")
            return
        process = subprocess.Popen(
            request["argv"],
            cwd=request["cwd"],
            env=environment,
            stdin=inputs if inputs is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            bufsize=0,
        )
        status["child_pid"] = process.pid
        _save_status(job_dir, status)
        for name in ("stdout", "stderr"):
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)

        while True:
            now = time.monotonic()
            returncode = process.poll()
            if returncode is not None and exit_at is None:
                exit_at = now
            if termination is None and returncode is None:
                if cancelled[0] or (job_dir / "cancel").exists():
                    termination = "cancelled"
                elif now - started >= timeout:
                    termination = "timed_out"
                if termination:
                    terminate_at = now
                    _signal_group(process, signal.SIGTERM)
            # On cancellation/timeout always finish group cleanup, including
            # descendants that ignored TERM after the direct child exited.
            if terminate_at is not None and not cleanup_sent and now - terminate_at >= grace:
                _signal_group(process, signal.SIGKILL)
                cleanup_sent = True
            # A grandchild inheriting pipes cannot keep a completed CLI's job
            # alive indefinitely.  Give ordinary buffered output time to drain.
            if exit_at is not None and selector.get_map() and termination is None:
                if now - exit_at >= grace and not cleanup_sent:
                    _signal_group(process, signal.SIGKILL)
                    cleanup_sent = True
                    status["descendant_cleanup"] = True
            if now >= next_heartbeat:
                _save_status(job_dir, status)
                next_heartbeat = now + 1.0

            for key, _ in selector.select(timeout=0.1):
                try:
                    data = os.read(key.fd, 64 * 1024)
                except BlockingIOError:
                    continue
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                previous = status[name + "_bytes"]
                status[name + "_bytes"] += len(data)
                remaining = max(0, limit - previous)
                if remaining:
                    outputs[name].write(data[:remaining])
                if len(data) > remaining:
                    status["log_truncated"] = True

            returncode = process.poll()
            if returncode is not None:
                if termination and not cleanup_sent:
                    continue
                if not selector.get_map():
                    break
                # Descendants that deliberately escape the process group may
                # still hold a pipe.  Close our ends rather than wait forever.
                if exit_at is not None and now - exit_at >= grace + 2.0:
                    status["pipes_closed_after_exit"] = True
                    break

        status["returncode"] = process.wait()
        if termination:
            status["state"] = termination
            status["reason"] = (
                "wall-clock timeout exceeded" if termination == "timed_out" else "cancel requested"
            )
        elif status["returncode"] == 0:
            status.update(state="completed", reason="command exited successfully")
        else:
            status.update(state="failed", reason="command exited with nonzero status")
    finally:
        if process is not None and process.poll() is None:
            _signal_group(process, signal.SIGKILL)
            process.wait()
        for key in list(selector.get_map().values()):
            key.fileobj.close()
        selector.close()
        for stream in outputs.values():
            stream.close()
        if inputs is not None:
            inputs.close()


def run_job(job_dir):
    """Execute exactly once; return 2 when another/existing worker owns the job."""
    job_dir = Path(job_dir).resolve(strict=True)
    with (job_dir / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2
        if (job_dir / "status.json").exists():
            return 2
        status = {
            "state": "running",
            "worker_pid": os.getpid(),
            "started_at": time.time(),
            "updated_at": time.time(),
            "finished_at": None,
            "returncode": None,
            "reason": "starting command",
        }
        cancelled = [False]

        def request_cancel(_signum, _frame):
            cancelled[0] = True

        previous_handlers = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_cancel)
        try:
            # Persist intent before Popen.  A crash here is ambiguous and must
            # be reconciled by the operator, never silently replayed.
            _save_status(job_dir, status)
            request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
            _validate_request(request)
            _supervise(job_dir, request, status, cancelled)
        except Exception as exc:
            status.update(state="failed", reason="%s: %s" % (type(exc).__name__, exc))
        finally:
            status["finished_at"] = time.time()
            _save_status(job_dir, status)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        return 0 if status["state"] == "completed" else 1


def main():
    if len(sys.argv) != 2:
        print("usage: python -m rsi_service.worker JOB_DIR", file=sys.stderr)
        return 2
    try:
        return run_job(sys.argv[1])
    except Exception as exc:
        print("worker error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
