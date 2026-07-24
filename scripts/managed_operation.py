from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.publish_operation_status import publish_status
from scripts.repair_utils import (
    ensure_safe_repair_changes,
    read_json,
    run_verification,
    safe_operation_id,
    write_json,
)

PLUGIN_OPERATIONS = {"model_intelligence", "execute_team"}


def _entrypoint_arguments(
    *,
    operation: str,
    operation_id: str,
    plan_json: str,
    ranking_limit: int,
    steward_mode: str,
    support_packet_json: str,
) -> list[str]:
    return [
        "--operation",
        operation,
        "--operation-id",
        operation_id,
        "--plan-json",
        plan_json,
        "--ranking-limit",
        str(ranking_limit),
        "--steward-mode",
        steward_mode,
        "--support-packet-json",
        support_packet_json,
    ]


def _entrypoint_command(
    *,
    operation: str,
    operation_id: str,
    plan_json: str,
    ranking_limit: int,
    steward_mode: str,
    support_packet_json: str,
) -> list[str]:
    arguments = _entrypoint_arguments(
        operation=operation,
        operation_id=operation_id,
        plan_json=plan_json,
        ranking_limit=ranking_limit,
        steward_mode=steward_mode,
        support_packet_json=support_packet_json,
    )
    if operation in PLUGIN_OPERATIONS:
        return [
            sys.executable,
            "-m",
            "scripts.plugin_runner",
            "--plugin",
            "expert-team",
            "--operation",
            operation,
            "--operation-id",
            operation_id,
            "--module",
            "scripts.action_entrypoint",
            "--",
            *arguments,
        ]
    return [sys.executable, "-m", "scripts.action_entrypoint", *arguments]


def _run(command: list[str], log_path: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, check=False, text=True, capture_output=True, env=os.environ.copy())
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"returncode={completed.returncode}\n\nSTDOUT:\n{completed.stdout}\n\nSTDERR:\n{completed.stderr}",
        encoding="utf-8",
    )
    return completed


def _load_json_object(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"raw_support_packet": value}
    return parsed if isinstance(parsed, dict) else {"raw_support_packet": value}


