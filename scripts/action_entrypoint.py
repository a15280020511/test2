from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

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


async def _execute_team(plan_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan_json is not valid JSON: {exc}") from exc

    # Fail before any paid model call if the Web-GPT-authored form is invalid.
    validate_execution_plan(payload)
    return await run_dynamic_team(payload)


async def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Action entrypoint for the dynamic expert team")
    parser.add_argument("--operation", required=True, choices=("model_intelligence", "execute_team"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--plan-json", default="{}")
    parser.add_argument("--ranking-limit", type=int, default=20)
    args = parser.parse_args()

    operation_id = _safe_operation_id(args.operation_id)
    output_dir = Path("artifacts") / operation_id
    metadata = {
        "operation_id": operation_id,
        "operation": args.operation,
        "status": "running",
    }
    _write_json(output_dir / "metadata.json", metadata)

    try:
        if args.operation == "model_intelligence":
            result = build_model_intelligence_snapshot(limit_per_ranking=args.ranking_limit)
            result_path = output_dir / "model_intelligence.json"
            compact_path = output_dir / "model_intelligence_compact.json"
            _write_json(compact_path, build_compact_model_intelligence_snapshot(result))
            metadata["readable_result_file"] = compact_path.name
        else:
            result = await _execute_team(args.plan_json)
            result_path = output_dir / "expert_team_result.json"
            metadata["readable_result_file"] = result_path.name

        _write_json(result_path, result)
        metadata.update({"status": "success", "result_file": result_path.name})
        _write_json(output_dir / "metadata.json", metadata)
        print(json.dumps(metadata, ensure_ascii=False))
    except Exception as exc:
        metadata.update(
            {
                "status": "failure",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        _write_json(output_dir / "metadata.json", metadata)
        print(json.dumps(metadata, ensure_ascii=False))
        raise


if __name__ == "__main__":
    asyncio.run(main())
