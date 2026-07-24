"""Execute a Web-GPT-authored dynamic expert-team plan.

The permanent DeepSeek control core is dependency-free. This module is imported only
inside the task-scoped ``expert-team`` plug. Every paid execution must carry evidence of
an earlier DeepSeek entry review and an explicit user-approved budget.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from agent_framework import Agent

from .openrouter_client import create_model_client


@dataclass(frozen=True)
class ExpertSpec:
    name: str
    mission: str
    instructions: str
    model: str


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    mode: str
    members: tuple[str, ...]
    input_from: tuple[str, ...]


@dataclass(frozen=True)
class OptionalAgentSpec:
    enabled: bool
    name: str
    model: str
    instructions: str


@dataclass(frozen=True)
class DeepSeekEntrySpec:
    status: str
    operation_id: str
    budget_options_presented: bool


@dataclass(frozen=True)
class BudgetSpec:
    approval_status: str
    tier: str
    currency: str
    max_cost_usd: float
    estimated_low_usd: float
    estimated_high_usd: float
    max_model_calls: int
    max_output_tokens_per_call: int
    approval_reference: str


@dataclass(frozen=True)
class ExecutionPlan:
    task: str
    rationale: str
    deepseek_entry: DeepSeekEntrySpec
    budget: BudgetSpec
    experts: tuple[ExpertSpec, ...]
    stages: tuple[StageSpec, ...]
    red_team: OptionalAgentSpec
    judge: OptionalAgentSpec


BUDGET_TIERS = {"economy", "balanced", "quality", "custom"}


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be >= 1")
    return value


def _model_allowlist() -> set[str] | None:
    models = {
        model.strip()
        for model in os.getenv("OPENROUTER_MODEL_POOL", "").split(",")
        if model.strip()
    }
    return models or None


def _validate_model(model: Any, field: str) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{field} must contain an OpenRouter model ID")
    model = model.strip()
    allowlist = _model_allowlist()
    if allowlist is not None and model not in allowlist:
        raise ValueError(f"{field} model is not in OPENROUTER_MODEL_POOL: {model}")
    return model


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _bounded_money(value: Any, field: str, *, minimum: float = 0.0, maximum: float = 1000.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return round(parsed, 6)


def _deepseek_entry(payload: Any) -> DeepSeekEntrySpec:
    if not isinstance(payload, dict):
        raise ValueError("deepseek_entry must be an object")
    status = str(payload.get("status") or "").strip().upper()
    if status != "READY":
        raise ValueError("deepseek_entry.status must be READY")
    operation_id = str(payload.get("operation_id") or "").strip()
    if not operation_id:
        raise ValueError("deepseek_entry.operation_id is required")
    presented = _strict_bool(payload.get("budget_options_presented"), "deepseek_entry.budget_options_presented")
    if not presented:
        raise ValueError("DeepSeek budget options must be presented to the user before execution")
    return DeepSeekEntrySpec(status, operation_id, presented)


def _budget(payload: Any) -> BudgetSpec:
    if not isinstance(payload, dict):
        raise ValueError("budget must be an object")
    approval_status = str(payload.get("approval_status") or "").strip().lower()
    if approval_status != "approved_by_user":
        raise ValueError("budget.approval_status must be approved_by_user")
    tier = str(payload.get("tier") or "").strip().lower()
    if tier not in BUDGET_TIERS:
        raise ValueError(f"budget.tier must be one of {sorted(BUDGET_TIERS)}")
    currency = str(payload.get("currency") or "").strip().upper()
    if currency != "USD":
        raise ValueError("budget.currency must be USD because OpenRouter model prices are denominated in USD")
    max_cost = _bounded_money(payload.get("max_cost_usd"), "budget.max_cost_usd", minimum=0.000001)
    estimate = payload.get("estimated_cost_usd")
    if not isinstance(estimate, dict):
        raise ValueError("budget.estimated_cost_usd must be an object")
    low = _bounded_money(estimate.get("low"), "budget.estimated_cost_usd.low")
    high = _bounded_money(estimate.get("high"), "budget.estimated_cost_usd.high")
    if high < low:
        raise ValueError("budget estimated high cost cannot be lower than low cost")
    if high > max_cost:
        raise ValueError("budget estimated high cost exceeds the user-approved maximum")
    max_calls = _bounded_int(payload.get("max_model_calls"), "budget.max_model_calls", 1, 50)
    max_tokens = _bounded_int(
        payload.get("max_output_tokens_per_call"),
        "budget.max_output_tokens_per_call",
        128,
        32768,
    )
    approval_reference = str(payload.get("approval_reference") or "").strip()
    if not approval_reference:
        raise ValueError("budget.approval_reference is required")
    return BudgetSpec(
        approval_status=approval_status,
        tier=tier,
        currency=currency,
        max_cost_usd=max_cost,
        estimated_low_usd=low,
        estimated_high_usd=high,
        max_model_calls=max_calls,
        max_output_tokens_per_call=max_tokens,
        approval_reference=approval_reference,
    )


def _optional_agent(payload: Any, *, default_name: str, field: str) -> OptionalAgentSpec:
    if not isinstance(payload, dict):
        return OptionalAgentSpec(False, default_name, "", "")
    enabled = _strict_bool(payload.get("enabled", False), f"{field}.enabled")
    name = str(payload.get("name") or default_name).strip()
    instructions = str(payload.get("instructions") or "").strip()
    model = ""
    if enabled:
        model = _validate_model(payload.get("model"), f"{field}.model")
        if not instructions:
            raise ValueError(f"{field}.instructions is required when enabled")
    return OptionalAgentSpec(enabled, name, model, instructions)


def validate_execution_plan(payload: dict[str, Any]) -> ExecutionPlan:
    """Validate the universal execution-plan form before any paid model call."""
    if not isinstance(payload, dict):
        raise TypeError("execution plan must be a JSON object")

    task = str(payload.get("task") or "").strip()
    if not task:
        raise ValueError("task is required")
    rationale = str(payload.get("rationale") or "").strip()
    deepseek_entry = _deepseek_entry(payload.get("deepseek_entry"))
    budget = _budget(payload.get("budget"))

    raw_experts = payload.get("experts")
    if not isinstance(raw_experts, list) or not raw_experts:
        raise ValueError("experts must be a non-empty list")
    max_experts = _positive_int_env("EXPERT_TEAM_MAX_EXPERTS", 12)
    if len(raw_experts) > max_experts:
        raise ValueError(f"too many experts: {len(raw_experts)} > {max_experts}")

    experts: list[ExpertSpec] = []
    expert_names: set[str] = set()
    for index, raw in enumerate(raw_experts, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"experts[{index - 1}] must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"experts[{index - 1}].name is required")
        if name in expert_names:
            raise ValueError(f"duplicate expert name: {name}")
        expert_names.add(name)
        mission = str(raw.get("mission") or "").strip()
        instructions = str(raw.get("instructions") or mission).strip()
        if not mission:
            raise ValueError(f"experts[{index - 1}].mission is required")
        if not instructions:
            raise ValueError(f"experts[{index - 1}].instructions is required")
        experts.append(
            ExpertSpec(
                name=name,
                mission=mission,
                instructions=instructions,
                model=_validate_model(raw.get("model"), f"experts[{index - 1}].model"),
            )
        )

    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ValueError("stages must be a non-empty list")
    max_stages = _positive_int_env("EXPERT_TEAM_MAX_STAGES", 12)
    if len(raw_stages) > max_stages:
        raise ValueError(f"too many stages: {len(raw_stages)} > {max_stages}")

    stages: list[StageSpec] = []
    prior_stage_ids: set[str] = set()
    for index, raw in enumerate(raw_stages):
        if not isinstance(raw, dict):
            raise ValueError(f"stages[{index}] must be an object")
        stage_id = str(raw.get("id") or "").strip()
        if not stage_id:
            raise ValueError(f"stages[{index}].id is required")
        if stage_id in prior_stage_ids:
            raise ValueError(f"duplicate stage id: {stage_id}")
        mode = str(raw.get("mode") or "parallel").strip().lower()
        if mode not in {"parallel", "sequential"}:
            raise ValueError(f"stages[{index}].mode must be parallel or sequential")
        members_raw = raw.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise ValueError(f"stages[{index}].members must be a non-empty list")
        members = tuple(str(member).strip() for member in members_raw)
        unknown_members = [member for member in members if member not in expert_names]
        if unknown_members:
            raise ValueError(f"stages[{index}] references unknown experts: {unknown_members}")
        inputs_raw = raw.get("input_from", ["task"])
        if not isinstance(inputs_raw, list) or not inputs_raw:
            raise ValueError(f"stages[{index}].input_from must be a non-empty list")
        input_from = tuple(str(source).strip() for source in inputs_raw)
        allowed_sources = {"task", *prior_stage_ids}
        invalid_sources = [source for source in input_from if source not in allowed_sources]
        if invalid_sources:
            raise ValueError(
                f"stages[{index}] input_from may reference only task or earlier stages: {invalid_sources}"
            )
        stages.append(StageSpec(stage_id, mode, members, input_from))
        prior_stage_ids.add(stage_id)

    red_team = _optional_agent(payload.get("red_team"), default_name="red_team", field="red_team")
    judge = _optional_agent(payload.get("judge"), default_name="final_judge", field="judge")
    planned_calls = sum(len(stage.members) for stage in stages) + int(red_team.enabled) + int(judge.enabled)
    if planned_calls > budget.max_model_calls:
        raise ValueError(
            f"planned model calls exceed user-approved limit: {planned_calls} > {budget.max_model_calls}"
        )

    return ExecutionPlan(
        task=task,
        rationale=rationale,
        deepseek_entry=deepseek_entry,
        budget=budget,
        experts=tuple(experts),
        stages=tuple(stages),
        red_team=red_team,
        judge=judge,
    )


def _expert_map(plan: ExecutionPlan) -> dict[str, ExpertSpec]:
    return {expert.name: expert for expert in plan.experts}


def _source_payload(task: str, stage_outputs: dict[str, list[dict[str, str]]], sources: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for source in sources:
        payload["task" if source == "task" else source] = task if source == "task" else stage_outputs[source]
    return payload


async def _run_expert(spec: ExpertSpec, context: dict[str, Any], max_tokens: int) -> dict[str, str]:
    agent = Agent(
        name=spec.name,
        client=create_model_client(spec.model),
        instructions=(
            f"Mission: {spec.mission}\n"
            f"Instructions: {spec.instructions}\n"
            "Use only supplied task context and evidence. Separate facts, assumptions, inferences, and uncertainty."
        ),
    )
    response = await agent.run(json.dumps(context, ensure_ascii=False), options={"max_tokens": max_tokens})
    return {"name": spec.name, "model": spec.model, "output": response.text}


async def _run_stage(
    stage: StageSpec,
    experts: dict[str, ExpertSpec],
    base_context: dict[str, Any],
    max_tokens: int,
) -> list[dict[str, str]]:
    if stage.mode == "parallel":
        return list(
            await asyncio.gather(
                *(_run_expert(experts[name], base_context, max_tokens) for name in stage.members)
            )
        )
    outputs: list[dict[str, str]] = []
    for name in stage.members:
        context = dict(base_context)
        if outputs:
            context["previous_experts_in_this_stage"] = outputs
        outputs.append(await _run_expert(experts[name], context, max_tokens))
    return outputs


async def run_dynamic_team(plan_payload: dict[str, Any]) -> dict[str, Any]:
    """Execute exactly the approved plan with deterministic call and output-token limits."""
    plan = validate_execution_plan(plan_payload)
    experts = _expert_map(plan)
    max_tokens = plan.budget.max_output_tokens_per_call

    stage_outputs: dict[str, list[dict[str, str]]] = {}
    for stage in plan.stages:
        context = _source_payload(plan.task, stage_outputs, stage.input_from)
        stage_outputs[stage.stage_id] = await _run_stage(stage, experts, context, max_tokens)

    red_team_output: str | None = None
    if plan.red_team.enabled:
        red_agent = Agent(
            name=plan.red_team.name,
            client=create_model_client(plan.red_team.model),
            instructions=plan.red_team.instructions,
        )
        red_payload = {
            "original_task": plan.task,
            "rationale": plan.rationale,
            "stage_outputs": stage_outputs,
            "instruction": "Challenge unsupported assumptions, contradictions, missing evidence, failure modes, and overconfidence.",
        }
        red_response = await red_agent.run(
            json.dumps(red_payload, ensure_ascii=False),
            options={"max_tokens": max_tokens},
        )
        red_team_output = red_response.text

    final_answer: str | None = None
    if plan.judge.enabled:
        judge_agent = Agent(
            name=plan.judge.name,
            client=create_model_client(plan.judge.model),
            instructions=plan.judge.instructions,
        )
        judge_payload = {
            "original_task": plan.task,
            "rationale": plan.rationale,
            "stage_outputs": stage_outputs,
            "red_team_output": red_team_output,
            "instruction": "Synthesize and arbitrate by evidence, not majority voting. Preserve material uncertainty.",
        }
        judge_response = await judge_agent.run(
            json.dumps(judge_payload, ensure_ascii=False),
            options={"max_tokens": max_tokens},
        )
        final_answer = judge_response.text

    planned_calls = sum(len(stage.members) for stage in plan.stages) + int(plan.red_team.enabled) + int(plan.judge.enabled)
    return {
        "plan_source": "web_gpt_after_deepseek_and_user_budget_approval",
        "plan": asdict(plan),
        "budget_enforcement": {
            "approval_status": plan.budget.approval_status,
            "tier": plan.budget.tier,
            "currency": plan.budget.currency,
            "estimated_cost_usd": {
                "low": plan.budget.estimated_low_usd,
                "high": plan.budget.estimated_high_usd,
            },
            "user_approved_max_cost_usd": plan.budget.max_cost_usd,
            "planned_model_calls": planned_calls,
            "max_model_calls": plan.budget.max_model_calls,
            "max_output_tokens_per_call": max_tokens,
            "note": "This is a pre-execution estimate and token/call guardrail, not a provider billing guarantee.",
        },
        "stage_outputs": stage_outputs,
        "red_team_output": red_team_output,
        "final_answer": final_answer,
    }
