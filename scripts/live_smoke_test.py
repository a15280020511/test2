from __future__ import annotations

import asyncio
import json
import os
import traceback
from pathlib import Path

from expert_team.dynamic_team import run_dynamic_team
from expert_team.model_intelligence import fetch_benchmarks, fetch_catalog_via_sdk, fetch_ranked_models

ARTIFACT = Path("artifacts/live_smoke_result.json")
MAX_INFERENCE_ATTEMPTS = 2


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
        "version": "2",
        "selection_policy": "CI-only smoke fixture; production uses the authoritative schema policy.",
        "task": "Return exactly the text SMOKE_OK and nothing else.",
        "rationale": "Single-agent connectivity smoke test with one bounded retry for an empty free-route response.",
        "deepseek_entry": {
            "status": "READY",
            "operation_id": "ci-smoke-fixture",
            "budget_options_presented": True,
        },
        "budget": {
            "approval_status": "approved_by_user",
            "tier": "economy",
            "currency": "USD",
            "max_cost_usd": 0.02,
            "estimated_cost_usd": {"low": 0.0, "high": 0.02},
            "max_model_calls": 2,
            "max_output_tokens_per_call": 128,
            "approval_reference": "Repository CI smoke fixture; not a production user task.",
        },
        "experts": [
            {
                "name": "smoke_expert",
                "mission": "Verify Agent Framework can reach OpenRouter.",
                "instructions": "Return exactly SMOKE_OK and nothing else.",
                "model": smoke_model,
            }
        ],
        "stages": [
            {
                "id": "smoke",
                "mode": "sequential",
                "members": ["smoke_expert"],
                "input_from": ["task"],
            }
        ],
        "red_team": {"enabled": False, "name": "red_team", "model": "", "instructions": ""},
        "judge": {"enabled": False, "name": "final_judge", "model": "", "instructions": ""},
    }

    result = None
    output = ""
    attempts: list[dict[str, object]] = []
    for attempt in range(1, MAX_INFERENCE_ATTEMPTS + 1):
        result = await run_dynamic_team(plan)
        output = result["stage_outputs"]["smoke"][0]["output"].strip()
        attempts.append({"attempt": attempt, "nonempty": bool(output), "matched": "SMOKE_OK" in output})
        if "SMOKE_OK" in output:
            break
        if attempt < MAX_INFERENCE_ATTEMPTS:
            await asyncio.sleep(2)
    else:
        raise RuntimeError(f"Unexpected live inference output after {MAX_INFERENCE_ATTEMPTS} attempts: {output[:200]}")

    assert result is not None
    payload = {
        "status": "passed",
        "catalog_model_count": len(catalog_data),
        "intelligence_top3": [item.get("id") for item in intelligence_ranking],
        "benchmark_count": len(benchmarks.get("data", [])),
        "smoke_model": smoke_model,
        "agent_framework_output": output,
        "inference_attempts": attempts,
        "budget_enforcement": result.get("budget_enforcement"),
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
