from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from scripts.publish_operation_status import publish_status
from scripts.repair_utils import safe_operation_id, write_json
from scripts.single_task_lock import release

PRODUCTION_WORKFLOW = "expert-team-production.yml"
ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}


def _request_json(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8")
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}


def _post_empty(url: str, token: str) -> int:
    request = urllib.request.Request(url, data=b"", method="POST")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        if exc.code in {409, 422}:
            return exc.code
        raise


def _matching_runs(repository: str, operation_id: str, token: str) -> list[dict[str, Any]]:
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/{PRODUCTION_WORKFLOW}/runs"
        "?event=workflow_dispatch&per_page=100"
    )
    payload = _request_json("GET", url, token)
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        return []
    return [
        run
        for run in runs
        if isinstance(run, dict) and operation_id in str(run.get("display_title") or "")
    ]


def cancel_operation(operation_id: str, reason: str, repository: str, token: str) -> dict[str, Any]:
    safe_id = safe_operation_id(operation_id)
    output_dir = Path("artifacts") / safe_id
    output_dir.mkdir(parents=True, exist_ok=True)
    publish_status(
        safe_id,
        "execute_team",
        "cancel_requested",
        detail=reason,
        current_policy="if-owner",
        active_step="cancellation",
    )

    runs = _matching_runs(repository, safe_id, token)
    active = [run for run in runs if str(run.get("status") or "") in ACTIVE_STATUSES]
    actions: list[dict[str, Any]] = []
    for run in active:
        run_id = str(run.get("id") or "")
        if not run_id:
            continue
        cancel_url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/cancel"
        status = _post_empty(cancel_url, token)
        actions.append({"run_id": run_id, "action": "cancel", "http_status": status})

    if active:
        time.sleep(5)
        latest = {str(run.get("id")): run for run in _matching_runs(repository, safe_id, token)}
        for run in active:
            run_id = str(run.get("id") or "")
            current = latest.get(run_id, {})
            if str(current.get("status") or "") in ACTIVE_STATUSES:
                force_url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/force-cancel"
                status = _post_empty(force_url, token)
                actions.append({"run_id": run_id, "action": "force-cancel", "http_status": status})

    lock_release: dict[str, Any]
    try:
        lock_release = release(safe_id)
    except Exception as exc:
        lock_release = {"released": False, "error": f"{type(exc).__name__}: {exc}"}

    result = {
        "schema_version": "1",
        "operation_id": safe_id,
        "requested_reason": reason,
        "matching_run_count": len(runs),
        "active_run_count": len(active),
        "actions": actions,
        "lock_release": lock_release,
        "status": "cancelled" if active else "not_active",
    }
    write_json(output_dir / "cancellation_result.json", result)
    publish_status(
        safe_id,
        "execute_team",
        "cancelled",
        detail=result["status"],
        current_policy="if-owner",
        active_step="cancelled",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel a production operation by operation_id")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--reason", default="user_requested")
    args = parser.parse_args()
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    repository = (os.getenv("GITHUB_REPOSITORY") or "a15280020511/test2").strip()
    if not token:
        raise RuntimeError("GitHub token is required to cancel workflow runs")
    result = cancel_operation(args.operation_id, args.reason, repository, token)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
