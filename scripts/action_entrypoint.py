from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any


def _safe_operation_id(value: str) -> str:
    cleaned = "".join(ch for ch in value.strip() if ch.isalnum() or ch in {"-", "_", "."})
    if not cleaned:
        raise ValueError("operation_id must contain at least one safe character")
    return cleaned[:120]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_minified_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


async def _execute_team(plan_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan_json is not valid JSON: {exc}") from exc

    # Plugin-only imports: this file remains usable by the permanent DeepSeek core
    # even when Microsoft Agent Framework and OpenRouter packages are absent.
    from expert_team.dynamic_team import run_dynamic_team, validate_execution_plan

    validate_execution_plan(payload)
    result = await run_dynamic_team(payload)

    # DeepSeek is the final publication gate, not another expert vote.
    from expert_team.deepseek_steward import run_deepseek_steward

    review_packet = {
        "request": "Audit the completed expert-team result before Web GPT publishes it.",
        "task": payload.get("task"),
        "deepseek_entry": payload.get("deepseek_entry"),
        "budget": payload.get("budget"),
        "execution_result": result,
        "constraints": [
            "Approve only when the result is internally consistent and supported by the supplied evidence.",
            "Do not treat process success as real-world correctness.",
            "Check budget compliance, missing evidence, contradictions, overconfidence, and incomplete stages.",
        ],
    }
    review = await run_deepseek_steward("REVIEW", json.dumps(review_packet, ensure_ascii=False))
    result["deepseek_final_review"] = review
    result["publication_status"] = "APPROVED" if review.get("status") == "APPROVE" else "BLOCKED"
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Action entrypoint for the dynamic expert system")
    parser.add_argument(
        "--operation",
        required=True,
        choices=("model_intelligence", "execute_team", "deepseek_steward"),
    )
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--plan-json", default="{}")
    parser.add_argument("--ranking-limit", type=int, default=20)
    parser.add_argument("--steward-mode", choices=("ASSIST", "REVIEW", "REPAIR"), default="ASSIST")
    parser.add_argument("--support-packet-json", default="{}")
    args = parser.parse_args()

    operation_id = _safe_operation_id(args.operation_id)
    output_dir = Path("artifacts") / operation_id
    metadata = {
        "operation_id": operation_id,
        "operation": args.operation,
        "status": "running",
        "active_plugin": "expert-team" if args.operation in {"model_intelligence", "execute_team"} else None,
    }
    if args.operation == "deepseek_steward":
        metadata["steward_mode"] = args.steward_mode
    _write_json(output_dir / "metadata.json", metadata)

    try:
        if args.operation == "model_intelligence":
            from expert_team.model_intelligence import (
                build_compact_model_intelligence_snapshot,
                build_model_intelligence_snapshot,
            )

            result = build_model_intelligence_snapshot(limit_per_ranking=args.ranking_limit)
            result_path = output_dir / "model_intelligence.json"
            gpt_path = output_dir / "model_intelligence_gpt.json"
            _write_minified_json(gpt_path, build_compact_model_intelligence_snapshot(result))
            metadata["readable_result_file"] = gpt_path.name
        elif args.operation == "execute_team":
            result = await _execute_team(args.plan_json)
            result_path = output_dir / "expert_team_result.json"
            metadata["readable_result_file"] = result_path.name
            metadata["publication_status"] = result.get("publication_status")
            metadata["deepseek_review_status"] = result.get("deepseek_final_review", {}).get("status")
        else:
            from expert_team.deepseek_steward import run_deepseek_steward

            result = await run_deepseek_steward(args.steward_mode, args.support_packet_json)
            result_path = output_dir / "deepseek_steward_result.json"
            metadata["readable_result_file"] = result_path.name
            metadata["steward_decision"] = result.get("decision") or result.get("status")
            metadata["steward_resume"] = result.get("resume") or result.get("status")

        _write_json(result_path, result)
        metadata.update({"status": "success", "result_file": result_path.name})
        _write_json(output_dir / "metadata.json", metadata)
        print(json.dumps(metadata, ensure_ascii=False))
    except Exception as exc:
        metadata.update({"status": "failure", "error_type": type(exc).__name__, "error": str(exc)})
        _write_json(output_dir / "metadata.json", metadata)
        print(json.dumps(metadata, ensure_ascii=False))
        raise


if __name__ == "__main__":
    asyncio.run(main())
