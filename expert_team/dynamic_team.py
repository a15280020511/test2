"""Execute a dynamically authored expert-team plan through Microsoft Agent Framework.

Web GPT owns user intent and initial planning. Runtime code owns deterministic safety:
JSON-Schema validation, model allow-listing, hard token limits, timeouts, bounded retries,
partial-result preservation, provenance, and audit records.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_framework import Agent
from jsonschema import Draft202012Validator

from .budget import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    DEFAULT_EXPERT_MAX_TOKENS,
    DEFAULT_JUDGE_MAX_TOKENS,
    DEFAULT_RED_TEAM_MAX_TOKENS,
    agent_limits,
    estimate_tokens,
)
from .openrouter_client import create_model_client


@dataclass(frozen=True)
class ExpertSpec:
    name: str
    mission: str
    instructions: str
    model: str
    max_completion_tokens: int
    timeout_seconds: int
    fallback_models: tuple[str, ...]


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    mode: str
    members: tuple[str, ...]
    input_from: tuple[str, ...]
    failure_policy: str
    minimum_successful_members: int


@dataclass(frozen=True)
class OptionalAgentSpec:
    enabled: bool
    name: str
    model: str
    instructions: str
    max_completion_tokens: int
    timeout_seconds: int
    fallback_models: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionPlan:
    task: str
    rationale: str
    budget: dict[str, Any]
    provenance: dict[str, Any]
    experts: tuple[ExpertSpec, ...]
    stages: tuple[StageSpec, ...]
    red_team: OptionalAgentSpec
    judge: OptionalAgentSpec


class StageExecutionError(RuntimeError):
    def __init__(self, stage_id: str, failures: list[dict[str, Any]], successes: int) -> None:
        self.stage_id = stage_id
        self.failures = failures
        self.successes = successes
        super().__init__(
            f"Stage {stage_id} did not meet its success policy: successes={successes}, "
            f"failures={json.dumps(failures, ensure_ascii=False)[:4000]}"
        )


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


def _fallback_models(payload: dict[str, Any], field: str) -> tuple[str, ...]:
    raw = payload.get("fallback_models", [])
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > 2:
        raise ValueError(f"{field}.fallback_models must be an array with at most two entries")
    values = tuple(_validate_model(value, f"{field}.fallback_models") for value in raw)
    if len(set(values)) != len(values):
        raise ValueError(f"{field}.fallback_models must be unique")
    return values


def _schema_validator() -> Draft202012Validator:
    schema_path = Path(__file__).resolve().parent.parent / "execution_plan.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _optional_agent(payload: Any, *, default_name: str, field: str) -> OptionalAgentSpec:
    if not isinstance(payload, dict):
        default_tokens = DEFAULT_RED_TEAM_MAX_TOKENS if field == "red_team" else DEFAULT_JUDGE_MAX_TOKENS
        return OptionalAgentSpec(False, default_name, "", "", default_tokens, DEFAULT_CALL_TIMEOUT_SECONDS, ())

    enabled = bool(payload.get("enabled", False))
    name = str(payload.get("name") or default_name).strip()
    instructions = str(payload.get("instructions") or "").strip()
    role = "red_team" if field == "red_team" else "judge"
    max_tokens, timeout_seconds = agent_limits(payload, role)
    model = ""
    fallbacks: tuple[str, ...] = ()
    if enabled:
        model = _validate_model(payload.get("model"), f"{field}.model")
        fallbacks = _fallback_models(payload, field)
        if not instructions:
            raise ValueError(f"{field}.instructions is required when enabled")
    return OptionalAgentSpec(enabled, name, model, instructions, max_tokens, timeout_seconds, fallbacks)


def validate_execution_plan(payload: dict[str, Any]) -> ExecutionPlan:
    """Validate JSON Schema and semantic execution constraints before any paid call."""
    if not isinstance(payload, dict):
        raise TypeError("execution plan must be a JSON object")

    errors = sorted(_schema_validator().iter_errors(payload), key=lambda item: list(item.absolute_path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise ValueError(f"execution plan schema violation at {location}: {first.message}")

    task = str(payload.get("task") or "").strip()
    rationale = str(payload.get("rationale") or "").strip()
    raw_experts = payload.get("experts")
    assert isinstance(raw_experts, list)

    max_experts = _positive_int_env("EXPERT_TEAM_MAX_EXPERTS", 12)
    if len(raw_experts) > max_experts:
        raise ValueError(f"too many experts: {len(raw_experts)} > {max_experts}")

    experts: list[ExpertSpec] = []
    expert_names: set[str] = set()
    for index, raw in enumerate(raw_experts):
        assert isinstance(raw, dict)
        name = str(raw.get("name") or "").strip()
        if name in expert_names:
            raise ValueError(f"duplicate expert name: {name}")
        expert_names.add(name)
        max_tokens, timeout_seconds = agent_limits(raw, "expert")
        experts.append(
            ExpertSpec(
                name=name,
                mission=str(raw.get("mission") or "").strip(),
                instructions=str(raw.get("instructions") or "").strip(),
                model=_validate_model(raw.get("model"), f"experts[{index}].model"),
                max_completion_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                fallback_models=_fallback_models(raw, f"experts[{index}]"),
            )
        )

    raw_stages = payload.get("stages")
    assert isinstance(raw_stages, list)
    max_stages = _positive_int_env("EXPERT_TEAM_MAX_STAGES", 12)
    if len(raw_stages) > max_stages:
        raise ValueError(f"too many stages: {len(raw_stages)} > {max_stages}")

    stages: list[StageSpec] = []
    prior_stage_ids: set[str] = set()
    for index, raw in enumerate(raw_stages):
        assert isinstance(raw, dict)
        stage_id = str(raw.get("id") or "").strip()
        if stage_id in prior_stage_ids:
            raise ValueError(f"duplicate stage id: {stage_id}")
        mode = str(raw.get("mode") or "parallel").strip().lower()
        members_raw = raw.get("members")
        assert isinstance(members_raw, list)
        members = tuple(str(member).strip() for member in members_raw)
        unknown_members = [member for member in members if member not in expert_names]
        if unknown_members:
            raise ValueError(f"stages[{index}] references unknown experts: {unknown_members}")
        inputs_raw = raw.get("input_from", ["task"])
        assert isinstance(inputs_raw, list)
        input_from = tuple(str(source).strip() for source in inputs_raw)
        allowed_sources = {"task", *prior_stage_ids}
        invalid_sources = [source for source in input_from if source not in allowed_sources]
        if invalid_sources:
            raise ValueError(
                f"stages[{index}] input_from may reference only task or earlier stages: {invalid_sources}"
            )
        failure_policy = str(raw.get("failure_policy") or "fail_fast")
        minimum = int(raw.get("minimum_successful_members") or len(members))
        if minimum < 1 or minimum > len(members):
            raise ValueError(f"stages[{index}].minimum_successful_members must be between 1 and member count")
        stages.append(StageSpec(stage_id, mode, members, input_from, failure_policy, minimum))
        prior_stage_ids.add(stage_id)

    red_team = _optional_agent(payload.get("red_team"), default_name="red_team", field="red_team")
    judge = _optional_agent(payload.get("judge"), default_name="final_judge", field="judge")
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}

    return ExecutionPlan(
        task=task,
        rationale=rationale,
        budget=dict(budget),
        provenance=dict(provenance),
        experts=tuple(experts),
        stages=tuple(stages),
        red_team=red_team,
        judge=judge,
    )


def _expert_map(plan: ExecutionPlan) -> dict[str, ExpertSpec]:
    return {expert.name: expert for expert in plan.experts}


def _source_payload(task: str, stage_outputs: dict[str, list[dict[str, Any]]], sources: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for source in sources:
        payload[source] = task if source == "task" else stage_outputs[source]
    return payload


def _classify_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "402" in text or "insufficient credit" in text or "afford" in text:
        return "budget_or_credit"
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in text:
        return "timeout"
    if any(marker in text for marker in ("429", "502", "503", "jsondecodeerror", "expecting value", "rate limit", "temporar")):
        return "transient_provider"
    if any(marker in text for marker in ("model not found", "unknown model", "404")):
        return "model_unavailable"
    return "permanent_or_unknown"


async def _run_model_agent(
    *,
    name: str,
    role: str,
    instructions: str,
    models: tuple[str, ...],
    prompt: str,
    max_completion_tokens: int,
    timeout_seconds: int,
) -> tuple[str, str, list[dict[str, Any]]]:
    call_records: list[dict[str, Any]] = []
    for model_index, model in enumerate(models):
        for attempt in range(1, 3):
            started = time.monotonic()
            record: dict[str, Any] = {
                "name": name,
                "role": role,
                "model": model,
                "model_index": model_index,
                "attempt": attempt,
                "max_completion_tokens": max_completion_tokens,
                "timeout_seconds": timeout_seconds,
                "estimated_input_tokens": estimate_tokens(prompt + instructions),
            }
            try:
                agent = Agent(name=name, client=create_model_client(model), instructions=instructions)
                response = await asyncio.wait_for(
                    agent.run(prompt, options={"max_tokens": max_completion_tokens}),
                    timeout=timeout_seconds,
                )
                text = response.text or ""
                if not text.strip():
                    raise RuntimeError("model returned empty text")
                record.update(
                    {
                        "status": "success",
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "estimated_output_tokens": estimate_tokens(text),
                    }
                )
                call_records.append(record)
                return model, text, call_records
            except Exception as exc:
                failure_class = _classify_error(exc)
                record.update(
                    {
                        "status": "failure",
                        "failure_class": failure_class,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[-4000:],
                        "duration_seconds": round(time.monotonic() - started, 3),
                    }
                )
                call_records.append(record)
                if failure_class == "budget_or_credit":
                    raise RuntimeError(json.dumps(record, ensure_ascii=False)) from exc
                if failure_class in {"timeout", "transient_provider"} and attempt == 1:
                    await asyncio.sleep(2)
                    continue
                break
    raise RuntimeError(json.dumps(call_records[-1] if call_records else {"error": "no model attempts"}, ensure_ascii=False))


async def _run_expert(spec: ExpertSpec, context: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    instructions = (
        f"Mission: {spec.mission}\n"
        f"Instructions: {spec.instructions}\n"
        "Use only supplied context and evidence. Separate facts, assumptions, inferences, and uncertainty. "
        "Do not fabricate missing evidence."
    )
    model, output, records = await _run_model_agent(
        name=spec.name,
        role="expert",
        instructions=instructions,
        models=(spec.model, *spec.fallback_models),
        prompt=json.dumps(context, ensure_ascii=False),
        max_completion_tokens=spec.max_completion_tokens,
        timeout_seconds=spec.timeout_seconds,
    )
    return {"name": spec.name, "model": model, "output": output, "status": "success"}, records


def _write_audit(path: Path | None, payload: Any) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run_stage(
    stage: StageSpec,
    experts: dict[str, ExpertSpec],
    base_context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    outputs: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    async def execute(name: str, context: dict[str, Any]) -> None:
        try:
            result, records = await _run_expert(experts[name], context)
            outputs.append(result)
            calls.extend(records)
        except Exception as exc:
            failure = {"name": name, "model": experts[name].model, "status": "failure", "error": str(exc)[-5000:]}
            failures.append(failure)
            try:
                decoded = json.loads(str(exc))
                if isinstance(decoded, dict):
                    calls.append(decoded)
            except json.JSONDecodeError:
                pass

    if stage.mode == "parallel":
        await asyncio.gather(*(execute(name, dict(base_context)) for name in stage.members))
    else:
        for name in stage.members:
            context = dict(base_context)
            if outputs:
                context["previous_experts_in_this_stage"] = outputs
            await execute(name, context)
            if failures and stage.failure_policy == "fail_fast":
                break

    if failures and (stage.failure_policy == "fail_fast" or len(outputs) < stage.minimum_successful_members):
        raise StageExecutionError(stage.stage_id, failures, len(outputs))
    return outputs, failures, calls


async def run_dynamic_team(
    plan_payload: dict[str, Any],
    *,
    audit_dir: Path | None = None,
    budget_preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = validate_execution_plan(plan_payload)
    experts = _expert_map(plan)
    stage_outputs: dict[str, list[dict[str, Any]]] = {}
    stage_failures: dict[str, list[dict[str, Any]]] = {}
    model_calls: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []

    try:
        for stage in plan.stages:
            context = _source_payload(plan.task, stage_outputs, stage.input_from)
            started = time.monotonic()
            outputs, failures, calls = await _run_stage(stage, experts, context)
            stage_outputs[stage.stage_id] = outputs
            stage_failures[stage.stage_id] = failures
            model_calls.extend(calls)
            trace.append(
                {
                    "stage_id": stage.stage_id,
                    "mode": stage.mode,
                    "success_count": len(outputs),
                    "failure_count": len(failures),
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            _write_audit(audit_dir / "partial_execution.json" if audit_dir else None, {
                "stage_outputs": stage_outputs,
                "stage_failures": stage_failures,
                "model_calls": model_calls,
                "trace": trace,
            })
    except Exception:
        _write_audit(audit_dir / "model_calls.json" if audit_dir else None, model_calls)
        _write_audit(audit_dir / "execution_trace.json" if audit_dir else None, trace)
        raise

    red_team_output: str | None = None
    if plan.red_team.enabled:
        red_payload = {
            "original_task": plan.task,
            "rationale": plan.rationale,
            "stage_outputs": stage_outputs,
            "stage_failures": stage_failures,
            "instruction": "Challenge unsupported assumptions, contradictions, missing evidence, failure modes, and overconfidence.",
        }
        model, red_team_output, calls = await _run_model_agent(
            name=plan.red_team.name,
            role="red_team",
            instructions=plan.red_team.instructions,
            models=(plan.red_team.model, *plan.red_team.fallback_models),
            prompt=json.dumps(red_payload, ensure_ascii=False),
            max_completion_tokens=plan.red_team.max_completion_tokens,
            timeout_seconds=plan.red_team.timeout_seconds,
        )
        model_calls.extend(calls)
        trace.append({"stage_id": "red_team", "model": model, "status": "success"})

    final_answer: str | None = None
    judge_model: str | None = None
    if plan.judge.enabled:
        judge_payload = {
            "original_task": plan.task,
            "rationale": plan.rationale,
            "stage_outputs": stage_outputs,
            "stage_failures": stage_failures,
            "red_team_output": red_team_output,
            "instruction": "Synthesize and arbitrate by evidence, not majority voting. Preserve uncertainty and do not fabricate evidence.",
        }
        judge_model, final_answer, calls = await _run_model_agent(
            name=plan.judge.name,
            role="judge",
            instructions=plan.judge.instructions,
            models=(plan.judge.model, *plan.judge.fallback_models),
            prompt=json.dumps(judge_payload, ensure_ascii=False),
            max_completion_tokens=plan.judge.max_completion_tokens,
            timeout_seconds=plan.judge.timeout_seconds,
        )
        model_calls.extend(calls)
        trace.append({"stage_id": "judge", "model": judge_model, "status": "success"})

    _write_audit(audit_dir / "model_calls.json" if audit_dir else None, model_calls)
    _write_audit(audit_dir / "execution_trace.json" if audit_dir else None, trace)
    provenance = dict(plan.provenance)
    plan_source = str(provenance.get("effective_plan_source") or "web_gpt")
    primary_models = {expert.model.split("/", 1)[0] for expert in plan.experts}
    judge_family = plan.judge.model.split("/", 1)[0] if plan.judge.enabled and plan.judge.model else ""
    independence_warning = bool(judge_family and judge_family in primary_models)

    return {
        "plan_source": plan_source,
        "provenance": provenance,
        "plan": asdict(plan),
        "budget_preflight": budget_preflight,
        "stage_outputs": stage_outputs,
        "stage_failures": stage_failures,
        "red_team_output": red_team_output,
        "final_answer": final_answer,
        "judge_model": judge_model,
        "judge_model_family_independence_warning": independence_warning,
        "model_calls_file": "model_calls.json",
        "execution_trace_file": "execution_trace.json",
    }
