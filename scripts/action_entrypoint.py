from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from expert_team.budget import preflight_execution_plan
from expert_team.deepseek_steward import run_deepseek_steward
from expert_team.dynamic_team import run_dynamic_team, validate_execution_plan
from expert_team.model_intelligence import (
    build_compact_model_intelligence_snapshot,
    build_model_intelligence_snapshot,
)


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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _execute_team(plan_json: str, output_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("plan_json must contain a JSON object")

    validate_execution_plan(payload)
    preflight = preflight_execution_plan(payload)
    _write_json(output_dir / "cost_preflight.json", preflight)

    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
        payload["provenance"] = provenance
    provenance.setdefault("original_plan_source", "web_gpt")
    provenance.setdefault("effective_plan_source", "web_gpt")
    provenance.setdefault("original_plan_sha256", _sha256_text(plan_json))
    _write_json(output_dir / "effective_execution_plan.json", payload)

    return await run_dynamic_team(payload, audit_dir=output_dir, budget_preflight=preflight)


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
    parser.add_argument("--steward-mode", choices=("ASSIST", "REPAIR"), default="ASSIST")
    parser.add_argument("--support-packet-json", default="{}")
    args = parser.parse_args()

    operation_id = _safe_operation_id(args.operation_id)
    output_dir = Path("artifacts") / operation_id
    metadata = {"operation_id": operation_id, "operation": args.operation, "status": "running"}
    if args.operation == "deepseek_steward":
        metadata["steward_mode"] = args.steward_mode
    _write_json(output_dir / "metadata.json", metadata)

    try:
        if args.operation == "model_intelligence":
            result = build_model_intelligence_snapshot(limit_per_ranking=args.ranking_limit)
            result_path = output_dir / "model_intelligence.json"
            gpt_path = output_dir / "model_intelligence_gpt.json"
            _write_minified_json(gpt_path, build_compact_model_intelligence_snapshot(result))
            metadata["readable_result_file"] = gpt_path.name
        elif args.operation == "execute_team":
            result = await _execute_team(args.plan_json, output_dir)
            result_path = output_dir / "expert_team_result.json"
            metadata["readable_result_file"] = result_path.name
            metadata["plan_source"] = result.get("plan_source")
            metadata["effective_plan_sha256"] = _sha256_text(
                json.dumps(result.get("plan") or {}, ensure_ascii=False, sort_keys=True)
            )
        else:
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
