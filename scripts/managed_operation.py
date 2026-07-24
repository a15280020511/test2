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


def _entrypoint_command(
    *, operation: str, operation_id: str, plan_json: str, ranking_limit: int,
    steward_mode: str, support_packet_json: str,
) -> list[str]:
    return [
        sys.executable, "-m", "scripts.action_entrypoint",
        "--operation", operation,
        "--operation-id", operation_id,
        "--plan-json", plan_json,
        "--ranking-limit", str(ranking_limit),
        "--steward-mode", steward_mode,
        "--support-packet-json", support_packet_json,
    ]


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
    try:
        publish_status(
            operation_id,
            operation,
            phase,
            active_step="managed_operation",
            current_policy="if-owner",
        )
    except Exception as exc:
        with (output_dir / "status_transition_errors.log").open("a", encoding="utf-8") as handle:
            handle.write(f"phase={phase} error={type(exc).__name__}: {exc}\n")


def _safe_unchanged_retry(result: dict) -> bool:
    if str(result.get("resume") or "STOP").upper() != "READY":
        return False
    text = " ".join(str(result.get(field) or "") for field in ("diagnosis", "message_to_web_gpt")).lower()
    if any(marker in text for marker in ("402", "credit", "budget", "afford", "insufficient")):
        return False
    return any(
        marker in text
        for marker in (
            "transient", "temporary", "network", "rate limit", "429", "502", "503",
            "jsondecodeerror", "could not be parsed", "invalid json",
        )
    )


def _recovery_reserve_allows_whole_retry(output_dir: Path, operation: str) -> tuple[bool, dict]:
    if operation != "execute_team":
        return True, {"reason": "non_expert_operation"}
    path = output_dir / "cost_preflight.json"
    if not path.exists():
        return False, {"reason": "missing_cost_preflight"}
    preflight = read_json(path)
    try:
        single_pass = float(preflight.get("single_clean_pass_estimated_usd"))
        reserve = float(preflight.get("reserved_recovery_budget_usd"))
    except (TypeError, ValueError):
        return False, {"reason": "invalid_cost_preflight"}
    return single_pass <= reserve, {
        "single_clean_pass_estimated_usd": single_pass,
        "reserved_recovery_budget_usd": reserve,
        "reason": "within_recovery_reserve" if single_pass <= reserve else "whole_retry_exceeds_recovery_reserve",
    }


def _record_retry_failure(
    *, output_dir: Path, auto_repair_result_path: Path, operation: str,
    decision: str, second: subprocess.CompletedProcess[str], changed_files: list[str], verification: str,
) -> None:
    result = read_json(auto_repair_result_path)
    result["verification"] = verification
    result["resume"] = "STOP"
    result["retry"] = {"status": "failure", "stderr": second.stderr[-8000:]}
    write_json(auto_repair_result_path, result)
    write_json(
        output_dir / "managed_operation.json",
        {
            "status": "STOP", "operation": operation, "attempts": 2,
            "auto_repair_triggered": True, "repair_decision": decision,
            "changed_files": changed_files, "reason": "The single allowed retry failed.",
        },
    )


