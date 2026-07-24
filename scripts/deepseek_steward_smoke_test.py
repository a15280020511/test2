from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from agent_framework import Agent

from expert_team.deepseek_steward import DEFAULT_STEWARD_MODEL
from expert_team.openrouter_client import create_model_client

ARTIFACT = Path("artifacts/deepseek_steward_smoke_result.json")


def _write(payload: dict) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def main() -> None:
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        _write({"status": "skipped_no_key"})
        print("DEEPSEEK_STEWARD_SMOKE_SKIPPED: OPENROUTER_API_KEY is not configured")
        return

    model = os.getenv("DEEPSEEK_STEWARD_MODEL", DEFAULT_STEWARD_MODEL)
    agent = Agent(
        name="deepseek_steward_smoke",
        client=create_model_client(model),
        instructions="Return exactly STEWARD_OK and nothing else.",
    )
    response = await agent.run("Return exactly STEWARD_OK and nothing else.")
    output = response.text.strip()
    if "STEWARD_OK" not in output:
        raise RuntimeError(f"Unexpected DeepSeek Steward smoke output: {output[:200]}")

    payload = {
        "status": "passed",
        "model": model,
        "output": output,
    }
    _write(payload)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