def _publish_transition(operation_id: str, operation: str, phase: str, output_dir: Path) -> None:
    """Best-effort progress publication; terminal publication remains a workflow hard step."""
    try:
        publish_status(operation_id, operation, phase)
    except Exception as exc:
        log_path = output_dir / "status_transition_errors.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"phase={phase} error={type(exc).__name__}: {exc}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Managed production operation with one automatic DeepSeek repair cycle")
    parser.add_argument("--operation", required=True, choices=("model_intelligence", "execute_team", "deepseek_steward"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--plan-json", default="{}")
    parser.add_argument("--ranking-limit", type=int, default=20)
    parser.add_argument("--steward-mode", choices=("ASSIST", "REVIEW", "REPAIR"), default="ASSIST")
    parser.add_argument("--support-packet-json", default="{}")
    args = parser.parse_args()

    operation_id = safe_operation_id(args.operation_id)
    output_dir = Path("artifacts") / operation_id
    output_dir.mkdir(parents=True, exist_ok=True)

    original_command = _entrypoint_command(
        operation=args.operation,
        operation_id=operation_id,
        plan_json=args.plan_json,
        ranking_limit=args.ranking_limit,
        steward_mode=args.steward_mode,
        support_packet_json=args.support_packet_json,
    )

    # Explicit Steward operations are the independent service operation themselves.
    # Do not recursively invoke Steward when Steward fails; DeepSeek unavailability is a hard stop.
    if args.operation == "deepseek_steward":
        completed = _run(original_command, output_dir / "managed_operation.log")
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)
        write_json(
            output_dir / "managed_operation.json",
            {"status": "success", "operation": args.operation, "attempts": 1, "auto_repair_triggered": False},
        )
        return

    first = _run(original_command, output_dir / "initial_operation.log")
    if first.returncode == 0:
        write_json(
            output_dir / "managed_operation.json",
            {
                "status": "success",
                "operation": args.operation,
                "attempts": 1,
                "auto_repair_triggered": False,
                "plugin": "expert-team" if args.operation in PLUGIN_OPERATIONS else None,
                "plugin_cleanup": "always",
            },
        )
        return

    # A GitHub-internal operation failure automatically becomes a DeepSeek REPAIR request.
    # This remains deliberately limited to one repair cycle and one retry.
    _publish_transition(operation_id, args.operation, "repairing", output_dir)
    repair_id = safe_operation_id(f"{operation_id}-auto-repair")
    supplied_packet = _load_json_object(args.support_packet_json)
    support_packet = {
        "operation_id": operation_id,
        "mode": "REPAIR",
        "request": "Automatically diagnose and repair the repository integration defect that caused the production operation to fail, then allow one retry.",
        "task": supplied_packet.get("task"),
        "current_state": "Original GitHub production operation failed before completion.",
        "failure_location": f"managed production operation: {args.operation}",
        "error_type": "ProductionOperationFailure",
        "error_message": (first.stderr or first.stdout or "unknown failure")[-12000:],
        "logs_excerpt": {"stdout": first.stdout[-12000:], "stderr": first.stderr[-12000:]},
        "attempts_already_made": ["Initial operation attempt failed. No manual repository repair was attempted."],
        "constraints": [
            "Use DeepSeek official API only for Steward.",
            "Never fall back to OpenRouter for Steward.",
            "Use the smallest evidence-based repair.",
            "Do not maintain or fork an upstream plugin package.",
            "Repair only the local manifest, adapter, workflow, or compatibility boundary.",
            "Only one automatic repair cycle and one retry are allowed.",
        ],
        "requested_outcome": "Repair the repository integration defect, pass verification, and make the original operation succeed on one retry.",
        "original_support_packet": supplied_packet,
    }

    repair_command = _entrypoint_command(
        operation="deepseek_steward",
        operation_id=repair_id,
        plan_json="{}",
        ranking_limit=args.ranking_limit,
        steward_mode="REPAIR",
        support_packet_json=json.dumps(support_packet, ensure_ascii=False),
    )
    repair = _run(repair_command, output_dir / "auto_repair_steward.log")
    if repair.returncode != 0:
        write_json(
            output_dir / "managed_operation.json",
            {
                "status": "STOP",
                "operation": args.operation,
                "attempts": 1,
                "auto_repair_triggered": True,
                "reason": "DeepSeek Steward failed or was unavailable. Hard stop; no provider fallback.",
            },
        )
        raise RuntimeError("Automatic repair hard-stopped because DeepSeek Steward failed or was unavailable")

    repair_result_path = Path("artifacts") / repair_id / "deepseek_steward_result.json"
    if not repair_result_path.exists():
        raise RuntimeError("DeepSeek Steward completed without a readable repair result")

    repair_result = read_json(repair_result_path)
    auto_repair_result_path = output_dir / "auto_repair_result.json"
    shutil.copy2(repair_result_path, auto_repair_result_path)
    decision = str(repair_result.get("decision") or "STOP").upper()
    if decision != "EDIT":
        write_json(
            output_dir / "managed_operation.json",
            {
                "status": "STOP",
                "operation": args.operation,
                "attempts": 1,
                "auto_repair_triggered": True,
                "repair_decision": decision,
                "reason": "DeepSeek Steward did not authorize a repository edit.",
            },
        )
        raise RuntimeError(f"Automatic repair stopped: DeepSeek Steward decision={decision}")

    changed = ensure_safe_repair_changes()
    run_verification()

    _publish_transition(operation_id, args.operation, "retrying", output_dir)
    second = _run(original_command, output_dir / "retry_operation.log")
    if second.returncode != 0:
        result = read_json(auto_repair_result_path)
        result["verification"] = "passed_before_retry"
        result["resume"] = "STOP"
        result["retry"] = {"status": "failure", "stderr": second.stderr[-8000:]}
        write_json(auto_repair_result_path, result)
        write_json(
            output_dir / "managed_operation.json",
            {
                "status": "STOP",
                "operation": args.operation,
                "attempts": 2,
                "auto_repair_triggered": True,
                "repair_decision": decision,
                "changed_files": changed,
                "reason": "The single allowed retry failed after verified repair.",
            },
        )
        raise RuntimeError("Original operation failed on the single allowed retry after automatic repair")

    result = read_json(auto_repair_result_path)
    result["verification"] = "passed"
    result["resume"] = "STOP"
    result["retry"] = {"status": "success", "attempt": 2}
    result["repair_delivery"] = {
        "status": "pending_delivery",
        "verification": "passed",
        "method": None,
        "pull_request_url": None,
    }
    write_json(auto_repair_result_path, result)
    write_json(
        output_dir / "auto_repair_manifest.json",
        {
            "operation_id": operation_id,
            "repair_operation_id": repair_id,
            "original_operation": args.operation,
            "status": "verified_retry_success_pending_delivery",
            "changed_files": changed,
            "attempts": 2,
        },
    )
    write_json(
        output_dir / "managed_operation.json",
        {
            "status": "success",
            "operation": args.operation,
            "attempts": 2,
            "auto_repair_triggered": True,
            "repair_decision": decision,
            "changed_files": changed,
            "retry": "success",
            "delivery": "pending",
        },
    )


if __name__ == "__main__":
    main()
