"""Highest-level DeepSeek technical supervisor for test2.

This layer sits above the production workflow. It may authorize bounded repository edits
or one validated budget-compliant execution-plan replacement without changing user intent.
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
`a15280020511/test2`. Web GPT owns user intent and final task decisions. Deterministic code
owns single-task locking, cancellation, state transitions, JSON-Schema validation, token
ceilings, and budget arithmetic. You own technical diagnosis and recovery decisions. Use the
official DeepSeek API only.

Your job is to keep the original operation moving whenever a safe recovery exists. A recovery
may be either:
1. a minimal verified repository EDIT; or
2. NO_EDIT + READY with `retry_operation_overrides` that changes execution mechanics only.

Mandatory constraints for every plan override:
- preserve the user's exact substantive task, facts, decision criteria, and requested outcome;
- never increase the original logical-task USD budget;
- reduce model price, token ceilings, expert count, or unnecessary stages when a 402 or budget
  preflight failure occurs;
- keep enough independent expertise for the task's risk and value;
- prefer an independent judge model family when affordable;
- include the complete valid plan_json, not a partial patch;
- expect deterministic code to reject the override unless it passes the Execution Plan Schema,
  semantic validation, current model-price preflight, provenance injection, and duplicate-Run checks.

Recovery examples:
- 402 or affordability: select current lower-cost compatible models and lower hard token ceilings.
- unavailable model: replace it with a current compatible model from supplied intelligence.
- transient 429/502/503/timeout/malformed provider response: authorize one bounded retry only.
- invalid execution plan: return one complete corrected plan that preserves the task and budget.
- single-task BUSY: do not bypass the lock and do not cancel the active task without user instruction.
- user cancellation: do not treat it as a technical fault.

Do not weaken verification, bypass the lock, remove budget controls, expose secrets, use OpenRouter
as Steward, or produce a business conclusion. If no safe recovery exists, return STOP.
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
    "plan_json": "optional complete replacement execution-plan JSON string",
    "ranking_limit": "optional replacement ranking limit"
  },
  "message_to_web_gpt": "concise technical outcome"
}

Rules:
- EDIT is only for an evidenced repository defect.
- NO_EDIT + READY is allowed for one safe transient retry or a complete budget-compliant plan replacement.
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
    if decision not in {"EDIT", "NO_EDIT", "STOP"}:
        decision = "STOP"
        result["decision"] = "STOP"
        result["diagnosis"] = "DeepSeek returned an invalid recovery decision"
    overrides = result.get("retry_operation_overrides")
    if not isinstance(overrides, dict):
        result["retry_operation_overrides"] = {}

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
