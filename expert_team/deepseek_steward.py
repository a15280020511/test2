"""DeepSeek Steward: repository service manager for test2.

ASSIST mode gives Web GPT repository-facing guidance without editing files.
REPAIR mode diagnoses repository faults and applies bounded full-file edits that are
subsequently verified and delivered through the GitHub repair workflow.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from agent_framework import Agent

from .openrouter_client import create_model_client

DEFAULT_STEWARD_MODEL = "deepseek/deepseek-v4-pro"
MAX_CONTEXT_CHARS = 180_000
MAX_EDIT_FILES = 12
MAX_EDIT_CHARS = 300_000

SKIP_CONTEXT_PREFIXES = (
    ".git/",
    "artifacts/",
    "runtime_results/",
    "__pycache__/",
)
PROTECTED_REPAIR_PREFIXES = (
    ".git/",
    "artifacts/",
    "runtime_results/",
    "tests/",
)
TEXT_SUFFIXES = {
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".toml",
    ".ini",
    ".cfg",
}

STEWARD_POLICY = """Web GPT owns user intent and final task decisions. DeepSeek Steward owns
repository technical diagnosis, maintenance and repair, and also assists Web GPT with
repository use and Execution Plan preparation. ASSIST never edits files. REPAIR may edit
repository source/configuration through bounded full-file replacements, but must never
modify tests, generated artifacts, runtime-results, secrets, or .git data. It must prefer
the smallest evidence-based repair and return STOP/NO_EDIT for external or transient
failures rather than inventing code changes. Autonomous repairs are accepted only after
the repository verification gate passes."""


def _steward_model() -> str:
    return os.getenv("DEEPSEEK_STEWARD_MODEL", DEFAULT_STEWARD_MODEL).strip() or DEFAULT_STEWARD_MODEL


def _safe_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"support_packet_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("support packet must be a JSON object")
    return payload


def _repo_context(root: Path = Path(".")) -> str:
    """Build a bounded text snapshot of repository source/configuration."""
    chunks: list[str] = []
    used = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel == p.rstrip("/") or rel.startswith(p) for p in SKIP_CONTEXT_PREFIXES):
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
            [
                "git",
                "show",
                "origin/runtime-results:runtime_results/model_intelligence_latest.json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        model_intel = completed.stdout
        if model_intel and used < MAX_CONTEXT_CHARS:
            remaining = MAX_CONTEXT_CHARS - used
            chunks.append(
                "\n===== LATEST MODEL INTELLIGENCE (possibly truncated) =====\n"
                + model_intel[:remaining]
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
    if any(path == p.rstrip("/") or path.startswith(p) for p in PROTECTED_REPAIR_PREFIXES):
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

You are not the user-facing commander. Web GPT remains responsible for user intent and final task decisions.
You are the repository technical service manager and Web GPT's repository-facing assistant.
Use only supplied evidence and repository context. Separate facts, inferences, and uncertainty. Never fabricate logs, runs, files, or repair success.
Return ONLY one JSON object, with no markdown fences.
"""
    if mode == "ASSIST":
        return base + """
Mode: ASSIST.
Do not edit repository files. Advise Web GPT how to use this repository and, when relevant, how to fill the Execution Plan.
Return this shape:
{
  "mode": "ASSIST",
  "status": "READY" | "STOP",
  "diagnosis": "brief assessment of the request/current state",
  "guidance": ["ordered concrete guidance"],
  "execution_plan_guidance": {
    "expert_count_guidance": "...",
    "role_guidance": ["..."],
    "stage_guidance": "...",
    "red_team_guidance": "...",
    "judge_guidance": "...",
    "model_selection_guidance": "..."
  },
  "missing_information": ["..."],
  "message_to_web_gpt": "concise next action"
}
Use current repository rules and latest model intelligence when present. Do not fill in fake current rankings.
"""
    return base + """
Mode: REPAIR.
Diagnose the technical problem. Only edit when evidence supports a repository defect. For external/transient problems, choose NO_EDIT or STOP.
Repairs must be minimal full-file replacements/deletions. Do not provide shell commands. Do not modify tests/, runtime_results/, artifacts/, or .git/.
Return this shape:
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
If decision is not EDIT, edits and delete_files must be empty arrays.
Do not weaken validation, remove mandatory CI gates, or make unrelated refactors.
"""


async def run_deepseek_steward(
    mode: str,
    support_packet_json: str,
    *,
    root: Path = Path("."),
) -> dict[str, Any]:
    normalized_mode = mode.strip().upper()
    if normalized_mode not in {"ASSIST", "REPAIR"}:
        raise ValueError("steward_mode must be ASSIST or REPAIR")

    support_packet = _safe_json(support_packet_json)
    context = _repo_context(root)
    agent = Agent(
        name="deepseek_steward",
        client=create_model_client(_steward_model()),
        instructions=_instructions(normalized_mode),
    )
    request = {
        "mode": normalized_mode,
        "support_packet": support_packet,
        "repository_context": context,
    }
    response = await agent.run(json.dumps(request, ensure_ascii=False))
    result = _extract_json_object(response.text)
    result["steward_model"] = _steward_model()
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
