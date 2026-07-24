from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from expert_team.budget import DEFAULT_TASK_BUDGET_USD, preflight_execution_plan
from expert_team.dynamic_team import validate_execution_plan
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
    return [
        run
        for run in runs
        if isinstance(run, dict) and original_operation_id in str(run.get("display_title") or "")
    ]


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _task_budget(plan: dict[str, Any]) -> float:
    budget = plan.get("budget")
    if not isinstance(budget, dict):
        return DEFAULT_TASK_BUDGET_USD
    return float(budget.get("max_total_usd", DEFAULT_TASK_BUDGET_USD))


def _git_show_runtime_json(path: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "show", f"origin/runtime-results:{path}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        return {}
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _published_recovery_evidence(operation_id: str) -> dict[str, Any]:
    base = f"runtime_results/{operation_id}"
    managed = _git_show_runtime_json(f"{base}/managed_operation.json")
    effective = _git_show_runtime_json(f"{base}/effective_execution_plan.json")
    provenance = effective.get("provenance") if isinstance(effective.get("provenance"), dict) else {}
    attempts = int(managed.get("attempts") or 0) if str(managed.get("attempts") or "0").isdigit() else 0
    source = str(provenance.get("effective_plan_source") or "")
    consumed = attempts >= 2 or source == "deepseek_top_supervisor"
    return {
        "consumed": consumed,
        "managed_attempts": attempts,
        "effective_plan_source": source or None,
        "reason": (
            "internal_whole_operation_retry_already_used"
            if attempts >= 2
            else "top_supervisor_recovery_run_already_used"
            if source == "deepseek_top_supervisor"
            else "recovery_budget_not_yet_used"
        ),
    }


def _prepare_retry_payload(
    retry_payload: dict[str, Any],
    steward_result: dict[str, Any],
    *,
    supervisor_operation_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    inputs = retry_payload.get("inputs")
    if not isinstance(inputs, dict):
        raise RuntimeError("retry dispatch payload has no inputs object")
    operation = str(inputs.get("operation") or "")
    overrides = steward_result.get("retry_operation_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    applied: dict[str, Any] = {}

    if operation == "execute_team":
        original_plan_raw = str(inputs.get("plan_json") or "{}")
        original_plan = _load_object(original_plan_raw)
        plan_value = overrides.get("plan_json")
        if isinstance(plan_value, dict):
            effective_plan = dict(plan_value)
            changed = True
        elif isinstance(plan_value, str) and plan_value.strip():
            effective_plan = _load_object(plan_value)
            changed = True
        elif plan_value in (None, ""):
            effective_plan = json.loads(json.dumps(original_plan))
            changed = False
        else:
            raise RuntimeError("DeepSeek retry_operation_overrides.plan_json must be a JSON string or object")

        original_task = str(original_plan.get("task") or "").strip()
        effective_task = str(effective_plan.get("task") or "").strip()
        if original_task and effective_task != original_task:
            raise RuntimeError("DeepSeek replacement plan changed the user's substantive task")

        original_budget = _task_budget(original_plan)
        effective_budget = _task_budget(effective_plan)
        if effective_budget > original_budget + 1e-9:
            raise RuntimeError("DeepSeek replacement plan may not increase the user's logical-task budget")
        effective_plan.setdefault(
            "budget",
            original_plan.get("budget")
            if isinstance(original_plan.get("budget"), dict)
            else {"max_total_usd": original_budget, "recovery_reserve_ratio": 0.30},
        )
        original_provenance = original_plan.get("provenance")
        original_source = (
            str(original_provenance.get("original_plan_source") or "web_gpt")
            if isinstance(original_provenance, dict)
            else "web_gpt"
        )
        effective_plan["provenance"] = {
            "original_plan_source": original_source,
            "effective_plan_source": "deepseek_top_supervisor",
            "original_plan_sha256": _sha256_json(original_plan),
            "supervisor_operation_id": supervisor_operation_id,
            "override_reason": str(steward_result.get("diagnosis") or "technical recovery"),
        }

        validate_execution_plan(effective_plan)
        preflight = preflight_execution_plan(effective_plan, execution_phase="recovery")
        write_json(output_dir / "replacement_plan_cost_preflight.json", preflight)
        inputs["plan_json"] = json.dumps(effective_plan, ensure_ascii=False, separators=(",", ":"))
        applied.update(
            {
                "plan_json": (
                    "replaced_validated_recovery_budget_compliant"
                    if changed
                    else "unchanged_validated_recovery_budget_compliant"
                ),
                "original_plan_sha256": _sha256_json(original_plan),
                "effective_plan_sha256": _sha256_json(effective_plan),
                "recovery_preflight_usd": preflight.get("estimated_worst_case_usd"),
                "available_recovery_budget_usd": preflight.get("available_execution_budget_usd"),
            }
        )

    if "ranking_limit" in overrides:
        ranking_value = str(overrides.get("ranking_limit") or "").strip()
        if not ranking_value.isdigit() or int(ranking_value) < 1:
            raise RuntimeError("DeepSeek retry_operation_overrides.ranking_limit must be a positive integer")
        inputs["ranking_limit"] = ranking_value
        applied["ranking_limit"] = ranking_value

    return applied


def _write_plan_and_result(output_dir: Path, result_path: Path, steward_result: dict, plan: dict) -> None:
    write_json(output_dir / "supervisor_resume.json", plan)
    steward_result["supervisor_resume"] = plan
    write_json(result_path, steward_result)


def _plan_resume(
    *, supervisor_operation_id: str, original_operation_id: str, failed_run_id: str,
    retry_dispatch_json: str, repository: str, token: str,
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
        "applied_retry_overrides": {},
        "recovery_budget_evidence": _published_recovery_evidence(original_operation_id),
        "dispatch_status": "not_attempted",
    }

    if resume != "READY":
        _write_plan_and_result(output_dir, result_path, steward_result, plan)
        return plan

    retry_payload = _load_object(retry_dispatch_json)
    if not retry_payload:
        plan["reason"] = "no_retry_dispatch_payload"
        _write_plan_and_result(output_dir, result_path, steward_result, plan)
        return plan

    plan["applied_retry_overrides"] = _prepare_retry_payload(
        retry_payload,
        steward_result,
        supervisor_operation_id=supervisor_operation_id,
        output_dir=output_dir,
    )

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
        run for run in runs
        if str(run.get("status") or "") in ACTIVE_STATUSES
        and str(run.get("id") or "") != failed_run_id
    ]

    if active_runs:
        plan["reason"] = "matching_run_already_active"
    elif any(str(run.get("conclusion") or "") == "success" for run in runs):
        plan["reason"] = "matching_run_already_succeeded"
    elif plan["recovery_budget_evidence"].get("consumed") is True:
        plan["reason"] = "logical_task_recovery_budget_already_consumed"
    else:
        plan["action"] = "dispatch"
        plan["reason"] = "validated_recovery_budget_available"
        plan["retry_dispatch_payload"] = retry_payload

    _write_plan_and_result(output_dir, result_path, steward_result, plan)
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
            result_path = output_dir / "deepseek_steward_result.json"
            steward_result = read_json(result_path)
            steward_result["supervisor_resume"] = plan
            write_json(result_path, steward_result)
            return plan
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 3:
                time.sleep(2**attempt)

    plan["dispatch_status"] = "failure"
    plan["dispatch_error"] = last_error
    write_json(plan_path, plan)
    result_path = output_dir / "deepseek_steward_result.json"
    steward_result = read_json(result_path)
    steward_result["supervisor_resume"] = plan
    write_json(result_path, steward_result)
    raise RuntimeError(f"Supervisor could not redispatch original operation: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan or execute one bounded original-operation resume")
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
