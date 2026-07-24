from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.repair_utils import safe_operation_id

LOCK_PATH = Path("runtime_results/control/single_task_lock.json")
DEFAULT_TTL_SECONDS = 75 * 60


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            f"STDOUT:\n{completed.stdout[-4000:]}\nSTDERR:\n{completed.stderr[-4000:]}"
        )
    return completed


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"state": "idle"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "invalid"}
    return value if isinstance(value, dict) else {"state": "invalid"}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_stale(lock: dict[str, Any], now: datetime) -> bool:
    expires_at = _parse_time(lock.get("expires_at"))
    if expires_at is None:
        return lock.get("state") not in {None, "idle"}
    return expires_at <= now


def _write_github_output(payload: dict[str, Any], output_path: str) -> None:
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"acquired={'true' if payload.get('acquired') else 'false'}\n")
        handle.write(f"lock_state={payload.get('state', '')}\n")
        handle.write(f"owner_operation_id={payload.get('owner_operation_id') or ''}\n")
        handle.write(f"owner_run_id={payload.get('owner_run_id') or ''}\n")
        handle.write(f"reason={payload.get('reason') or ''}\n")


def _commit_and_push(worktree: Path, message: str) -> bool:
    _run(["git", "-C", str(worktree), "add", str(LOCK_PATH)])
    diff = _run(["git", "-C", str(worktree), "diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        return True
    _run(["git", "-C", str(worktree), "commit", "-m", message])
    pushed = _run(["git", "-C", str(worktree), "push", "origin", "HEAD:runtime-results"], check=False)
    return pushed.returncode == 0


def _with_worktree() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    _run(["git", "config", "user.name", "github-actions[bot]"])
    _run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    _run(["git", "fetch", "origin", "runtime-results"])
    tmp = tempfile.TemporaryDirectory(prefix="test2-single-task-lock-")
    worktree = Path(tmp.name) / "worktree"
    _run(["git", "worktree", "add", str(worktree), "origin/runtime-results"])
    return tmp, worktree


def acquire(operation_id: str, run_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    safe_id = safe_operation_id(operation_id)
    run_id = str(run_id).strip()
    if not run_id:
        raise ValueError("run_id is required")
    if ttl_seconds < 300 or ttl_seconds > 24 * 60 * 60:
        raise ValueError("ttl_seconds must be between 300 and 86400")

    last_error = ""
    for attempt in range(1, 7):
        tmp: tempfile.TemporaryDirectory[str] | None = None
        worktree: Path | None = None
        try:
            tmp, worktree = _with_worktree()
            target = worktree / LOCK_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            lock = _read_json(target)
            now = datetime.now(timezone.utc)
            same_owner = (
                lock.get("state") == "held"
                and str(lock.get("owner_operation_id") or "") == safe_id
                and str(lock.get("owner_run_id") or "") == run_id
            )
            if same_owner:
                payload = dict(lock)
                payload.update({"acquired": True, "reason": "already_owned"})
                return payload

            if lock.get("state") == "held" and not _is_stale(lock, now):
                return {
                    "acquired": False,
                    "state": "busy",
                    "reason": "another_operation_holds_the_single_task_lock",
                    "owner_operation_id": lock.get("owner_operation_id"),
                    "owner_run_id": lock.get("owner_run_id"),
                    "owner_acquired_at": lock.get("acquired_at"),
                    "owner_expires_at": lock.get("expires_at"),
                }

            payload = {
                "schema_version": "1",
                "state": "held",
                "owner_operation_id": safe_id,
                "owner_run_id": run_id,
                "acquired_at": now.isoformat(),
                "heartbeat_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
                "previous_lock_state": lock.get("state"),
                "stale_lock_replaced": bool(lock.get("state") == "held"),
            }
            target.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            if _commit_and_push(worktree, f"Acquire single-task lock {safe_id}"):
                payload.update({"acquired": True, "reason": "acquired", "attempt": attempt})
                return payload
            last_error = "concurrent lock update rejected"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if worktree is not None:
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], check=False)
            if tmp is not None:
                tmp.cleanup()
            subprocess.run(["git", "worktree", "prune"], check=False)
        if attempt < 6:
            time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"Could not acquire single-task lock: {last_error}")


def heartbeat(operation_id: str, run_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    safe_id = safe_operation_id(operation_id)
    for attempt in range(1, 5):
        tmp: tempfile.TemporaryDirectory[str] | None = None
        worktree: Path | None = None
        try:
            tmp, worktree = _with_worktree()
            target = worktree / LOCK_PATH
            lock = _read_json(target)
            if str(lock.get("owner_operation_id") or "") != safe_id or str(lock.get("owner_run_id") or "") != str(run_id):
                return {"updated": False, "reason": "not_lock_owner", **lock}
            now = datetime.now(timezone.utc)
            lock["heartbeat_at"] = now.isoformat()
            lock["expires_at"] = (now + timedelta(seconds=ttl_seconds)).isoformat()
            target.write_text(json.dumps(lock, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            if _commit_and_push(worktree, f"Heartbeat single-task lock {safe_id}"):
                return {"updated": True, **lock}
        finally:
            if worktree is not None:
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], check=False)
            if tmp is not None:
                tmp.cleanup()
            subprocess.run(["git", "worktree", "prune"], check=False)
        if attempt < 4:
            time.sleep(attempt)
    raise RuntimeError("Could not refresh single-task lock heartbeat")


def release(operation_id: str, run_id: str = "") -> dict[str, Any]:
    safe_id = safe_operation_id(operation_id)
    for attempt in range(1, 6):
        tmp: tempfile.TemporaryDirectory[str] | None = None
        worktree: Path | None = None
        try:
            tmp, worktree = _with_worktree()
            target = worktree / LOCK_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            lock = _read_json(target)
            owner_id = str(lock.get("owner_operation_id") or "")
            owner_run = str(lock.get("owner_run_id") or "")
            if lock.get("state") != "held" or owner_id != safe_id:
                return {"released": False, "reason": "not_lock_owner", **lock}
            if run_id and owner_run and owner_run != str(run_id):
                return {"released": False, "reason": "run_id_mismatch", **lock}
            now = datetime.now(timezone.utc)
            payload = {
                "schema_version": "1",
                "state": "idle",
                "owner_operation_id": None,
                "owner_run_id": None,
                "released_operation_id": safe_id,
                "released_run_id": owner_run or None,
                "released_at": now.isoformat(),
            }
            target.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            if _commit_and_push(worktree, f"Release single-task lock {safe_id}"):
                return {"released": True, **payload}
        finally:
            if worktree is not None:
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], check=False)
            if tmp is not None:
                tmp.cleanup()
            subprocess.run(["git", "worktree", "prune"], check=False)
        if attempt < 5:
            time.sleep(attempt)
    raise RuntimeError("Could not release single-task lock")


def main() -> None:
    parser = argparse.ArgumentParser(description="Atomic global single-task lock for test2 production operations")
    parser.add_argument("command", choices=("acquire", "heartbeat", "release"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--run-id", default=os.getenv("GITHUB_RUN_ID", ""))
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    parser.add_argument("--github-output", default=os.getenv("GITHUB_OUTPUT", ""))
    args = parser.parse_args()

    if args.command == "acquire":
        result = acquire(args.operation_id, args.run_id, args.ttl_seconds)
        _write_github_output(result, args.github_output)
    elif args.command == "heartbeat":
        result = heartbeat(args.operation_id, args.run_id, args.ttl_seconds)
    else:
        result = release(args.operation_id, args.run_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
