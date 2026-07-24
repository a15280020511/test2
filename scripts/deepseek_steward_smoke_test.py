from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from expert_team.deepseek_official import generate_official_deepseek_json

ARTIFACT = Path("artifacts/deepseek_steward_smoke_result.json")


def _write(payload: dict) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def main() -> None:
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        _write({"status": "skipped_no_key", "provider": "DeepSeek official API"})
        print("DEEPSEEK_STEWARD_SMOKE_SKIPPED: DEEPSEEK_API_KEY is not configured")
        return

    model, output = await generate_official_deepseek_json(
        "Return one JSON object exactly matching the requested schema.",
        {
            "request": "Connectivity smoke test",
            "required_json": {"status": "STEWARD_OK"},
        },
    )
    payload = json.loads(output)
    if payload.get("status") != "STEWARD_OK":
        raise RuntimeError(f"Unexpected official DeepSeek Steward smoke output: {output[:500]}")

    result = {
        "status": "passed",
        "provider": "DeepSeek official API",
        "base_url": "https://api.deepseek.com",
        "model": model,
        "output": payload,
    }
    _write(result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
