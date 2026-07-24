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


def _pricing_from_catalog_rows(rows: Any) -> dict[str, ModelPrice]:
    if not isinstance(rows, list):
        return {}
    prices: dict[str, ModelPrice] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        pricing = item.get("pricing")
        if not model_id or not isinstance(pricing, dict):
            continue
        try:
            prompt = float(pricing.get("prompt"))
            completion = float(pricing.get("completion"))
        except (TypeError, ValueError):
            continue
        if prompt < 0 or completion < 0:
            continue
        prices[model_id] = ModelPrice(prompt, completion)
    return prices


def _selected_model_ids(plan: dict[str, Any]) -> set[str]:
    model_ids: set[str] = set()
    experts = plan.get("experts")
    if isinstance(experts, list):
        for raw in experts:
            if not isinstance(raw, dict):
                continue
            model = str(raw.get("model") or "").strip()
            if model:
                model_ids.add(model)
            fallbacks = raw.get("fallback_models")
            if isinstance(fallbacks, list):
                model_ids.update(str(item).strip() for item in fallbacks if str(item).strip())
    for role in ("red_team", "judge"):
        raw = plan.get(role)
        if not isinstance(raw, dict) or not bool(raw.get("enabled")):
            continue
        model = str(raw.get("model") or "").strip()
        if model:
            model_ids.add(model)
        fallbacks = raw.get("fallback_models")
        if isinstance(fallbacks, list):
            model_ids.update(str(item).strip() for item in fallbacks if str(item).strip())
    return model_ids


def _load_current_prices(required_models: set[str]) -> tuple[dict[str, ModelPrice], list[str]]:
    prices = pricing_from_snapshot(_load_snapshot())
    sources = ["runtime_results/model_intelligence_latest.json"]
    missing = required_models - prices.keys()
    if missing:
        # The GPT-sized snapshot is intentionally bounded. Fetch the current official catalog
        # only when a submitted model is outside that compact planning view.
        from expert_team.model_intelligence import fetch_catalog_via_sdk

        catalog = fetch_catalog_via_sdk()
        prices.update(_pricing_from_catalog_rows(catalog.get("data")))
        sources.append("OpenRouter official model catalog preflight")
    return prices, sources


def _budget_settings(plan: dict[str, Any]) -> tuple[float, float, float, float]:
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
    normal = max_total * (1.0 - reserve_ratio)
    recovery = max_total * reserve_ratio
    return max_total, reserve_ratio, normal, recovery


def _call_cost(price: ModelPrice, input_tokens: int, output_tokens: int) -> float:
    return input_tokens * price.prompt_per_token + output_tokens * price.completion_per_token


def _price_role(
    *,
    role: str,
    name: str,
    model: str,
    fallback_models: list[str],
    input_tokens: int,
    output_tokens: int,
    prices: dict[str, ModelPrice],
) -> tuple[dict[str, Any], float, float]:
    primary = prices.get(model)
    if primary is None:
        raise BudgetPreflightError(
            f"No current pricing metadata for model {model}; choose a current model or refresh model intelligence"
        )
    primary_single = _call_cost(primary, input_tokens, output_tokens)
    attempts = [
        {
            "model": model,
            "attempts": 2,
            "prompt_price_per_token": primary.prompt_per_token,
            "completion_price_per_token": primary.completion_per_token,
            "estimated_cost_usd": round(primary_single * 2, 6),
        }
    ]
    worst_case = primary_single * 2
    for fallback in fallback_models:
        fallback_price = prices.get(fallback)
        if fallback_price is None:
            raise BudgetPreflightError(
                f"No current pricing metadata for fallback model {fallback}; choose a current fallback model"
            )
        fallback_cost = _call_cost(fallback_price, input_tokens, output_tokens)
        attempts.append(
            {
                "model": fallback,
                "attempts": 1,
                "prompt_price_per_token": fallback_price.prompt_per_token,
                "completion_price_per_token": fallback_price.completion_per_token,
                "estimated_cost_usd": round(fallback_cost, 6),
            }
        )
        worst_case += fallback_cost
    return (
        {
            "role": role,
            "name": name,
            "primary_model": model,
            "fallback_models": fallback_models,
            "estimated_input_tokens": input_tokens,
            "max_completion_tokens": output_tokens,
            "attempt_plan": attempts,
            "single_clean_call_usd": round(primary_single, 6),
            "estimated_worst_case_usd": round(worst_case, 6),
        },
        primary_single,
        worst_case,
    )


