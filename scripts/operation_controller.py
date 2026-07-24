from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.publish_operation_status import publish_status
from scripts.repair_utils import safe_operation_id, write_json

PRODUCTION_WORKFLOW = "expert-team-production.yml"
SUPERVISOR_WORKFLOW = "deepseek-supervisor.yml"
TERMINAL_STATES = {"success", "STOP", "failure", "cancelled"}
ACTIVE_STATES = {"running", "repairing", "retrying", "cancel_requested"}
MAX_CONTROLLER_INPUT_CHARS = 45_000


def _request_json(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}


def _dispatch(repository: str, workflow: str, token: str, payload: dict[str, Any]) -> None:
    _request_json(
        "POST",
        f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches",
        token,
        payload,
    )


def _event_request() -> tuple[dict[str, Any], int]:
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path.exists():
        raise RuntimeError("GITHUB_EVENT_PATH is unavailable")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    issue = event.get("issue") if isinstance(event, dict) else None
    comment = event.get("comment") if isinstance(event, dict) else None
    if not isinstance(issue, dict) or int(issue.get("number") or 0) != 15:
        raise RuntimeError("Operation controller accepts submissions only from control issue #15")
    if not isinstance(comment, dict):
        raise RuntimeError("Issue comment payload is missing")
    body = comment.get("body")
    if not isinstance(body, str):
        raise RuntimeError("Issue comment body is missing")
    if len(body) > MAX_CONTROLLER_INPUT_CHARS:
        raise RuntimeError(
            f"Operation submission exceeds the safe controller payload limit of {MAX_CONTROLLER_INPUT_CHARS} characters; "
            "reduce evidence text or submit a smaller evidence reference"
        )
    try:
        request = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Operation submission comment is not valid JSON: {exc}") from exc
    if not isinstance(request, dict) or request.get("command") != "submit_operation":
        raise RuntimeError("Issue comment is not a submit_operation command")
    return request, int(comment.get("id") or 0)


def _normalize_json_input(value: Any, field: str) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{field} string is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{field} must contain a JSON object")
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    raise RuntimeError(f"{field} must be an object or stringified object")


def _normalize_request(raw: dict[str, Any], receipt_comment_id: int) -> dict[str, Any]:
    operation_id = safe_operation_id(str(raw.get("operation_id") or ""))
    operation = str(raw.get("operation") or "").strip()
    if operation not in {"model_intelligence", "execute_team", "deepseek_steward"}:
        raise RuntimeError("operation must be model_intelligence, execute_team, or deepseek_steward")
    ranking_limit = str(raw.get("ranking_limit") or "20").strip()
    if not ranking_limit.isdigit() or int(ranking_limit) < 1 or int(ranking_limit) > 100:
        raise RuntimeError("ranking_limit must be between 1 and 100")
    steward_mode = str(raw.get("steward_mode") or "ASSIST").upper()
    if steward_mode not in {"ASSIST", "REPAIR"}:
        raise RuntimeError("steward_mode must be ASSIST or REPAIR")
    plan_json = _normalize_json_input(raw.get("plan_json"), "plan_json")
    support_packet_json = _normalize_json_input(raw.get("support_packet_json"), "support_packet_json")
    if len(plan_json) + len(support_packet_json) > MAX_CONTROLLER_INPUT_CHARS:
        raise RuntimeError(
            f"Normalized plan and support packet exceed the safe dispatch limit of {MAX_CONTROLLER_INPUT_CHARS} characters"
        )
    return {
        "operation_id": operation_id,
        "operation": operation,
        "receipt_comment_id": str(receipt_comment_id),
        "plan_json": plan_json,
        "ranking_limit": ranking_limit,
        "steward_mode": steward_mode,
        "support_packet_json": support_packet_json,
        "task_label": str(raw.get("task_label") or operation_id)[:200],
        "resubmit_busy": bool(raw.get("resubmit_busy", False)),
    }


