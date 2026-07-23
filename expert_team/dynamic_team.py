"""Execute a Web-GPT-authored dynamic expert-team plan.

Web GPT is the planner. This module does not choose experts, models, or workflow
shape. It validates a submitted execution-plan form and executes it with
Microsoft Agent Framework through OpenRouter.

Everything task-specific can be selected by Web GPT per request:
- expert count and roles
- model for every expert
- parallel or sequential stages
- stage ordering and dependencies
- optional red-team review
- optional final judge

Optional safety configuration:
    OPENROUTER_MODEL_POOL
        Comma-separated allow-list of OpenRouter model IDs. If unset, any
        non-empty model ID supplied by Web GPT is accepted.
    EXPERT_TEAM_MAX_EXPERTS
        Runtime safety ceiling. Default: 12.
    EXPERT_TEAM_MAX_STAGES
        Runtime safety ceiling. Default: 12.
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
class ExecutionPlan:
    task: str
    rationale: str
    experts: tuple[ExpertSpec, ...]
    stages: tuple[StageSpec, ...]
    red_team: OptionalAgentSpec
    judge: OptionalAgentSpec


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


def _optional_agent(payload: Any, *, default_name: str, field: str) -> OptionalAgentSpec:
    if not isinstance(payload, dict):
        return OptionalAgentSpec(False, default_name, "", "")

    enabled = bool(payload.get("enabled", False))
    name = str(payload.get("name") or default_name).strip()
    instructions = str(payload.get("instructions") or "").strip()
    model = ""
    if enabled:
        model = _validate_model(payload.get("model"), f"{field}.model")
        if not instructions:
            raise ValueError(f"{field}.instructions is required when enabled")

    return OptionalAgentSpec(enabled, name, model, instructions)


def validate_execution_plan(payload: dict[str, Any]) -> ExecutionPlan:
    """Validate the universal execution-plan form authored by Web GPT."""
    if not isinstance(payload, dict):
        raise TypeError("execution plan must be a JSON object")

    task = str(payload.get("task") or "").strip()
    if not task:
        raise ValueError("task is required")

    rationale = str(payload.get("rationale") or "").strip()

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

    return ExecutionPlan(
        task=task,
        rationale=rationale,
        experts=tuple(experts),
        stages=tuple(stages),
        red_team=red_team,
        judge=judge,
    )


def _expert_map(plan: ExecutionPlan) -> dict[str, ExpertSpec]:
    return {expert.name: expert for expert in plan.experts}


def _source_payload(
    task: str,
    stage_outputs: dict[str, list[dict[str, str]]],
    sources: tuple[str, ...],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for source in sources:
        if source == "task":
            payload["task"] = task
        else:
            payload[source] = stage_outputs[source]
    return payload


async def _run_expert(spec: ExpertSpec, context: dict[str, Any]) -> dict[str, str]:
    agent = Agent(
        name=spec.name,
        client=create_model_client(spec.model),
        instructions=(
            f"Mission: {spec.mission}\n"
            f"Instructions: {spec.instructions}\n"
            "Use only the supplied task context and available evidence. Separate facts, assumptions, "
            "inferences, and uncertainty. Do not fabricate missing evidence."
        ),
    )
    response = await agent.run(json.dumps(context, ensure_ascii=False))
    return {"name": spec.name, "model": spec.model, "output": response.text}


async def _run_stage(
    stage: StageSpec,
    experts: dict[str, ExpertSpec],
    base_context: dict[str, Any],
) -> list[dict[str, str]]:
    if stage.mode == "parallel":
        return list(
            await asyncio.gather(
                *(_run_expert(experts[name], base_context) for name in stage.members)
            )
        )

    outputs: list[dict[str, str]] = []
    for name in stage.members:
        context = dict(base_context)
        if outputs:
            context["previous_experts_in_this_stage"] = outputs
        outputs.append(await _run_expert(experts[name], context))
    return outputs


async def run_dynamic_team(plan_payload: dict[str, Any]) -> dict[str, Any]:
    """Execute exactly the dynamic expert-team plan supplied by Web GPT."""
    plan = validate_execution_plan(plan_payload)
    experts = _expert_map(plan)

    stage_outputs: dict[str, list[dict[str, str]]] = {}
    for stage in plan.stages:
        context = _source_payload(plan.task, stage_outputs, stage.input_from)
        stage_outputs[stage.stage_id] = await _run_stage(stage, experts, context)

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
            "instruction": "Challenge the work. Identify unsupported assumptions, contradictions, missing evidence, failure modes, and overconfidence.",
        }
        red_response = await red_agent.run(json.dumps(red_payload, ensure_ascii=False))
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
            "instruction": "Produce the final answer by synthesis and arbitration, not majority voting. Preserve material uncertainty and do not fabricate evidence.",
        }
        judge_response = await judge_agent.run(json.dumps(judge_payload, ensure_ascii=False))
        final_answer = judge_response.text

    return {
        "plan_source": "web_gpt",
        "plan": asdict(plan),
        "stage_outputs": stage_outputs,
        "red_team_output": red_team_output,
        "final_answer": final_answer,
    }
