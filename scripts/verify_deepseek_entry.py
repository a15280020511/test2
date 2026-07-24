from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}")


class EntryVerificationError(RuntimeError):
    pass


def _load_plan(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EntryVerificationError(f"plan_json is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise EntryVerificationError("plan_json must be an object")
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
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EntryVerificationError("durable DeepSeek result is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EntryVerificationError("durable DeepSeek result must be an object")
    return payload


def verify(plan: dict[str, Any]) -> dict[str, Any]:
    entry = plan.get("deepseek_entry")
    if not isinstance(entry, dict):
        raise EntryVerificationError("plan requires deepseek_entry")
    operation_id = str(entry.get("operation_id") or "").strip()
    result = _read_runtime_result(operation_id)
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
    return {
        "schema_version": 1,
        "status": "VERIFIED",
        "deepseek_operation_id": operation_id,
        "deepseek_mode": "ASSIST",
        "deepseek_status": "READY",
        "budget_tiers": ["economy", "balanced", "quality"],
        "steward_model": result.get("steward_model"),
        "provider": result.get("steward_provider"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    audit = verify(_load_plan(args.plan_json))
    output = Path("artifacts") / args.operation_id / "deepseek-entry-verified.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