def _fetch_runtime_state(operation_id: str) -> dict[str, Any]:
    subprocess.run(["git", "fetch", "origin", "runtime-results"], check=False, capture_output=True, text=True)
    completed = subprocess.run(
        ["git", "show", f"origin/runtime-results:runtime_results/operations/{operation_id}/state.json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return {}
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _production_payload(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": "main",
        "inputs": {
            "operation_id": request["operation_id"],
            "operation": request["operation"],
            "receipt_comment_id": request["receipt_comment_id"],
            "plan_json": request["plan_json"],
            "ranking_limit": request["ranking_limit"],
            "steward_mode": request["steward_mode"],
            "support_packet_json": request["support_packet_json"],
        },
    }


def _dispatch_startup_supervisor(
    *, repository: str, token: str, request: dict[str, Any], controller_run_id: str,
    production_payload: dict[str, Any], last_state: dict[str, Any],
) -> str:
    supervisor_id = safe_operation_id(f"supervisor-{request['operation_id']}-startup-{controller_run_id}")
    support_packet = {
        "operation_id": supervisor_id,
        "original_operation_id": request["operation_id"],
        "receipt_comment_id": request["receipt_comment_id"],
        "mode": "REPAIR",
        "failure_class": "startup_timeout",
        "request": "Diagnose why an accepted production task did not reach a Worker-owned operation state within the startup window.",
        "current_state": last_state or {"status": "accepted_only"},
        "failure_location": "operation controller to production Worker handoff",
        "run_id": controller_run_id,
        "attempts_already_made": ["Production workflow_dispatch was accepted once."],
        "constraints": [
            "Use official DeepSeek API only",
            "Do not duplicate an active matching Run",
            "Preserve user intent and budget",
            "Resume the production operation at most once",
        ],
        "requested_outcome": "Confirm an active Run or safely restore the production dispatch.",
    }
    payload = {
        "ref": "main",
        "inputs": {
            "operation_id": supervisor_id,
            "original_operation_id": request["operation_id"],
            "failed_run_id": "",
            "receipt_comment_id": request["receipt_comment_id"],
            "failure_class": "startup_timeout",
            "support_packet_json": json.dumps(support_packet, ensure_ascii=False, separators=(",", ":")),
            "retry_dispatch_json": json.dumps(production_payload, ensure_ascii=False, separators=(",", ":")),
        },
    }
    _dispatch(repository, SUPERVISOR_WORKFLOW, token, payload)
    return supervisor_id


def run_controller() -> dict[str, Any]:
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    repository = (os.getenv("GITHUB_REPOSITORY") or "a15280020511/test2").strip()
    controller_run_id = str(os.getenv("GITHUB_RUN_ID") or "controller")
    if not token:
        raise RuntimeError("GitHub token is required for operation control")

    raw, comment_id = _event_request()
    request = _normalize_request(raw, comment_id)
    operation_id = request["operation_id"]
    output_dir = Path("artifacts") / operation_id
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = _fetch_runtime_state(operation_id)
    existing_status = str(existing.get("status") or "")

    if existing_status in ACTIVE_STATES or existing_status in TERMINAL_STATES:
        result = {
            "schema_version": "1",
            "operation_id": operation_id,
            "controller_action": "idempotent_no_dispatch",
            "existing_state": existing,
            "reason": "operation_id_already_has_authoritative_state",
        }
        write_json(output_dir / "controller_result.json", result)
        return result
    if existing_status == "BUSY" and not request["resubmit_busy"]:
        result = {
            "schema_version": "1",
            "operation_id": operation_id,
            "controller_action": "busy_not_resubmitted",
            "existing_state": existing,
            "reason": "set resubmit_busy=true only after the prior lock owner finishes",
        }
        write_json(output_dir / "controller_result.json", result)
        return result

    publish_status(
        operation_id,
        request["operation"],
        "accepted",
        receipt_comment_id=request["receipt_comment_id"],
        active_step="controller_dispatch",
        detail=request["task_label"],
        current_policy="never",
    )
    payload = _production_payload(request)
    _dispatch(repository, PRODUCTION_WORKFLOW, token, payload)

    last_state: dict[str, Any] = {}
    started = False
    for _ in range(7):
        time.sleep(15)
        last_state = _fetch_runtime_state(operation_id)
        state = str(last_state.get("status") or "")
        if state == "BUSY" or state in ACTIVE_STATES or state in TERMINAL_STATES:
            started = True
            break

    supervisor_id: str | None = None
    if not started:
        supervisor_id = _dispatch_startup_supervisor(
            repository=repository,
            token=token,
            request=request,
            controller_run_id=controller_run_id,
            production_payload=payload,
            last_state=last_state,
        )

    result = {
        "schema_version": "1",
        "operation_id": operation_id,
        "receipt_comment_id": request["receipt_comment_id"],
        "controller_run_id": controller_run_id,
        "controller_action": "production_dispatched",
        "production_dispatch_accepted": True,
        "worker_state_observed": started,
        "last_operation_state": last_state,
        "startup_supervisor_operation_id": supervisor_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(output_dir / "controller_result.json", result)
    return result


def main() -> None:
    try:
        result = run_controller()
    except Exception as exc:
        result = {
            "schema_version": "1",
            "controller_action": "failure",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        Path("artifacts/controller").mkdir(parents=True, exist_ok=True)
        write_json(Path("artifacts/controller/controller_result.json"), result)
        print(json.dumps(result, ensure_ascii=False))
        raise
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