def preflight_execution_plan(
    plan: dict[str, Any],
    *,
    pricing_by_model: dict[str, ModelPrice] | None = None,
    execution_phase: str = "normal",
) -> dict[str, Any]:
    if execution_phase not in {"normal", "recovery"}:
        raise BudgetPreflightError("execution_phase must be normal or recovery")
    max_total, reserve_ratio, normal_budget, recovery_budget = _budget_settings(plan)
    if pricing_by_model is None:
        prices, pricing_sources = _load_current_prices(_selected_model_ids(plan))
    else:
        prices = pricing_by_model
        pricing_sources = ["injected deterministic test pricing"]

    available = normal_budget if execution_phase == "normal" else recovery_budget
    if available <= 0:
        raise BudgetPreflightError(f"No budget is reserved for {execution_phase} execution")

    task = str(plan.get("task") or "")
    rationale = str(plan.get("rationale") or "")
    base_tokens = estimate_tokens(task + "\n" + rationale) + 256
    items: list[dict[str, Any]] = []
    expert_output_total = 0
    single_clean_total = 0.0
    worst_case_total = 0.0

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
        fallbacks = [str(item).strip() for item in raw.get("fallback_models", []) if str(item).strip()]
        item, single, worst = _price_role(
            role="expert",
            name=str(raw.get("name") or "expert"),
            model=str(raw.get("model") or ""),
            fallback_models=fallbacks,
            input_tokens=input_tokens,
            output_tokens=max_tokens,
            prices=prices,
        )
        items.append(item)
        single_clean_total += single
        worst_case_total += worst

    red_team = plan.get("red_team")
    red_output_tokens = 0
    if isinstance(red_team, dict) and bool(red_team.get("enabled")):
        red_output_tokens, _ = agent_limits(red_team, "red_team")
        fallbacks = [str(item).strip() for item in red_team.get("fallback_models", []) if str(item).strip()]
        item, single, worst = _price_role(
            role="red_team",
            name=str(red_team.get("name") or "red_team"),
            model=str(red_team.get("model") or ""),
            fallback_models=fallbacks,
            input_tokens=base_tokens + expert_output_total + estimate_tokens(str(red_team.get("instructions") or "")),
            output_tokens=red_output_tokens,
            prices=prices,
        )
        items.append(item)
        single_clean_total += single
        worst_case_total += worst

    judge = plan.get("judge")
    if isinstance(judge, dict) and bool(judge.get("enabled")):
        judge_tokens, _ = agent_limits(judge, "judge")
        fallbacks = [str(item).strip() for item in judge.get("fallback_models", []) if str(item).strip()]
        item, single, worst = _price_role(
            role="judge",
            name=str(judge.get("name") or "final_judge"),
            model=str(judge.get("model") or ""),
            fallback_models=fallbacks,
            input_tokens=base_tokens + expert_output_total + red_output_tokens + estimate_tokens(str(judge.get("instructions") or "")),
            output_tokens=judge_tokens,
            prices=prices,
        )
        items.append(item)
        single_clean_total += single
        worst_case_total += worst

    estimated = round(worst_case_total, 6)
    summary = {
        "schema_version": "2",
        "execution_phase": execution_phase,
        "max_total_usd": round(max_total, 4),
        "recovery_reserve_ratio": reserve_ratio,
        "normal_execution_budget_usd": round(normal_budget, 4),
        "reserved_recovery_budget_usd": round(recovery_budget, 4),
        "available_execution_budget_usd": round(available, 4),
        "single_clean_pass_estimated_usd": round(single_clean_total, 6),
        "estimated_worst_case_usd": estimated,
        "includes_primary_transient_retry": True,
        "includes_all_declared_fallbacks": True,
        "within_budget": estimated <= available,
        "pricing_sources": pricing_sources,
        "items": items,
    }
    if estimated > available:
        raise BudgetPreflightError(
            f"{execution_phase.capitalize()} worst-case model cost ${estimated:.4f} exceeds available "
            f"budget ${available:.4f}; DeepSeek Top Supervisor must lower hard token limits, "
            "reduce unnecessary calls, or select lower-cost compatible models"
        )
    return summary
