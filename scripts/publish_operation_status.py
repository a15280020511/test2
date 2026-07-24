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

CURRENT_STATUS_PATH = Path("runtime_results/current_operation_status.json")
HISTORY_STATUS_DIR = Path("runtime_results/status")


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


def _current_run_id() -> str | None:
    value = (os.getenv("GITHUB_RUN_ID") or "").strip()
    return value or None


def _optional_text(explicit: str, env_name: str) -> str | None:
    value = (explicit or os.getenv(env_name) or "").strip()
    return value or None


def _build_status(
    operation_id: str,
    operation: str,
    phase: str,
    job_status: str = "",
    result_published: str = "",
    receipt_comment_id: str = "",
    supervisor_for_operation_id: str = "",
) -> dict[str, Any]:
    output_dir = Path("artifacts") / operation_id
    metadata = _read_json(output_dir / "metadata.json")
    managed = _read_json(output_dir / "managed_operation.json")
    auto_repair = _read_json(output_dir / "auto_repair_result.json")

    result_file = str(metadata.get("readable_result_file") or metadata.get("result_file") or "").strip()
    local_result_exists = bool(result_file and (output_dir / result_file).exists())
    published = result_published.strip().lower() == "success"

    if phase == "start":
        status = "running"
        result_ready = False
        repair_status = "none"
    elif phase == "repairing":
        status = "repairing"
        result_ready = False
        repair_status = "repairing"
    elif phase == "retrying":
        status = "retrying"
        result_ready = False
        repair_status = "attempted"
    elif phase == "final":
        metadata_status = str(metadata.get("status") or "").lower()
        managed_status = str(managed.get("status") or "").upper()
        result_ready = bool(published and local_result_exists)

        if job_status == "success" and metadata_status == "success" and result_ready:
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
    else:
        raise ValueError(f"unsupported status phase: {phase}")

    return {
        "schema_version": "3",
        "operation_id": operation_id,
        "operation": operation,
        "status": status,
        "run_id": _current_run_id(),
        "receipt_comment_id": _optional_text(receipt_comment_id, "RECEIPT_COMMENT_ID"),
        "supervisor_for_operation_id": _optional_text(
            supervisor_for_operation_id,
            "SUPERVISOR_FOR_OPERATION_ID",
        ),
        "result_ready": result_ready,
        "result_published": published,
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
            history_target = worktree / HISTORY_STATUS_DIR / f"{operation_id}.json"
            current_target = worktree / CURRENT_STATUS_PATH
            history_target.parent.mkdir(parents=True, exist_ok=True)
            current_target.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            history_target.write_text(encoded, encoding="utf-8")
            current_target.write_text(encoded, encoding="utf-8")

            _run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "add",
                    str(history_target.relative_to(worktree)),
                    str(current_target.relative_to(worktree)),
                ]
            )
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


def publish_status(
    operation_id: str,
    operation: str,
    phase: str,
    *,
    job_status: str = "",
    result_published: str = "",
    receipt_comment_id: str = "",
    supervisor_for_operation_id: str = "",
) -> dict[str, Any]:
    safe_id = safe_operation_id(operation_id)
    payload = _build_status(
        safe_id,
        operation,
        phase,
        job_status,
        result_published,
        receipt_comment_id,
        supervisor_for_operation_id,
    )
    last_error: Exception | None = None
    delays = (1, 2, 4, 8)
    for attempt in range(5):
        try:
            _publish_once(payload, safe_id)
            return payload
        except Exception as exc:
            last_error = exc
            subprocess.run(["git", "worktree", "prune"], check=False)
            if attempt < 4:
                time.sleep(delays[attempt])
    assert last_error is not None
    raise last_error


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish permanent and per-operation status records for Web GPT control flow")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--phase", choices=("start", "repairing", "retrying", "final"), required=True)
    parser.add_argument("--job-status", default="")
    parser.add_argument("--result-published", default="")
    parser.add_argument("--receipt-comment-id", default="")
    parser.add_argument("--supervisor-for-operation-id", default="")
    args = parser.parse_args()

    payload = publish_status(
        args.operation_id,
        args.operation,
        args.phase,
        job_status=args.job_status,
        result_published=args.result_published,
        receipt_comment_id=args.receipt_comment_id,
        supervisor_for_operation_id=args.supervisor_for_operation_id,
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
