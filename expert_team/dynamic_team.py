"""Dynamic expert-team composition on Microsoft Agent Framework + OpenRouter.

The planner reads each task and decides which expert roles are needed. Expert
roles are created at runtime, executed concurrently, optionally challenged by
a red-team agent, and then synthesized by a final judge.

Configuration:
    OPENROUTER_API_KEY       Required.
    OPENROUTER_MODEL         Required fallback model.
    OPENROUTER_PLANNER_MODEL Optional planner model; falls back to OPENROUTER_MODEL.
    OPENROUTER_MODEL_POOL    Optional comma-separated OpenRouter model IDs that the
                             planner may assign to experts. If omitted, every role
                             uses the fallback model.

The model pool is an allow-list: the planner cannot invent or silently route to
models outside it. This keeps dynamic composition flexible without making the
runtime unpredictable.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

from agent_framework import Agent

from .openrouter_client import create_model_client

MAX_EXPERTS = 6


@dataclass(frozen=True)
class ExpertSpec:
    name: str
    mission: str
    instructions: str
    model: str


@dataclass(frozen=True)
class TeamPlan:
    reason: str
    experts: tuple[ExpertSpec, ...]
    red_team_enabled: bool
    red_team_model: str
    red_team_instructions: str
    judge_model: str
    judge_instructions: str


def _fallback_model() -> str:
    model = os.getenv("OPENROUTER_MODEL")
    if not model:
        raise RuntimeError("OPENROUTER_MODEL is not set")
    return model


def _allowed_models() -> tuple[str, ...]:
    fallback = _fallback_model()
    configured = [
        model.strip()
        for model in os.getenv("OPENROUTER_MODEL_POOL", "").split(",")
        if model.strip()
    ]
    # Preserve order while always allowing the fallback model.
    return tuple(dict.fromkeys([fallback, *configured]))


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract one JSON object from a model response without extra dependencies."""
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError("Planner did not return a JSON object")


def _safe_model(candidate: Any, allowed: tuple[str, ...], fallback: str) -> str:
    if isinstance(candidate, str) and candidate in allowed:
        return candidate
    return fallback


def _validate_plan(payload: dict[str, Any]) -> TeamPlan:
    allowed = _allowed_models()
    fallback = _fallback_model()

    raw_experts = payload.get("experts")
    if not isinstance(raw_experts, list) or not raw_experts:
        raise ValueError("Planner returned no experts")

    experts: list[ExpertSpec] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw_experts[:MAX_EXPERTS], start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"expert_{index}").strip()
        if name in seen_names:
            name = f"{name}_{index}"
        seen_names.add(name)

        mission = str(item.get("mission") or "Analyze the task from the assigned perspective.").strip()
        instructions = str(item.get("instructions") or mission).strip()
        model = _safe_model(item.get("model"), allowed, fallback)
        experts.append(ExpertSpec(name=name, mission=mission, instructions=instructions, model=model))

    if not experts:
        raise ValueError("Planner returned no valid expert definitions")

    raw_red = payload.get("red_team") if isinstance(payload.get("red_team"), dict) else {}
    raw_judge = payload.get("judge") if isinstance(payload.get("judge"), dict) else {}

    return TeamPlan(
        reason=str(payload.get("reason") or "Dynamic team selected for this task.").strip(),
        experts=tuple(experts),
        red_team_enabled=bool(raw_red.get("enabled", False)),
        red_team_model=_safe_model(raw_red.get("model"), allowed, fallback),
        red_team_instructions=str(
            raw_red.get("instructions")
            or "Act as an adversarial reviewer. Find unsupported assumptions, contradictions, missing evidence, failure modes, and overconfident conclusions."
        ).strip(),
        judge_model=_safe_model(raw_judge.get("model"), allowed, fallback),
        judge_instructions=str(
            raw_judge.get("instructions")
            or "Synthesize the expert work. Resolve disagreements by evidence and reasoning, explicitly preserve uncertainty, and produce the best final answer."
        ).strip(),
    )


