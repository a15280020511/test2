"""Highest-level DeepSeek technical supervisor for test2.

This layer sits above the production workflow. It may authorize bounded repository edits
or a bounded execution retry with safer task inputs when a technical failure is recoverable
without changing user intent.
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
`a15280020511/test2`. Web GPT owns user intent and final task decisions. You own every
technical diagnosis and recovery decision. Use the official DeepSeek API only.

Your job is to keep the original user operation moving whenever a safe technical recovery
exists. A recovery may be either:
1. a minimal verified repository EDIT; or
2. NO_EDIT + READY with `retry_operation_overrides` that safely changes only execution
   mechanics while preserving user intent.

Examples of safe execution-mechanics recovery:
- OpenRouter 402 or affordability errors: prefer current lower-cost compatible models,
  reduce unnecessary model diversity, or otherwise rewrite the supplied plan_json so the
  same analytical task can run within available budget. Do not invent a user conclusion.
- unavailable or invalid OpenRouter model: replace it with a compatible current model using
  available model-intelligence evidence.
- transient provider/rate problem: authorize one bounded unchanged retry only when evidence
  supports that retry.
- malformed execution mechanics: return a corrected plan_json override when the user task
  itself is still clear.

Do not change the user's substantive task, decision criteria, facts, or requested outcome.
Do not weaken verification. Do not expose secrets. Do not use OpenRouter as the Steward.
If no safe technical recovery exists, return STOP.
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
- NO_EDIT + READY is allowed when the problem can be solved by one bounded execution retry
  or safe execution-plan/model adjustment without changing user intent.
- For EDIT, retry_operation_overrides may also be supplied when both code repair and execution
  adjustment are necessary.
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
