from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}")
BUDGET_KEYS = (
    "approval_status",
    "tier",
    "currency",
    "max_cost_usd",
    "estimated_cost_usd",
    "max_model_calls",
    "max_output_tokens_per_call",
    "approval_reference",
)


class EntryVerificationError(RuntimeError):
    pass


def _load_object(value: str, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EntryVerificationError(f"{field} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EntryVerificationError(f"{field} must be an object")
    return payload


def _read_runtime_result(operation_id: str) -> dict[str, Any]:
    if not SAFE_ID.fullmatch(operation_id):
        raise EntryVerificationError("deepseek entry operation_id is unsafe")
    path = f"runtime_results/{operation_id}/deepseek_steward_result.json"
    completed = subprocess.run(
        ["git", "show", f"origin/runtime-results:{path}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise EntryVerificationError(
            f"no durable DeepSeek ASSIST result for operation_id={operation_id}: {completed.stderr[-500:]}"
        )
    return _load_object(completed.stdout, "durable DeepSeek result")


def _normalize_budget(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EntryVerificationError(f"{field} must be an object")
    missing = [key for key in BUDGET_KEYS if key not in value]
    if missing:
        raise EntryVerificationError(f"{field} is missing fields: {missing}")
    return {key: value[key] for key in BUDGET_KEYS}


def _verify_receipt(
    *,
    plan: dict[str, Any],
    receipt: dict[str, Any],
    execution_operation_id: str,
) -> dict[str, Any]:
    if receipt.get("operation_id") != execution_operation_id:
        raise EntryVerificationError("budget receipt operation_id does not match the execution operation")
    if receipt.get("operation") != "execute_team":
        raise EntryVerificationError("budget receipt operation must be execute_team")

    entry = plan.get("deepseek_entry")
    if not isinstance(entry, dict):
        raise EntryVerificationError("plan requires deepseek_entry")
    if receipt.get("deepseek_assist_operation_id") != entry.get("operation_id"):
        raise EntryVerificationError("budget receipt references a different DeepSeek ASSIST operation")

    plan_budget = _normalize_budget(plan.get("budget"), "plan budget")
    receipt_budget = _normalize_budget(receipt.get("budget_approval"), "budget receipt approval")
    if receipt_budget != plan_budget:
        raise EntryVerificationError("budget receipt does not exactly match the submitted execution plan")
    if receipt_budget.get("approval_status") != "approved_by_user":
        raise EntryVerificationError("budget receipt does not contain user approval")

    source = str(receipt.get("source") or "").strip()
    if source != "web_gpt_after_user_budget_selection":
        raise EntryVerificationError("budget receipt source is invalid")
    return receipt_budget


def verify(
    plan: dict[str, Any],
    receipt: dict[str, Any],
    *,
    execution_operation_id: str,
    receipt_comment_id: str,
) -> dict[str, Any]:
    entry = plan.get("deepseek_entry")
    if not isinstance(entry, dict):
        raise EntryVerificationError("plan requires deepseek_entry")
    deepseek_operation_id = str(entry.get("operation_id") or "").strip()
    result = _read_runtime_result(deepseek_operation_id)
    if str(result.get("mode") or "").upper() != "ASSIST":
        raise EntryVerificationError("referenced DeepSeek result is not ASSIST mode")
    if str(result.get("status") or "").upper() != "READY":
        raise EntryVerificationError("referenced DeepSeek ASSIST result is not READY")
    options = result.get("budget_options")
    if not isinstance(options, list) or len(options) != 3:
        raise EntryVerificationError("DeepSeek ASSIST must provide exactly three budget options")
    tiers = {str(item.get("tier") or "").lower() for item in options if isinstance(item, dict)}
    if tiers != {"economy", "balanced", "quality"}:
        raise EntryVerificationError("DeepSeek budget options must be economy, balanced, and quality")
    if not str(result.get("budget_question_to_user") or "").strip():
        raise EntryVerificationError("DeepSeek ASSIST result lacks the budget question for Web GPT")
    if entry.get("budget_options_presented") is not True:
        raise EntryVerificationError("plan does not confirm that Web GPT presented budget options")

    approved_budget = _verify_receipt(
        plan=plan,
        receipt=receipt,
        execution_operation_id=execution_operation_id,
    )
    return {
        "schema_version": 2,
        "status": "VERIFIED",
        "deepseek_operation_id": deepseek_operation_id,
        "deepseek_mode": "ASSIST",
        "deepseek_status": "READY",
        "budget_tiers": ["economy", "balanced", "quality"],
        "budget_receipt_comment_id": receipt_comment_id,
        "approved_budget": approved_budget,
        "steward_model": result.get("steward_model"),
        "provider": result.get("steward_provider"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--receipt-body-file", type=Path, required=True)
    parser.add_argument("--receipt-comment-id", required=True)
    args = parser.parse_args()

    plan = _load_object(args.plan_json, "plan_json")
    receipt = _load_object(args.receipt_body_file.read_text(encoding="utf-8"), "budget receipt body")
    audit = verify(
        plan,
        receipt,
        execution_operation_id=args.operation_id,
        receipt_comment_id=args.receipt_comment_id,
    )
    output = Path("artifacts") / args.operation_id / "deepseek-entry-verified.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
