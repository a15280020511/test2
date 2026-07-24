from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.publish_operation_status import publish_status
from scripts.single_task_lock import heartbeat


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one command while refreshing operation and single-task heartbeats")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--active-step", required=True)
    parser.add_argument("--interval-seconds", type=int, default=120)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("wrapped command is required after --")
    if args.interval_seconds < 30 or args.interval_seconds > 300:
        raise ValueError("interval-seconds must be between 30 and 300")

    log_dir = Path("artifacts") / args.operation_id
    log_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_log = log_dir / "heartbeat_errors.log"
    process = subprocess.Popen(command)
    next_heartbeat = time.monotonic() + args.interval_seconds
    while process.poll() is None:
        time.sleep(2)
        if time.monotonic() < next_heartbeat:
            continue
        errors: list[str] = []
        try:
            heartbeat(args.operation_id, args.run_id)
        except Exception as exc:
            errors.append(f"lock heartbeat: {type(exc).__name__}: {exc}")
        try:
            publish_status(
                args.operation_id,
                args.operation,
                "heartbeat",
                active_step=args.active_step,
                current_policy="if-owner",
            )
        except Exception as exc:
            errors.append(f"status heartbeat: {type(exc).__name__}: {exc}")
        if errors:
            with heartbeat_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"errors": errors}, ensure_ascii=False) + "\n")
        next_heartbeat = time.monotonic() + args.interval_seconds

    raise SystemExit(process.returncode or 0)


if __name__ == "__main__":
    main()
