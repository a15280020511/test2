from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TASK_BUDGET_USD = 1.00
DEFAULT_RECOVERY_RESERVE_RATIO = 0.30
DEFAULT_EXPERT_MAX_TOKENS = 2200
DEFAULT_RED_TEAM_MAX_TOKENS = 1600
DEFAULT_JUDGE_MAX_TOKENS = 3200
DEFAULT_CALL_TIMEOUT_SECONDS = 240
MAX_OPERATOR_BUDGET_USD = 10.00


class BudgetPreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelPrice:
    prompt_per_token: float
    completion_per_token: float


def estimate_tokens(text: str) -> int:
    """Conservative language-agnostic estimate; one token per three UTF-8 bytes."""
    raw = (text or "").encode("utf-8")
    return max(1, math.ceil(len(raw) / 3))


def _positive_int(value: Any, default: int, field: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BudgetPreflightError(f"{field} must be an integer") from exc
    if parsed < 64 or parsed > 16384:
        raise BudgetPreflightError(f"{field} must be between 64 and 16384")
    return parsed


def _positive_timeout(value: Any, default: int, field: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BudgetPreflightError(f"{field} must be an integer") from exc
    if parsed < 30 or parsed > 900:
        raise BudgetPreflightError(f"{field} must be between 30 and 900 seconds")
    return parsed


def agent_limits(payload: dict[str, Any], role: str) -> tuple[int, int]:
    default_tokens = {
        "expert": DEFAULT_EXPERT_MAX_TOKENS,
        "red_team": DEFAULT_RED_TEAM_MAX_TOKENS,
        "judge": DEFAULT_JUDGE_MAX_TOKENS,
    }[role]
    return (
        _positive_int(payload.get("max_completion_tokens"), default_tokens, f"{role}.max_completion_tokens"),
        _positive_timeout(payload.get("timeout_seconds"), DEFAULT_CALL_TIMEOUT_SECONDS, f"{role}.timeout_seconds"),
    )


def _load_snapshot() -> dict[str, Any]:
    candidates = [
        Path("runtime_results/model_intelligence_latest.json"),
        Path("model_intelligence_latest.json"),
    ]
    for path in candidates:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
    completed = subprocess.run(
        ["git", "show", "origin/runtime-results:runtime_results/model_intelligence_latest.json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise BudgetPreflightError(
            "Current model-intelligence pricing is unavailable; refresh model_intelligence before paid execution"
        )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise BudgetPreflightError("Current model-intelligence snapshot is not a JSON object")
    return value


def pricing_from_snapshot(snapshot: dict[str, Any]) -> dict[str, ModelPrice]:
    models = snapshot.get("models")
    if not isinstance(models, dict):
        raise BudgetPreflightError("Model-intelligence snapshot has no models object")
    prices: dict[str, ModelPrice] = {}
    for model_id, metadata in models.items():
        if not isinstance(metadata, dict):
            continue
        pricing = metadata.get("pricing")
        if not isinstance(pricing, dict):
            continue
        try:
            prompt = float(pricing.get("prompt"))
            completion = float(pricing.get("completion"))
        except (TypeError, ValueError):
            continue
        if prompt < 0 or completion < 0:
            continue
        prices[str(model_id)] = ModelPrice(prompt, completion)
    return prices


def _budget_settings(plan: dict[str, Any]) -> tuple[float, float, float]:
    payload = plan.get("budget")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise BudgetPreflightError("budget must be an object")
    try:
        max_total = float(payload.get("max_total_usd", DEFAULT_TASK_BUDGET_USD))
        reserve_ratio = float(payload.get("recovery_reserve_ratio", DEFAULT_RECOVERY_RESERVE_RATIO))
    except (TypeError, ValueError) as exc:
        raise BudgetPreflightError("budget values must be numeric") from exc
    operator_max = float(os.getenv("EXPERT_TEAM_MAX_BUDGET_USD", str(MAX_OPERATOR_BUDGET_USD)))
    if max_total <= 0 or max_total > operator_max:
        raise BudgetPreflightError(f"budget.max_total_usd must be > 0 and <= {operator_max:.2f}")
    if reserve_ratio < 0 or reserve_ratio > 0.50:
        raise BudgetPreflightError("budget.recovery_reserve_ratio must be between 0 and 0.50")
    usable = max_total * (1.0 - reserve_ratio)
    return max_total, reserve_ratio, usable


def _price_item(
    *,
    role: str,
    name: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    prices: dict[str, ModelPrice],
) -> dict[str, Any]:
    price = prices.get(model)
    if price is None:
        raise BudgetPreflightError(
            f"No current bounded pricing metadata for model {model}; choose a model from the current model-intelligence snapshot"
        )
    input_cost = input_tokens * price.prompt_per_token
    output_cost = output_tokens * price.completion_per_token
    return {
        "role": role,
        "name": name,
        "model": model,
        "estimated_input_tokens": input_tokens,
        "max_completion_tokens": output_tokens,
        "prompt_price_per_token": price.prompt_per_token,
        "completion_price_per_token": price.completion_per_token,
        "estimated_worst_case_usd": round(input_cost + output_cost, 6),
    }


def preflight_execution_plan(
    plan: dict[str, Any],
    *,
    pricing_by_model: dict[str, ModelPrice] | None = None,
) -> dict[str, Any]:
    max_total, reserve_ratio, usable = _budget_settings(plan)
    prices = pricing_by_model or pricing_from_snapshot(_load_snapshot())

    task = str(plan.get("task") or "")
    rationale = str(plan.get("rationale") or "")
    base_tokens = estimate_tokens(task + "\n" + rationale) + 256
    items: list[dict[str, Any]] = []
    expert_output_total = 0

    experts = plan.get("experts")
    if not isinstance(experts, list) or not experts:
        raise BudgetPreflightError("experts must be a non-empty list")
    for raw in experts:
        if not isinstance(raw, dict):
            raise BudgetPreflightError("each expert must be an object")
        max_tokens, _ = agent_limits(raw, "expert")
        expert_output_total += max_tokens
        input_tokens = base_tokens + estimate_tokens(
            str(raw.get("mission") or "") + "\n" + str(raw.get("instructions") or "")
        )
        items.append(
            _price_item(
                role="expert",
                name=str(raw.get("name") or "expert"),
                model=str(raw.get("model") or ""),
                input_tokens=input_tokens,
                output_tokens=max_tokens,
                prices=prices,
            )
        )

    red_team = plan.get("red_team")
    red_output_tokens = 0
    if isinstance(red_team, dict) and bool(red_team.get("enabled")):
        red_output_tokens, _ = agent_limits(red_team, "red_team")
        items.append(
            _price_item(
                role="red_team",
                name=str(red_team.get("name") or "red_team"),
                model=str(red_team.get("model") or ""),
                input_tokens=base_tokens + expert_output_total + estimate_tokens(str(red_team.get("instructions") or "")),
                output_tokens=red_output_tokens,
                prices=prices,
            )
        )

    judge = plan.get("judge")
    if isinstance(judge, dict) and bool(judge.get("enabled")):
        judge_tokens, _ = agent_limits(judge, "judge")
        items.append(
            _price_item(
                role="judge",
                name=str(judge.get("name") or "final_judge"),
                model=str(judge.get("model") or ""),
                input_tokens=base_tokens + expert_output_total + red_output_tokens + estimate_tokens(str(judge.get("instructions") or "")),
                output_tokens=judge_tokens,
                prices=prices,
            )
        )

    estimated = round(sum(float(item["estimated_worst_case_usd"]) for item in items), 6)
    summary = {
        "schema_version": "1",
        "max_total_usd": round(max_total, 4),
        "recovery_reserve_ratio": reserve_ratio,
        "reserved_usd": round(max_total - usable, 4),
        "normal_execution_budget_usd": round(usable, 4),
        "estimated_worst_case_usd": estimated,
        "within_budget": estimated <= usable,
        "pricing_source": "runtime_results/model_intelligence_latest.json",
        "items": items,
    }
    if estimated > usable:
        raise BudgetPreflightError(
            f"Worst-case model cost ${estimated:.4f} exceeds normal execution budget ${usable:.4f}; "
            "DeepSeek Top Supervisor must lower token limits or select lower-cost compatible models"
        )
    return summary
