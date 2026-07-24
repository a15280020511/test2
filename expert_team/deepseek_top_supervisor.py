"""Highest-level DeepSeek technical supervisor for test2.

This layer sits above the production workflow. It may authorize bounded repository edits
or a bounded non-paid execution retry when a technical failure is recoverable without
changing user intent. Paid expert-team attempts are never redispatched automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .deepseek_official import DEEPSEEK_API_BASE, generate_official_deepseek_json
from .deepseek_steward import (
    _apply_repair_edits,
    _extract_json_object,
    _repo_context,
    _safe_json,
)


TOP_SUPERVISOR_POLICY = """You are the highest technical supervisor for GitHub repository
`a15280020511/test2`. Web GPT owns user intent, user budget communication, and final task
decisions. You own every technical diagnosis and recovery decision. Use the official DeepSeek
API only.

Your job is to diagnose every failure and keep the original operation moving only when recovery
is safe and authorized. A recovery may be either:
1. a minimal verified repository EDIT; or
2. for non-paid operations only, NO_EDIT + READY with `retry_operation_overrides` that changes
   execution mechanics while preserving user intent.

Hard budget boundary:
- If the original operation is `execute_team`, the failed attempt may already have consumed some
  or all of the approved model budget.
- Never authorize automatic redispatch, unchanged retry, lower-cost model substitution, plan
  rewrite, or additional model calls for `execute_team` under the old receipt.
- You may diagnose and repair repository code, but the current operation must remain STOP.
- Tell Web GPT to explain the failure and possible prior spend to the user, present a new budget,
  obtain explicit approval, create a new durable budget receipt, and use a new operation_id.
- Never fabricate, alter, reuse, or bypass user budget approval.

Examples of safe non-paid execution-mechanics recovery:
- model-intelligence affordability errors: reduce ranking size or use a compatible read-only path;
- unavailable or invalid catalog model data: use current compatible model-intelligence evidence;
- transient provider/rate problem in a non-paid operation: authorize one bounded unchanged retry
  only when evidence supports it;
- malformed non-paid execution mechanics: return a corrected ranking or request override.

Do not change the user's substantive task, decision criteria, facts, requested outcome, or budget.
Do not weaken verification. Do not expose secrets. Do not use OpenRouter as the Steward. If no
safe technical recovery exists, return STOP.
"""


def _instructions() -> str:
    return TOP_SUPERVISOR_POLICY + """
Return ONLY one JSON object, with no markdown fences:
{
  "mode": "REPAIR",
  "decision": "EDIT" | "NO_EDIT" | "STOP",
  "diagnosis": "root cause",
  "confidence": 0.0,
  "edits": [{"path": "relative/repo/path", "content": "complete replacement file content"}],
  "delete_files": ["relative/repo/path"],
  "verification": ["verification requirements"],
  "resume": "READY" | "STOP",
  "retry_operation_overrides": {
    "plan_json": "optional complete replacement execution-plan JSON string for non-paid operations only",
    "ranking_limit": "optional replacement ranking limit"
  },
  "budget_reapproval_required": false,
  "message_to_web_gpt": "concise technical outcome"
}

Rules:
- EDIT is only for an evidenced repository defect.
- For execute_team, always set resume=STOP, retry_operation_overrides={}, and
  budget_reapproval_required=true, even when an EDIT is appropriate.
- NO_EDIT + READY is allowed only for one bounded non-paid operation retry or safe non-paid
  execution adjustment.
- For EDIT on a non-paid operation, retry_operation_overrides may be supplied when both code repair
  and execution adjustment are necessary.
- If no override is needed, return an empty retry_operation_overrides object.
- Never modify tests/, runtime_results/, artifacts/, .git/, or secrets.
"""


async def run_deepseek_top_supervisor(
    support_packet_json: str,
    *,
    root: Path = Path("."),
) -> dict[str, Any]:
    support_packet = _safe_json(support_packet_json)
    context = _repo_context(root)
    request = {
        "mode": "REPAIR",
        "support_packet": support_packet,
        "repository_context": context,
    }
    model, response_text = await generate_official_deepseek_json(_instructions(), request)
    result = _extract_json_object(response_text)
    result["mode"] = "REPAIR"
    result["steward_model"] = model
    result["steward_provider"] = "DeepSeek official API"
    result["steward_api_base"] = DEEPSEEK_API_BASE
    result["policy"] = "DEEPSEEK_STEWARD.md"
    result["supervisor"] = "deepseek_top_supervisor"

    decision = str(result.get("decision") or "STOP").upper()
    overrides = result.get("retry_operation_overrides")
    if not isinstance(overrides, dict):
        result["retry_operation_overrides"] = {}

    operation = str(support_packet.get("operation") or "").strip()
    if operation == "execute_team":
        result["resume"] = "STOP"
        result["retry_operation_overrides"] = {}
        result["budget_reapproval_required"] = True

    if decision == "EDIT":
        result["repair_application"] = _apply_repair_edits(result, root)
        result["resume"] = "STOP"
        result["repair_delivery"] = {
            "status": "pending_verification",
            "method": None,
            "pull_request_url": None,
            "verification": "pending",
        }
    else:
        result["repair_application"] = {"applied_files": [], "deleted_files": []}
        result["edits"] = []
        result["delete_files"] = []
        if decision == "STOP":
            result["resume"] = "STOP"

    return result