async def plan_team(task: str) -> TeamPlan:
    """Use one planner model to build a task-specific expert team."""
    if not task.strip():
        raise ValueError("task must not be empty")

    allowed = _allowed_models()
    planner_model = os.getenv("OPENROUTER_PLANNER_MODEL") or _fallback_model()
    if planner_model not in allowed:
        # Planner may be separately configured, but it must still be explicitly allowed.
        allowed = tuple(dict.fromkeys([planner_model, *allowed]))

    planner = Agent(
        name="team_planner",
        client=create_model_client(planner_model),
        instructions=(
            "You are the expert-team architect. Analyze the user's task and create only the specialists actually needed. "
            "Do not use a fixed domain roster. Prefer 1-2 experts for simple tasks and 3-6 for genuinely complex tasks. "
            "Experts should have non-overlapping missions. Enable red-team review for high-stakes, uncertain, adversarial, strategic, financial, legal, medical, safety-critical, or evidence-poor tasks. "
            "Always define a final judge. Select models only from the provided allow-list. Return JSON only, with no markdown."
        ),
    )

    prompt = {
        "task": task,
        "allowed_models": list(allowed),
        "schema": {
            "reason": "why this team shape fits the task",
            "experts": [
                {
                    "name": "short unique role name",
                    "mission": "what this expert uniquely owns",
                    "instructions": "precise expert instructions",
                    "model": "one exact allowed model ID",
                }
            ],
            "red_team": {
                "enabled": True,
                "model": "one exact allowed model ID",
                "instructions": "adversarial review instructions",
            },
            "judge": {
                "model": "one exact allowed model ID",
                "instructions": "final synthesis and arbitration instructions",
            },
        },
        "hard_rules": [
            f"Use between 1 and {MAX_EXPERTS} experts.",
            "Do not duplicate expert missions.",
            "Use only model IDs in allowed_models.",
            "Return exactly one valid JSON object.",
        ],
    }

    response = await planner.run(json.dumps(prompt, ensure_ascii=False))
    return _validate_plan(_extract_json_object(response.text))


async def _run_expert(task: str, spec: ExpertSpec) -> dict[str, str]:
    agent = Agent(
        name=spec.name,
        client=create_model_client(spec.model),
        instructions=(
            f"Your mission: {spec.mission}\n"
            f"Instructions: {spec.instructions}\n"
            "Work independently. Separate facts, assumptions, inferences, and uncertainty. "
            "Do not defer to other experts because you cannot see their outputs."
        ),
    )
    response = await agent.run(task)
    return {"name": spec.name, "model": spec.model, "output": response.text}


async def run_dynamic_team(task: str) -> dict[str, Any]:
    """Plan, instantiate, run, red-team, and judge a task-specific expert team."""
    plan = await plan_team(task)

    expert_outputs = await asyncio.gather(
        *(_run_expert(task, spec) for spec in plan.experts)
    )

    red_team_output: str | None = None
    if plan.red_team_enabled:
        red_team = Agent(
            name="red_team",
            client=create_model_client(plan.red_team_model),
            instructions=plan.red_team_instructions,
        )
        red_payload = {
            "original_task": task,
            "expert_outputs": expert_outputs,
            "instruction": "Challenge the expert work. Do not merely summarize it.",
        }
        red_response = await red_team.run(json.dumps(red_payload, ensure_ascii=False))
        red_team_output = red_response.text

    judge = Agent(
        name="final_judge",
        client=create_model_client(plan.judge_model),
        instructions=plan.judge_instructions,
    )
    judge_payload = {
        "original_task": task,
        "team_plan": asdict(plan),
        "expert_outputs": expert_outputs,
        "red_team_output": red_team_output,
        "instruction": (
            "Produce the final answer. Use expert outputs as inputs, not as votes. Resolve conflicts explicitly; "
            "do not fabricate missing evidence; state material uncertainty."
        ),
    }
    final_response = await judge.run(json.dumps(judge_payload, ensure_ascii=False))

    return {
        "plan": asdict(plan),
        "expert_outputs": expert_outputs,
        "red_team_output": red_team_output,
        "final_answer": final_response.text,
    }
