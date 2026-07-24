from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.repair_utils import safe_operation_id


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, text=True)


def _current_run_id() -> int | None:
    value = (os.getenv("GITHUB_RUN_ID") or "").strip()
    return int(value) if value.isdigit() else None


def _build_status(operation_id: str, operation: str, phase: str, job_status: str) -> dict[str, Any]:
    output_dir = Path("artifacts") / operation_id
    metadata = _read_json(output_dir / "metadata.json")
    managed = _read_json(output_dir / "managed_operation.json")
    auto_repair = _read_json(output_dir / "auto_repair_result.json")

    if phase == "start":
        status = "running"
        result_ready = False
        repair_status = "none"
    else:
        metadata_status = str(metadata.get("status") or "").lower()
        managed_status = str(managed.get("status") or "").upper()
        result_file = str(metadata.get("readable_result_file") or metadata.get("result_file") or "").strip()
        result_ready = bool(result_file and (output_dir / result_file).exists())

        if job_status == "success" and metadata_status == "success":
            status = "success"
        elif managed_status == "STOP" or metadata_status == "failure":
            status = "STOP"
        else:
            status = "failure"

        if managed.get("auto_repair_triggered") is True:
            repair_status = (
                "repaired"
                if str(auto_repair.get("resume") or "").upper() == "READY"
                else "attempted"
            )
        else:
            repair_status = "none"

    return {
        "schema_version": "1",
        "operation_id": operation_id,
        "operation": operation,
        "status": status,
        "run_id": _current_run_id(),
        "result_ready": result_ready,
        "repair_status": repair_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _publish_once(payload: dict[str, Any], operation_id: str) -> None:
    _run(["git", "config", "user.name", "github-actions[bot]"])
    _run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    _run(["git", "fetch", "origin", "runtime-results"])

    with tempfile.TemporaryDirectory(prefix="test2-operation-status-") as tmp:
        worktree = Path(tmp) / "worktree"
        _run(["git", "worktree", "add", str(worktree), "origin/runtime-results"])
        try:
            target = worktree / "runtime_results" / "status" / f"{operation_id}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            _run(["git", "-C", str(worktree), "add", str(target.relative_to(worktree))])
            diff = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--cached", "--quiet"],
                check=False,
                text=True,
            )
            if diff.returncode == 0:
                return
            _run(["git", "-C", str(worktree), "commit", "-m", f"Update operation status {operation_id}"])
            _run(["git", "-C", str(worktree), "push", "origin", "HEAD:runtime-results"])
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], check=False)


def _publish(payload: dict[str, Any], operation_id: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            _publish_once(payload, operation_id)
            return
        except Exception as exc:  # retry only this tiny control-plane publication
            last_error = exc
            subprocess.run(["git", "worktree", "prune"], check=False)
            if attempt < 3:
                time.sleep(2)
    assert last_error is not None
    raise last_error


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish a small operation status record for Web GPT control flow")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--phase", choices=("start", "final"), required=True)
    parser.add_argument("--job-status", default="")
    args = parser.parse_args()

    operation_id = safe_operation_id(args.operation_id)
    payload = _build_status(operation_id, args.operation, args.phase, args.job_status)
    _publish(payload, operation_id)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
