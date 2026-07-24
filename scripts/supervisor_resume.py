from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from scripts.repair_utils import read_json, safe_operation_id, write_json

WORKFLOW_FILE = "expert-team-production.yml"
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


def _load_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"retry_dispatch_json is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError("retry_dispatch_json must be a JSON object")
    return value


def _matching_runs(repository: str, token: str, original_operation_id: str) -> list[dict[str, Any]]:
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs"
        "?event=workflow_dispatch&per_page=100"
    )
    payload = _request_json("GET", url, token)
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        return []
    matched: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        display_title = str(run.get("display_title") or "")
        if original_operation_id in display_title:
            matched.append(run)
    return matched


def _plan_resume(
    *,
    supervisor_operation_id: str,
    original_operation_id: str,
    failed_run_id: str,
    retry_dispatch_json: str,
    repository: str,
    token: str,
) -> dict[str, Any]:
    output_dir = Path("artifacts") / supervisor_operation_id
    result_path = output_dir / "deepseek_steward_result.json"
    if not result_path.exists():
        raise RuntimeError("DeepSeek supervisor result is missing")
    steward_result = read_json(result_path)
    resume = str(steward_result.get("resume") or "STOP").upper()

    plan: dict[str, Any] = {
        "original_operation_id": original_operation_id,
        "known_failed_run_id": failed_run_id or None,
        "steward_resume": resume,
        "action": "none",
        "reason": "steward_not_ready",
        "matching_runs": [],
        "dispatch_status": "not_attempted",
    }

    if resume != "READY":
        write_json(output_dir / "supervisor_resume.json", plan)
        steward_result["supervisor_resume"] = plan
        write_json(result_path, steward_result)
        return plan

    retry_payload = _load_object(retry_dispatch_json)
    if not retry_payload:
        plan["reason"] = "no_retry_dispatch_payload"
        write_json(output_dir / "supervisor_resume.json", plan)
        steward_result["supervisor_resume"] = plan
        write_json(result_path, steward_result)
        return plan

    runs = _matching_runs(repository, token, original_operation_id)
    plan["matching_runs"] = [
        {
            "id": run.get("id"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "display_title": run.get("display_title"),
        }
        for run in runs
    ]

    active_runs = [
        run
        for run in runs
        if str(run.get("status") or "") in ACTIVE_STATUSES
        and str(run.get("id") or "") != failed_run_id
    ]

    if active_runs:
        plan["reason"] = "matching_run_already_active"
    elif any(str(run.get("conclusion") or "") == "success" for run in runs):
        plan["reason"] = "matching_run_already_succeeded"
    elif len(runs) >= 2:
        plan["reason"] = "bounded_retry_limit_reached"
    else:
        plan["action"] = "dispatch"
        plan["reason"] = "no_active_matching_run_after_supervisor_ready"
        plan["retry_dispatch_payload"] = retry_payload

    write_json(output_dir / "supervisor_resume.json", plan)
    steward_result["supervisor_resume"] = plan
    write_json(result_path, steward_result)
    return plan


def _execute_resume(*, supervisor_operation_id: str, repository: str, token: str) -> dict[str, Any]:
    output_dir = Path("artifacts") / supervisor_operation_id
    plan_path = output_dir / "supervisor_resume.json"
    if not plan_path.exists():
        raise RuntimeError("supervisor_resume.json is missing")
    plan = read_json(plan_path)
    if plan.get("action") != "dispatch":
        return plan

    payload = plan.get("retry_dispatch_payload")
    if not isinstance(payload, dict):
        raise RuntimeError("planned retry dispatch payload is missing")

    url = f"https://api.github.com/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    last_error = ""
    for attempt in range(1, 4):
        try:
            _request_json("POST", url, token, payload)
            plan["dispatch_status"] = "accepted"
            plan["dispatch_attempt"] = attempt
            write_json(plan_path, plan)
            return plan
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 3:
                time.sleep(2**attempt)

    plan["dispatch_status"] = "failure"
    plan["dispatch_error"] = last_error
    write_json(plan_path, plan)
    raise RuntimeError(f"Supervisor could not redispatch original operation: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan or execute one bounded original-operation resume after DeepSeek supervision")
    parser.add_argument("--mode", choices=("plan", "execute"), required=True)
    parser.add_argument("--supervisor-operation-id", required=True)
    parser.add_argument("--original-operation-id", default="")
    parser.add_argument("--failed-run-id", default="")
    parser.add_argument("--retry-dispatch-json", default="{}")
    args = parser.parse_args()

    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    repository = (os.getenv("GITHUB_REPOSITORY") or "a15280020511/test2").strip()
    if not token:
        raise RuntimeError("GitHub token is required for supervisor run inspection and resume")

    supervisor_operation_id = safe_operation_id(args.supervisor_operation_id)
    original_operation_id = safe_operation_id(args.original_operation_id) if args.original_operation_id else ""

    if args.mode == "plan":
        if not original_operation_id:
            raise ValueError("original_operation_id is required in plan mode")
        result = _plan_resume(
            supervisor_operation_id=supervisor_operation_id,
            original_operation_id=original_operation_id,
            failed_run_id=args.failed_run_id.strip(),
            retry_dispatch_json=args.retry_dispatch_json,
            repository=repository,
            token=token,
        )
    else:
        result = _execute_resume(
            supervisor_operation_id=supervisor_operation_id,
            repository=repository,
            token=token,
        )

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
