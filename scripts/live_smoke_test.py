from __future__ import annotations

import asyncio
import json
import os
import traceback
from pathlib import Path

from expert_team.dynamic_team import run_dynamic_team
from expert_team.model_intelligence import (
    fetch_benchmarks,
    fetch_catalog_via_sdk,
    fetch_ranked_models,
)

ARTIFACT = Path("artifacts/live_smoke_result.json")
SELECTION_POLICY = (
    "默认采用质量约束下的动态最优组合：先保证任务所需质量，再在满足质量的候选模型中优化成本和速度；"
    "随着任务复杂度、风险、价值和不确定性提高，自动增加专家数量、模型多样性和红队强度；"
    "重大任务以能力优先，普通任务以性价比优先。不得固定专家数量、固定模型或固定工作流，必须具体问题具体分析。"
)


def _write(payload: dict) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run() -> None:
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        _write({"status": "skipped_no_key"})
        print("LIVE_SMOKE_SKIPPED: OPENROUTER_API_KEY is not configured")
        return

    catalog = fetch_catalog_via_sdk()
    catalog_data = catalog.get("data", []) if isinstance(catalog, dict) else []
    if not catalog_data:
        raise RuntimeError("OpenRouter SDK returned an empty model catalog")

    intelligence_ranking = fetch_ranked_models("intelligence-high-to-low", limit=3)
    if not intelligence_ranking:
        raise RuntimeError("OpenRouter intelligence ranking returned no models")

    benchmarks = fetch_benchmarks()
    if not isinstance(benchmarks.get("data", []), list):
        raise RuntimeError("OpenRouter benchmark response is malformed")

    smoke_model = os.getenv("OPENROUTER_SMOKE_MODEL", "openrouter/free")
    plan = {
        "version": "1",
        "selection_policy": SELECTION_POLICY,
        "task": "Return exactly the text SMOKE_OK and nothing else.",
        "rationale": "Use one low-cost model with the smallest hard output ceiling for a connectivity test.",
        "budget": {"max_total_usd": 0.05, "recovery_reserve_ratio": 0.0},
        "experts": [
            {
                "name": "smoke_expert",
                "mission": "Verify Agent Framework can reach OpenRouter.",
                "instructions": "Return exactly SMOKE_OK and nothing else.",
                "model": smoke_model,
                "max_completion_tokens": 64,
                "timeout_seconds": 120,
                "fallback_models": [],
            }
        ],
        "stages": [
            {
                "id": "smoke",
                "mode": "sequential",
                "members": ["smoke_expert"],
                "input_from": ["task"],
                "failure_policy": "fail_fast",
                "minimum_successful_members": 1,
            }
        ],
        "red_team": {
            "enabled": False,
            "name": "red_team",
            "model": "",
            "instructions": "",
        },
        "judge": {
            "enabled": False,
            "name": "final_judge",
            "model": "",
            "instructions": "",
        },
    }

    result = await run_dynamic_team(plan)
    output = result["stage_outputs"]["smoke"][0]["output"].strip()
    if "SMOKE_OK" not in output:
        raise RuntimeError(f"Unexpected live inference output: {output[:200]}")

    payload = {
        "status": "passed",
        "catalog_model_count": len(catalog_data),
        "intelligence_top3": [item.get("id") for item in intelligence_ranking],
        "benchmark_count": len(benchmarks.get("data", [])),
        "smoke_model": smoke_model,
        "agent_framework_output": output,
    }
    _write(payload)
    print(json.dumps(payload, ensure_ascii=False))


async def main() -> None:
    try:
        await _run()
    except Exception as exc:
        failure = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        _write(failure)
        print(json.dumps(failure, ensure_ascii=False))
        raise


if __name__ == "__main__":
    asyncio.run(main())
