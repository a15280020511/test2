"""Official DeepSeek API client used exclusively by DeepSeek Steward.

The expert marketplace remains on OpenRouter. DeepSeek Steward is intentionally
isolated from OpenRouter and talks directly to https://api.deepseek.com with the
DEEPSEEK_API_KEY repository secret.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEFAULT_STEWARD_MODEL = "deepseek-v4-pro"
DEFAULT_MAX_TOKENS = 65536
_MODEL_VERSION_RE = re.compile(r"^deepseek-v(?P<version>\d+(?:\.\d+)*)(?:-(?P<tier>[a-z0-9-]+))?$")


def _api_key() -> str:
    value = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not value:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. DeepSeek Steward uses the official DeepSeek API "
            "and never falls back to OpenRouter."
        )
    return value


def _max_tokens() -> int:
    raw = os.getenv("DEEPSEEK_STEWARD_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("DEEPSEEK_STEWARD_MAX_TOKENS must be an integer") from exc
    if value < 1024:
        raise RuntimeError("DEEPSEEK_STEWARD_MAX_TOKENS must be >= 1024")
    return value


def _request_json(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: int = 300) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{DEEPSEEK_API_BASE}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "test2-deepseek-steward/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed trusted official host
            parsed = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
        except Exception:  # pragma: no cover - defensive error reporting only
            detail = ""
        raise RuntimeError(f"DeepSeek official API HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"DeepSeek official API connection failed: {exc.reason}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("DeepSeek official API returned a non-object JSON response")
    return parsed


def list_official_models() -> list[str]:
    """Return model IDs currently exposed by DeepSeek's official /models endpoint."""
    payload = _request_json("GET", "/models", timeout=60)
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("DeepSeek official /models response has no valid data list")
    models = [str(item.get("id")).strip() for item in data if isinstance(item, dict) and item.get("id")]
    if not models:
        raise RuntimeError("DeepSeek official /models returned no model IDs")
    return models


def _strength_key(model_id: str) -> tuple[int, int, int, int, str]:
    """Best-effort ordering for official DeepSeek model IDs.

    Higher version wins first; within a version, capability-oriented tiers outrank
    speed-oriented tiers. The current official V4 pair therefore selects V4-Pro.
    """
    match = _MODEL_VERSION_RE.match(model_id)
    if not match:
        legacy_score = {
            "deepseek-reasoner": 200,
            "deepseek-chat": 100,
        }.get(model_id, 0)
        return (0, 0, 0, legacy_score, model_id)

    version_parts = [int(part) for part in match.group("version").split(".")[:3]]
    version_parts += [0] * (3 - len(version_parts))
    tier = (match.group("tier") or "").lower()
    tier_score = 250
    if "ultra" in tier:
        tier_score = 700
    elif "max" in tier:
        tier_score = 650
    elif "pro" in tier:
        tier_score = 600
    elif "reason" in tier:
        tier_score = 500
    elif "chat" in tier:
        tier_score = 300
    elif "flash" in tier:
        tier_score = 100
    return (version_parts[0], version_parts[1], version_parts[2], tier_score, model_id)


@lru_cache(maxsize=1)
def select_strongest_official_model() -> str:
    """Select the strongest available official DeepSeek model by default.

    DEEPSEEK_STEWARD_MODEL is an explicit operator override. Without an override,
    the official model list is inspected at runtime. If model discovery itself is
    temporarily unavailable, the current strongest official baseline V4-Pro is used.
    """
    override = os.getenv("DEEPSEEK_STEWARD_MODEL", "").strip()
    if override:
        return override

    try:
        models = [model for model in list_official_models() if model.startswith("deepseek-")]
        if models:
            return max(models, key=_strength_key)
    except RuntimeError:
        pass
    return DEFAULT_STEWARD_MODEL


def _generate_json_sync(system_prompt: str, payload: dict[str, Any]) -> tuple[str, str]:
    model = select_strongest_official_model()
    messages = [
        {
            "role": "system",
            "content": system_prompt
            + "\nYou must return one non-empty valid JSON object and no markdown fences.",
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]

    request_payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
        "max_tokens": _max_tokens(),
    }

    # DeepSeek documents that JSON Output can occasionally return empty content.
    # Retry once with an explicit reminder; never fall back to another provider.
    for attempt in range(2):
        response = _request_json("POST", "/chat/completions", request_payload, timeout=300)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("DeepSeek official API returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return model, content.strip()
        if attempt == 0:
            request_payload["messages"] = messages + [
                {
                    "role": "user",
                    "content": "Return the required non-empty JSON object now.",
                }
            ]

    raise RuntimeError("DeepSeek official API returned empty JSON content twice")


async def generate_official_deepseek_json(system_prompt: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Call the official DeepSeek API without blocking the async workflow."""
    return await asyncio.to_thread(_generate_json_sync, system_prompt, payload)
