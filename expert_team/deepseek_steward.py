"""Independent DeepSeek Steward for test2.

ASSIST is the highest-priority Web GPT entry, REVIEW is the final publication gate,
and REPAIR diagnoses and fixes bounded repository integration faults. All modes use the
official DeepSeek API directly and remain independent of optional expert-team plugins.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .deepseek_official import (
    DEFAULT_STEWARD_MODEL,
    DEEPSEEK_API_BASE,
    generate_official_deepseek_json,
    select_strongest_official_model,
)

MAX_CONTEXT_CHARS = 180_000
MAX_EDIT_FILES = 12
MAX_EDIT_CHARS = 300_000
SKIP_CONTEXT_PREFIXES = (".git/", "artifacts/", "runtime_results/", "__pycache__/")
PROTECTED_REPAIR_PREFIXES = (".git/", "artifacts/", "runtime_results/", "tests/")
TEXT_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".md", ".txt", ".toml", ".ini", ".cfg"}

STEWARD_POLICY = """DeepSeek Steward is the independent highest-priority technical control layer.
Web GPT owns user intent and public evidence collection, but every new expert/paid task first
comes to ASSIST for readiness, plugin, and budget guidance. REVIEW is required before
publication. Every technical anomaly comes to REPAIR before Web GPT gives up. DeepSeek owns
only repository integration and compatibility; upstream maintainers own their packages.
ASSIST and REVIEW never edit files. REPAIR may make bounded full-file changes, but never
modifies tests, generated artifacts, runtime-results, secrets, or .git data. Use the official
DeepSeek API only and never fall back to OpenRouter."""


def _steward_model() -> str:
    return select_strongest_official_model()


def _safe_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"support_packet_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("support packet must be a JSON object")
    return payload


def _repo_context(root: Path = Path(".")) -> str:
    chunks: list[str] = []
    used = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in SKIP_CONTEXT_PREFIXES):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"requirements.txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        block = f"\n===== FILE: {rel} =====\n{text}\n"
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        chunks.append(block)
        used += len(block)

    try:
        completed = subprocess.run(
            ["git", "show", "origin/runtime-results:runtime_results/model_intelligence_latest.json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.stdout and used < MAX_CONTEXT_CHARS:
            chunks.append(
                "\n===== LATEST MODEL INTELLIGENCE (possibly truncated) =====\n"
                + completed.stdout[: MAX_CONTEXT_CHARS - used]
            )
    except (subprocess.SubprocessError, OSError):
        pass
    return "".join(chunks)


def _extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("DeepSeek Steward did not return a valid JSON object")


def _validate_repair_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("repair edit path must be a non-empty string")
    path = raw_path.strip().replace("\\", "/")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe repair path: {path}")
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PROTECTED_REPAIR_PREFIXES):
        raise ValueError(f"protected repair path: {path}")
    return path


def _apply_repair_edits(payload: dict[str, Any], root: Path = Path(".")) -> dict[str, Any]:
    edits = payload.get("edits", [])
    deletes = payload.get("delete_files", [])
    if not isinstance(edits, list) or not isinstance(deletes, list):
        raise ValueError("edits and delete_files must be arrays")
    if len(edits) + len(deletes) > MAX_EDIT_FILES:
        raise ValueError(f"too many repair file operations: {len(edits) + len(deletes)} > {MAX_EDIT_FILES}")

    applied: list[str] = []
    deleted: list[str] = []
    total_chars = 0
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("each repair edit must be an object")
        path = _validate_repair_path(edit.get("path"))
        content = edit.get("content")
        if not isinstance(content, str):
            raise ValueError(f"repair edit content must be a string: {path}")
        total_chars += len(content)
        if total_chars > MAX_EDIT_CHARS:
            raise ValueError(f"repair edit payload exceeds {MAX_EDIT_CHARS} characters")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        applied.append(path)

    for raw_path in deletes:
        path = _validate_repair_path(raw_path)
        target = root / path
        if target.exists() and target.is_file():
            target.unlink()
            deleted.append(path)
    return {"applied_files": applied, "deleted_files": deleted}


def _instructions(mode: str) -> str:
    base = f"""You are DeepSeek Steward for GitHub repository a15280020511/test2.

{STEWARD_POLICY}

Use only supplied evidence and repository context. Separate facts, inferences, assumptions,
and uncertainty. Never fabricate logs, runs, files, prices, user approval, or repair success.
Return ONLY one JSON object, with no markdown fences.
"""
    if mode == "ASSIST":
        return base + """
