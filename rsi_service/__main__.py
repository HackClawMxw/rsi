"""Local foreground service CLI; use a service manager for deployment."""
import argparse
import json
import signal
import sys
import time
from pathlib import Path

from .config import load_config
from .controller import Controller, controller_lock, status_summary
from .storage import read_json


def main():
    parser = argparse.ArgumentParser(description="Persistent Codex/Day0 skill evolution service")
    parser.add_argument("command", choices=["serve", "status", "stop", "cancel", "events", "recover"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--tail", type=int, default=20)
    args = parser.parse_args()
    try:
        config_path = Path(args.config).resolve()
        raw = read_json(config_path)
        if not isinstance(raw, dict) or not isinstance(raw.get("state_dir"), str):
            raise ValueError("config requires state_dir")
        root = (config_path.parent / Path(raw["state_dir"]).expanduser()).resolve()
        initialized = (root / "state.json").exists()
        # Operational controls must work even when a model/evaluator mount is
        # gone. The original source is also unnecessary for reattaching workers.
        if initialized and args.command != "serve":
            config = read_json(root / "config.json")
        else:
            config = load_config(config_path, check_paths=not initialized)
        if args.command == "status":
            print(json.dumps(status_summary(config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "events":
            path = root / "events.jsonl"
            if path.exists():
                from collections import deque
                with path.open() as stream:
                    print("".join(deque(stream, maxlen=max(1, args.tail))), end="")
            return 0
        if args.command in ("stop", "cancel"):
            if not (root / "state.json").exists():
                raise ValueError("service has not been initialized")
            if args.command == "stop":
                (root / "stop.request").touch()
                print("Stop requested; in-flight worker will continue. Use serve to reattach.")
            else:
                state = read_json(root / "state.json")
                cycle = None if state["current"] is None else state["cycles"][state["current"]]
                if not cycle or not cycle.get("job"):
                    raise ValueError("no current job to cancel")
                (Path(cycle["job"]["dir"]) / "cancel").touch()
                print("Cancellation requested; inspect status before recovery.")
            return 0
        with controller_lock(root):
            if config.get("optimizer"):
                from .search_controller import SearchController
                controller = SearchController(config)
            else:
                controller = Controller(config)
            if args.command == "recover":
                controller.recover()
                print("Blocked cycle abandoned. Use serve to continue within the remaining budget.")
                return 0
            if controller.state["status"] in ("blocked", "completed"):
                print(json.dumps(status_summary(config), ensure_ascii=False, indent=2))
                return 2 if controller.state["status"] == "blocked" else 0
            (root / "stop.request").unlink(missing_ok=True)
            stop = [False]
            def request_stop(signum, frame):
                stop[0] = True
            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
            controller.state["status"] = "running"
            controller.save()
            controller.event("controller_started")
            print("RSI service running: " + str(root), flush=True)
            while not stop[0] and not (root / "stop.request").exists():
                if not controller.tick():
                    break
                time.sleep(config["limits"]["poll_seconds"])
            if controller.state["status"] not in ("completed", "blocked"):
                controller.state["status"] = "paused"
                controller.save()
                controller.event("controller_paused", worker_continues=True)
            print(json.dumps(status_summary(config), ensure_ascii=False, indent=2))
            return 2 if controller.state["status"] == "blocked" else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("rsi-service: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
