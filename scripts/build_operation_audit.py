from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.repair_utils import safe_operation_id, write_json

WORKFLOWS = ("expert-team-production.yml", "deepseek-supervisor.yml", "cancel-operation.yml")
LOCAL_FILES = (
    "metadata.json",
    "managed_operation.json",
    "cost_preflight.json",
    "effective_execution_plan.json",
    "model_calls.json",
    "execution_trace.json",
    "partial_execution.json",
    "auto_repair_result.json",
    "deepseek_steward_result.json",
    "supervisor_resume.json",
    "single_task_lock.json",
    "cancellation_result.json",
)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"read_error": f"{type(exc).__name__}: {exc}"}


def _request_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
        return value if isinstance(value, dict) else {}


def _github_runs(operation_id: str, repository: str, token: str) -> tuple[list[dict[str, Any]], list[str]]:
    runs: list[dict[str, Any]] = []
    errors: list[str] = []
    for workflow in WORKFLOWS:
        try:
            payload = _request_json(
                f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?per_page=100",
                token,
            )
            for run in payload.get("workflow_runs", []):
                if not isinstance(run, dict):
                    continue
                title = str(run.get("display_title") or "")
                if operation_id not in title:
                    continue
                runs.append(
                    {
                        "workflow": workflow,
                        "run_id": run.get("id"),
                        "display_title": title,
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "created_at": run.get("created_at"),
                        "run_started_at": run.get("run_started_at"),
                        "updated_at": run.get("updated_at"),
                        "html_url": run.get("html_url"),
                        "head_sha": run.get("head_sha"),
                    }
                )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"{workflow}: {type(exc).__name__}: {exc}")
    runs.sort(key=lambda item: str(item.get("created_at") or ""))
    return runs, errors


def build_audit(operation_id: str) -> dict[str, Any]:
    safe_id = safe_operation_id(operation_id)
    output_dir = Path("artifacts") / safe_id
    local = {name: _read_json(output_dir / name) for name in LOCAL_FILES if (output_dir / name).exists()}
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    repository = (os.getenv("GITHUB_REPOSITORY") or "a15280020511/test2").strip()
    runs: list[dict[str, Any]] = []
    collection_errors: list[str] = []
    if token:
        runs, collection_errors = _github_runs(safe_id, repository, token)
    else:
        collection_errors.append("GitHub token unavailable; server-side Run timeline not collected")

    model_calls = local.get("model_calls.json")
    successful_calls = 0
    failed_calls = 0
    if isinstance(model_calls, list):
        successful_calls = sum(1 for item in model_calls if isinstance(item, dict) and item.get("status") == "success")
        failed_calls = sum(1 for item in model_calls if isinstance(item, dict) and item.get("status") == "failure")

    return {
        "schema_version": "1",
        "operation_id": safe_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": repository,
        "logical_task_budget": local.get("cost_preflight.json"),
        "model_call_summary": {
            "successful": successful_calls,
            "failed": failed_calls,
            "usage_note": "Token counts in model_calls are conservative text estimates unless provider usage is explicitly available.",
        },
        "runs": runs,
        "run_collection_errors": collection_errors,
        "local_records": local,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build one authoritative operation audit JSON")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    result = build_audit(args.operation_id)
    output = Path("artifacts") / safe_operation_id(args.operation_id) / "operation_audit.json"
    write_json(output, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
