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
LEGACY_HISTORY_STATUS_DIR = Path("runtime_results/status")
OPERATIONS_DIR = Path("runtime_results/operations")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            f"STDOUT:\n{completed.stdout[-4000:]}\nSTDERR:\n{completed.stderr[-4000:]}"
        )


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
    active_step: str = "",
    attempt: int = 1,
    detail: str = "",
) -> dict[str, Any]:
    output_dir = Path("artifacts") / operation_id
    metadata = _read_json(output_dir / "metadata.json")
    managed = _read_json(output_dir / "managed_operation.json")
    auto_repair = _read_json(output_dir / "auto_repair_result.json")
    lock_result = _read_json(output_dir / "single_task_lock.json")

    result_file = str(metadata.get("readable_result_file") or metadata.get("result_file") or "").strip()
    local_result_exists = bool(result_file and (output_dir / result_file).exists())
    published = result_published.strip().lower() == "success"
    now = datetime.now(timezone.utc).isoformat()

    status = phase
    result_ready = False
    repair_status = "not_triggered"
    cancel_requested = phase in {"cancel_requested", "cancelled"}

    if phase in {"accepted", "queued"}:
        status = phase
    elif phase == "busy":
        status = "BUSY"
    elif phase in {"start", "heartbeat"}:
        status = "running"
    elif phase == "repairing":
        status = "repairing"
        repair_status = "diagnosing"
    elif phase == "retrying":
        status = "retrying"
        repair_status = "retry_authorized"
    elif phase == "cancel_requested":
        status = "cancel_requested"
    elif phase == "cancelled":
        status = "cancelled"
    elif phase == "final":
        metadata_status = str(metadata.get("status") or "").lower()
        managed_status = str(managed.get("status") or "").upper()
        result_ready = bool(published and local_result_exists)
        if job_status == "success" and metadata_status == "success" and result_ready:
            status = "success"
        elif managed_status == "STOP":
            status = "STOP"
        else:
            status = "failure"

        if managed.get("auto_repair_triggered") is True:
            retry = auto_repair.get("retry") if isinstance(auto_repair.get("retry"), dict) else {}
            if retry.get("status") == "success" and status == "success":
                repair_status = "retry_succeeded"
            elif retry.get("status") == "failure":
                repair_status = "retry_failed"
            elif str(auto_repair.get("resume") or "").upper() == "READY":
                repair_status = "retry_authorized"
            else:
                repair_status = "diagnosed"
    else:
        raise ValueError(f"unsupported status phase: {phase}")

    return {
        "schema_version": "4",
        "operation_id": operation_id,
        "operation": operation,
        "status": status,
        "run_id": _current_run_id(),
        "attempt": max(1, int(attempt)),
        "receipt_comment_id": _optional_text(receipt_comment_id, "RECEIPT_COMMENT_ID"),
        "supervisor_for_operation_id": _optional_text(supervisor_for_operation_id, "SUPERVISOR_FOR_OPERATION_ID"),
        "result_ready": result_ready,
        "result_published": published,
        "repair_status": repair_status,
        "active_step": active_step or None,
        "heartbeat_at": now,
        "updated_at": now,
        "cancel_requested": cancel_requested,
        "detail": detail or None,
        "lock_owner_operation_id": lock_result.get("owner_operation_id"),
        "busy_owner_operation_id": lock_result.get("owner_operation_id") if status == "BUSY" else None,
        "busy_owner_run_id": lock_result.get("owner_run_id") if status == "BUSY" else None,
    }


def _should_update_current(current: dict[str, Any], payload: dict[str, Any], policy: str) -> bool:
    if policy == "always":
        return True
    if policy == "never":
        return False
    if policy == "if-owner":
        current_id = str(current.get("operation_id") or "")
        return not current_id or current_id == str(payload.get("operation_id") or "")
    raise ValueError(f"unsupported current policy: {policy}")


def _publish_once(payload: dict[str, Any], operation_id: str, current_policy: str) -> None:
    _run(["git", "config", "user.name", "github-actions[bot]"])
    _run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    _run(["git", "fetch", "origin", "runtime-results"])

    with tempfile.TemporaryDirectory(prefix="test2-operation-status-") as tmp:
        worktree = Path(tmp) / "worktree"
        _run(["git", "worktree", "add", str(worktree), "origin/runtime-results"])
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            legacy_target = worktree / LEGACY_HISTORY_STATUS_DIR / f"{operation_id}.json"
            operation_target = worktree / OPERATIONS_DIR / operation_id / "state.json"
            run_id = str(payload.get("run_id") or "no-run")
            attempt_target = worktree / OPERATIONS_DIR / operation_id / "attempts" / f"{run_id}.json"
            current_target = worktree / CURRENT_STATUS_PATH
            for target in (legacy_target, operation_target, attempt_target, current_target):
                target.parent.mkdir(parents=True, exist_ok=True)
            legacy_target.write_text(encoded, encoding="utf-8")
            operation_target.write_text(encoded, encoding="utf-8")
            attempt_target.write_text(encoded, encoding="utf-8")

            current = _read_json(current_target)
            if _should_update_current(current, payload, current_policy):
                current_target.write_text(encoded, encoding="utf-8")

            _run(["git", "-C", str(worktree), "add", "runtime_results"])
            diff = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--cached", "--quiet"],
                check=False,
                text=True,
            )
            if diff.returncode == 0:
                return
            _run(["git", "-C", str(worktree), "commit", "-m", f"Update operation ledger {operation_id}"])
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
    active_step: str = "",
    attempt: int = 1,
    detail: str = "",
    current_policy: str = "always",
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
        active_step,
        attempt,
        detail,
    )
    last_error: Exception | None = None
    delays = (1, 2, 4, 8)
    for attempt_index in range(5):
        try:
            _publish_once(payload, safe_id, current_policy)
            return payload
        except Exception as exc:
            last_error = exc
            subprocess.run(["git", "worktree", "prune"], check=False)
            if attempt_index < 4:
                time.sleep(delays[attempt_index])
    assert last_error is not None
    raise last_error


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish permanent, per-operation, and per-attempt status records")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument(
        "--phase",
        choices=("accepted", "queued", "busy", "start", "heartbeat", "repairing", "retrying", "cancel_requested", "cancelled", "final"),
        required=True,
    )
    parser.add_argument("--job-status", default="")
    parser.add_argument("--result-published", default="")
    parser.add_argument("--receipt-comment-id", default="")
    parser.add_argument("--supervisor-for-operation-id", default="")
    parser.add_argument("--active-step", default="")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--detail", default="")
    parser.add_argument("--current-policy", choices=("always", "never", "if-owner"), default="always")
    args = parser.parse_args()

    payload = publish_status(
        args.operation_id,
        args.operation,
        args.phase,
        job_status=args.job_status,
        result_published=args.result_published,
        receipt_comment_id=args.receipt_comment_id,
        supervisor_for_operation_id=args.supervisor_for_operation_id,
        active_step=args.active_step,
        attempt=args.attempt,
        detail=args.detail,
        current_policy=args.current_policy,
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