Mode: ASSIST — mandatory first contact for a new expert-team or paid task.
Do not edit files. Determine whether the task is ready, which temporary plugins are needed,
and provide three materially different budget options for Web GPT to show the user.
Use current model intelligence when available. Cost values are estimates, not billing guarantees.
Return this JSON shape:
{
  "mode": "ASSIST",
  "status": "READY" | "STOP",
  "diagnosis": "brief task and repository assessment",
  "guidance": ["ordered concrete guidance"],
  "plugin_recommendations": [
    {"plugin": "expert-team", "needed": true, "reason": "..."}
  ],
  "budget_options": [
    {
      "tier": "economy" | "balanced" | "quality",
      "estimated_cost_usd": {"low": 0.0, "high": 0.0},
      "max_cost_usd": 0.0,
      "max_model_calls": 1,
      "max_output_tokens_per_call": 128,
      "tradeoff": "..."
    }
  ],
  "budget_question_to_user": "A concise question asking the user to select one option or set a custom maximum.",
  "execution_plan_guidance": {
    "expert_count_guidance": "...",
    "role_guidance": ["..."],
    "stage_guidance": "...",
    "red_team_guidance": "...",
    "judge_guidance": "...",
    "model_selection_guidance": "..."
  },
  "missing_information": ["..."],
  "message_to_web_gpt": "Present budget options to the user and do not execute until approval is recorded."
}
Return exactly three budget options when status is READY. Do not claim user approval.
"""
    if mode == "REVIEW":
        return base + """
Mode: REVIEW — mandatory final publication audit.
Do not edit files. Check that the result follows the task, evidence, approved budget, planned
stages, red-team/judge requirements, and uncertainty rules. Program success alone is not quality.
Return this JSON shape:
{
  "mode": "REVIEW",
  "status": "APPROVE" | "REPLAN" | "COLLECT" | "STOP",
  "diagnosis": "concise quality judgment",
  "checks": {
    "task_alignment": "PASS" | "FAIL",
    "evidence_and_assumptions": "PASS" | "FAIL",
    "internal_consistency": "PASS" | "FAIL",
    "budget_compliance": "PASS" | "FAIL",
    "stage_completion": "PASS" | "FAIL",
    "publication_safety": "PASS" | "FAIL"
  },
  "required_actions": ["..."],
  "message_to_web_gpt": "Publish only when status is APPROVE."
}
"""
    return base + """
Mode: REPAIR.
Diagnose the technical problem. Only edit when evidence supports a repository-owned integration
or compatibility defect. For external, transient, upstream-package, or budget-approval problems,
choose NO_EDIT or STOP. Do not maintain or fork upstream plugins.
Return this JSON shape:
{
  "mode": "REPAIR",
  "decision": "EDIT" | "NO_EDIT" | "STOP",
  "diagnosis": "root cause",
  "confidence": 0.0,
  "edits": [{"path": "relative/repo/path", "content": "complete replacement file content"}],
  "delete_files": ["relative/repo/path"],
  "verification": ["what the controlled workflow should verify"],
  "resume": "READY" | "STOP",
  "message_to_web_gpt": "what Web GPT should do after repair"
}
If decision is not EDIT, edits and delete_files must be empty arrays. Do not weaken validation,
remove CI gates, expose secrets, or make unrelated refactors.
"""


async def run_deepseek_steward(
    mode: str,
    support_packet_json: str,
    *,
    root: Path = Path("."),
) -> dict[str, Any]:
    normalized_mode = mode.strip().upper()
    if normalized_mode not in {"ASSIST", "REVIEW", "REPAIR"}:
        raise ValueError("steward_mode must be ASSIST, REVIEW, or REPAIR")

    support_packet = _safe_json(support_packet_json)
    request = {
        "mode": normalized_mode,
        "support_packet": support_packet,
        "repository_context": _repo_context(root),
    }
    model, response_text = await generate_official_deepseek_json(
        _instructions(normalized_mode),
        request,
    )
    result = _extract_json_object(response_text)
    result["steward_model"] = model
    result["steward_provider"] = "DeepSeek official API"
    result["steward_api_base"] = DEEPSEEK_API_BASE
    result["policy"] = "DEEPSEEK_STEWARD.md"

    if normalized_mode == "REPAIR":
        decision = str(result.get("decision") or "STOP").upper()
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
    else:
        result["repair_application"] = {"applied_files": [], "deleted_files": []}
    return result
