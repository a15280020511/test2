from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from scripts import cross_repo_control as core

ENTRY_DECISIONS = {"READY", "COLLECT", "REPLAN", "REPAIR", "STOP"}


class PriorityControlError(RuntimeError):
    """Raised when the mandatory DeepSeek-first gate cannot progress safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--revision", default="1")
    parser.add_argument("--task-objective", default="")
    parser.add_argument("--task-json", default="{}")
    parser.add_argument("--evidence-json", default="{}")
    parser.add_argument("--execution-plan-json", default="{}")
    parser.add_argument("--target-run-id", default="")
    parser.add_argument("--support-context-json", default="{}")
    parser.add_argument("--target-repo", default=core.TARGET_REPOSITORY)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_object(raw: str, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise PriorityControlError(f"{field} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PriorityControlError(f"{field} must be a JSON object")
    return value


def parse_deepseek_content(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PriorityControlError("DeepSeek entry response requires choices[]")
    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise PriorityControlError("DeepSeek entry response was empty")
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PriorityControlError(f"DeepSeek entry response was not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise PriorityControlError("DeepSeek entry response must be an object")
    if result.get("decision") not in ENTRY_DECISIONS:
        raise PriorityControlError(f"invalid DeepSeek entry decision: {result.get('decision')!r}")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise PriorityControlError("DeepSeek entry response requires summary")
    for key in ("required_evidence", "assumptions", "risks", "required_actions"):
        value = result.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise PriorityControlError(f"DeepSeek entry response {key} must be a string list")
        result[key] = value
    plan = result.get("temporary_execution_plan")
    if plan is not None:
        if not isinstance(plan, dict):
            raise PriorityControlError("temporary_execution_plan must be an object or null")
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PriorityControlError("temporary_execution_plan requires non-empty steps[]")
    return result


def mandatory_entry_gate(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if not args.task_objective.strip():
        raise PriorityControlError("task_objective is required before the DeepSeek entry gate")
    task = parse_object(args.task_json, field="task_json")
    evidence = parse_object(args.evidence_json, field="evidence_json")
    supplied_plan = parse_object(args.execution_plan_json, field="execution_plan_json")
    support_context = parse_object(args.support_context_json, field="support_context_json")

    deepseek = core.DeepSeekClient(os.environ.get("DEEPSEEK_API_KEY", ""))
    packet = {
        "control_repository": core.CONTROL_REPOSITORY,
        "target_repository": core.TARGET_REPOSITORY,
        "operation": args.operation.upper(),
        "task_id": args.task_id,
        "revision": core.parse_revision(args.revision),
        "task_objective": args.task_objective,
        "task": task,
        "evidence": evidence,
        "candidate_execution_plan": supplied_plan,
        "support_context": support_context,
        "required_outcome": (
            "Act before any production dispatch. Return READY only when evidence and a complete executable "
            "temporary plan are adequate. Return COLLECT for missing evidence, REPLAN for a corrected plan, "
            "REPAIR for a confirmed repository defect, or STOP for an external or unsafe blocker."
        ),
    }
    prompt = (
        "You are the mandatory highest-priority independent DeepSeek entry authority for "
        "a15280020511/test. No production workflow may start before this decision. "
        "Return JSON only with decision READY, COLLECT, REPLAN, REPAIR, or STOP; summary; "
        "temporary_execution_plan object or null; required_evidence[]; assumptions[]; risks[]; "
        "required_actions[]; and confidence. Distinguish facts, assumptions, supplied evidence, "
        "and uncertainty. Never fabricate data, logs, repository state, or execution success."
    )
    response = deepseek._json_request(  # noqa: SLF001 - shared official client and model resolution
        "https://api.deepseek.com/chat/completions",
        payload={
            "model": deepseek.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(packet, ensure_ascii=False, sort_keys=True)},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        },
    )
    result = parse_deepseek_content(response)
    result["runtime_model"] = deepseek.model

    effective_plan = result.get("temporary_execution_plan")
    if effective_plan is None:
        effective_plan = supplied_plan
    if result["decision"] == "READY":
        if not isinstance(effective_plan, dict):
            raise PriorityControlError("READY requires an effective execution plan object")
        steps = effective_plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PriorityControlError("READY requires an effective plan with non-empty steps[]")
    effective_plan_json = json.dumps(
        effective_plan if isinstance(effective_plan, dict) else supplied_plan,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return result, effective_plan_json


def redact_ticket(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    ticket = clean.pop("control_ticket", None)
    if isinstance(ticket, dict):
        canonical = json.dumps(ticket, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        clean["control_ticket_receipt"] = {
            "ticket_sha256": core.sha256(canonical),
            "issuer": ticket.get("issuer"),
            "target_repo": ticket.get("target_repo"),
            "task_id": ticket.get("task_id"),
            "revision": ticket.get("revision"),
            "issued_at": ticket.get("issued_at"),
            "expires_at": ticket.get("expires_at"),
        }
    return clean


def controller_argv(
    args: argparse.Namespace,
    *,
    operation: str | None = None,
    target_run_id: str | None = None,
    support_context_json: str | None = None,
    execution_plan_json: str | None = None,
    output_dir: Path | None = None,
) -> list[str]:
    return [
        "cross_repo_control.py",
        "--operation",
        operation or args.operation,
        "--task-id",
        args.task_id,
        "--revision",
        args.revision,
        "--task-objective",
        args.task_objective,
        "--task-json",
        args.task_json,
        "--evidence-json",
        args.evidence_json,
        "--execution-plan-json",
        execution_plan_json if execution_plan_json is not None else args.execution_plan_json,
        "--target-run-id",
        target_run_id if target_run_id is not None else args.target_run_id,
        "--support-context-json",
        support_context_json if support_context_json is not None else args.support_context_json,
        "--target-repo",
        args.target_repo,
        "--output-dir",
        str(output_dir or args.output_dir),
    ]


def invoke_controller(
    args: argparse.Namespace,
    *,
    operation: str | None = None,
    target_run_id: str | None = None,
    support_context_json: str | None = None,
    execution_plan_json: str | None = None,
    output_dir: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    destination = output_dir or args.output_dir
    old_argv = sys.argv
    captured = io.StringIO()
    try:
        sys.argv = controller_argv(
            args,
            operation=operation,
            target_run_id=target_run_id,
            support_context_json=support_context_json,
            execution_plan_json=execution_plan_json,
            output_dir=destination,
        )
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            return_code = core.main()
    finally:
        sys.argv = old_argv

    result_path = destination / "control-result.json"
    if not result_path.exists():
        raise PriorityControlError("underlying controller produced no control-result.json")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise PriorityControlError("underlying controller result must be an object")
    result = redact_ticket(result)
    write_json(result_path, result)
    return return_code, result


def automatic_repair(
    args: argparse.Namespace,
    *,
    source_result: dict[str, Any],
    entry_gate: dict[str, Any] | None,
) -> dict[str, Any] | None:
    diagnosis = source_result.get("deepseek_diagnosis")
    if not isinstance(diagnosis, dict) or diagnosis.get("decision") != "REPAIR":
        return None
    run_id = source_result.get("target_run_id")
    if not isinstance(run_id, int):
        return None

    prior_context = parse_object(args.support_context_json, field="support_context_json")
    repair_context = {
        **prior_context,
        "automatic_repair_requested": True,
        "entry_gate": entry_gate,
        "failed_control_result": source_result,
        "diagnosis": diagnosis,
    }
    repair_dir = args.output_dir / "automatic-repair"
    _, repair_result = invoke_controller(
        args,
        operation="REPAIR",
        target_run_id=str(run_id),
        support_context_json=json.dumps(repair_context, ensure_ascii=False, separators=(",", ":")),
        output_dir=repair_dir,
    )
    return repair_result


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    operation = args.operation.upper()
    entry_gate: dict[str, Any] | None = None
    effective_plan_json = args.execution_plan_json

    try:
        if operation in {"START", "RESTART"}:
            entry_gate, effective_plan_json = mandatory_entry_gate(args)
            write_json(args.output_dir / "entry-gate.json", entry_gate)
            if entry_gate["decision"] != "READY":
                blocked = {
                    "status": "ENTRY_BLOCKED",
                    "operation": operation,
                    "task_id": args.task_id,
                    "revision": core.parse_revision(args.revision),
                    "target_repo": args.target_repo,
                    "entry_gate": entry_gate,
                    "generated_at": int(time.time()),
                }
                write_json(args.output_dir / "control-result.json", blocked)
                print(json.dumps(blocked, ensure_ascii=False, indent=2, sort_keys=True))
                return 2

        return_code, result = invoke_controller(
            args,
            execution_plan_json=effective_plan_json,
        )
        if entry_gate is not None:
            result["entry_gate"] = entry_gate

        repair_result = automatic_repair(
            args,
            source_result=result,
            entry_gate=entry_gate,
        )
        if repair_result is not None:
            result["automatic_repair"] = repair_result
            if repair_result.get("status") == "REPAIR_PR_CREATED":
                result["status"] = "TARGET_FAILED_REPAIR_PR_CREATED"

        write_json(args.output_dir / "control-result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return return_code
    except Exception as exc:  # noqa: BLE001
        failure = {
            "status": "CONTROL_GATE_FAILED",
            "operation": operation,
            "task_id": args.task_id,
            "revision": args.revision,
            "target_repo": args.target_repo,
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": int(time.time()),
        }
        write_json(args.output_dir / "control-result.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