def _stop_for_reserve(
    output_dir: Path,
    auto_repair_result_path: Path,
    operation: str,
    decision: str,
    reserve_evidence: dict,
    changed_files: list[str],
) -> None:
    result = read_json(auto_repair_result_path)
    result["resume"] = "STOP"
    result["retry"] = {"status": "not_attempted", "reason": "recovery_reserve_insufficient", **reserve_evidence}
    write_json(auto_repair_result_path, result)
    write_json(
        output_dir / "managed_operation.json",
        {
            "status": "STOP",
            "operation": operation,
            "attempts": 1,
            "auto_repair_triggered": True,
            "repair_decision": decision,
            "changed_files": changed_files,
            "reason": "A whole-operation retry would exceed the reserved recovery budget; top supervisor must replan.",
            "budget_evidence": reserve_evidence,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Managed operation with one DeepSeek diagnosis and one budgeted retry")
    parser.add_argument("--operation", required=True, choices=("model_intelligence", "execute_team", "deepseek_steward"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--plan-json", default="{}")
    parser.add_argument("--ranking-limit", type=int, default=20)
    parser.add_argument("--steward-mode", choices=("ASSIST", "REPAIR"), default="ASSIST")
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
            {"status": "success", "operation": args.operation, "attempts": 1, "auto_repair_triggered": False},
        )
        return

    _publish_transition(operation_id, args.operation, "repairing", output_dir)
    repair_id = safe_operation_id(f"{operation_id}-auto-repair")
    supplied_packet = _load_json_object(args.support_packet_json)
    support_packet = {
        "operation_id": operation_id,
        "mode": "REPAIR",
        "request": "Diagnose the failed operation. Authorize one unchanged retry only for a transient provider failure and only when the reserved recovery budget covers a clean pass.",
        "task": supplied_packet.get("task"),
        "current_state": "Original GitHub production operation failed before completion.",
        "failure_location": f"managed production operation: {args.operation}",
        "error_type": "ProductionOperationFailure",
        "error_message": (first.stderr or first.stdout or "unknown failure")[-12000:],
        "logs_excerpt": {"stdout": first.stdout[-12000:], "stderr": first.stderr[-12000:]},
        "attempts_already_made": ["Initial operation attempt failed."],
        "constraints": [
            "Use DeepSeek official API only for Steward.",
            "Never fall back to OpenRouter for Steward.",
            "One diagnosis and one retry maximum.",
            "Do not retry an unchanged 402, affordability, or budget failure.",
            "A whole-operation retry must fit the reserved recovery budget.",
            "Execution-plan changes belong to DeepSeek Top Supervisor and require schema and budget validation.",
        ],
        "requested_outcome": "Choose EDIT for a repository defect, NO_EDIT+READY for a safe transient retry, or STOP for top-supervisor escalation.",
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
                "status": "STOP", "operation": args.operation, "attempts": 1,
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
    reserve_allowed, reserve_evidence = _recovery_reserve_allows_whole_retry(output_dir, args.operation)

    if decision == "NO_EDIT" and _safe_unchanged_retry(repair_result):
        if not reserve_allowed:
            _stop_for_reserve(
                output_dir, auto_repair_result_path, args.operation, decision, reserve_evidence, []
            )
            raise RuntimeError("Transient whole-operation retry blocked by the logical-task recovery budget")
        _publish_transition(operation_id, args.operation, "retrying", output_dir)
        second = _run(original_command, output_dir / "retry_operation.log")
        if second.returncode != 0:
            _record_retry_failure(
                output_dir=output_dir,
                auto_repair_result_path=auto_repair_result_path,
                operation=args.operation,
                decision=decision,
                second=second,
                changed_files=[],
                verification="not_required_for_transient_retry",
            )
            raise RuntimeError("Original operation failed on the single transient retry")
        result = read_json(auto_repair_result_path)
        result["verification"] = "not_required_for_transient_retry"
        result["resume"] = "READY"
        result["retry"] = {
            "status": "success", "attempt": 2, "type": "unchanged_transient_retry", **reserve_evidence
        }
        write_json(auto_repair_result_path, result)
        write_json(
            output_dir / "managed_operation.json",
            {
                "status": "success", "operation": args.operation, "attempts": 2,
                "auto_repair_triggered": True, "repair_decision": decision,
                "changed_files": [], "retry": "success", "delivery": "not_required",
                "budget_evidence": reserve_evidence,
            },
        )
        return

    if decision != "EDIT":
        write_json(
            output_dir / "managed_operation.json",
            {
                "status": "STOP", "operation": args.operation, "attempts": 1,
                "auto_repair_triggered": True, "repair_decision": decision,
                "reason": "DeepSeek did not authorize a repository edit or safe budgeted transient retry; top supervisor must decide any replan.",
            },
        )
        raise RuntimeError(f"Automatic repair stopped for top-supervisor escalation: DeepSeek decision={decision}")

    changed = ensure_safe_repair_changes()
    run_verification()
    if not reserve_allowed:
        _stop_for_reserve(
            output_dir, auto_repair_result_path, args.operation, decision, reserve_evidence, changed
        )
        raise RuntimeError("Verified code repair could not retry because the recovery reserve is insufficient")

    _publish_transition(operation_id, args.operation, "retrying", output_dir)
    second = _run(original_command, output_dir / "retry_operation.log")
    if second.returncode != 0:
        _record_retry_failure(
            output_dir=output_dir,
            auto_repair_result_path=auto_repair_result_path,
            operation=args.operation,
            decision=decision,
            second=second,
            changed_files=changed,
            verification="passed_before_retry",
        )
        raise RuntimeError("Original operation failed on the single allowed retry after verified repair")

    result = read_json(auto_repair_result_path)
    result["verification"] = "passed"
    result["resume"] = "STOP"
    result["retry"] = {
        "status": "success", "attempt": 2, "type": "verified_repository_edit", **reserve_evidence
    }
    result["repair_delivery"] = {
        "status": "pending_delivery", "verification": "passed", "method": None, "pull_request_url": None,
    }
    write_json(auto_repair_result_path, result)
    write_json(
        output_dir / "auto_repair_manifest.json",
        {
            "operation_id": operation_id, "repair_operation_id": repair_id,
            "original_operation": args.operation, "status": "verified_retry_success_pending_delivery",
            "changed_files": changed, "attempts": 2,
        },
    )
    write_json(
        output_dir / "managed_operation.json",
        {
            "status": "success", "operation": args.operation, "attempts": 2,
            "auto_repair_triggered": True, "repair_decision": decision,
            "changed_files": changed, "retry": "success", "delivery": "pending",
            "budget_evidence": reserve_evidence,
        },
    )


if __name__ == "__main__":
    main()
