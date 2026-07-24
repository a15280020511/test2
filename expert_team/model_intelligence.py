"""OpenRouter model-intelligence layer for Web GPT planning.

This module is deliberately read-only. It exposes current OpenRouter model metadata,
rankings and benchmark signals so Web GPT can choose task-specific models before it
submits an execution plan. Expert inference itself remains in Microsoft Agent
Framework via ``openrouter_client.py``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from openrouter import OpenRouter

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
RANKING_SORTS = (
    "intelligence-high-to-low",
    "coding-high-to-low",
    "agentic-high-to-low",
    "design-arena-elo-high-to-low",
    "top-weekly",
    "most-popular",
    "throughput-high-to-low",
    "latency-low-to-high",
    "pricing-low-to-high",
    "context-high-to-low",
    "newest",
)

# The complete model-intelligence snapshot is kept in the audit artifact. GPT Actions
# only receives a bounded planning snapshot to avoid ResponseTooLargeError.
GPT_RANKING_LIMIT = 6
GPT_SELECTION_PARAMETERS = frozenset(
    {
        "tools",
        "tool_choice",
        "structured_outputs",
        "response_format",
        "reasoning",
        "include_reasoning",
    }
)


def _api_key() -> str:
    value = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not value:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    return value


def _plain(value: Any) -> Any:
    """Convert generated SDK/Pydantic response objects into JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _plain(method())
            except TypeError:
                continue
    if hasattr(value, "__dict__"):
        return {
            key: _plain(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return str(value)


def _unwrap_models_response(response: Any) -> dict[str, Any]:
    """Unwrap OpenRouter SDK GetModelsResponse.result into its ModelsListResponse."""
    if response is None:
        raise RuntimeError("OpenRouter SDK returned no response")

    result = getattr(response, "result", None)
    if result is not None:
        payload = _plain(result)
    else:
        payload = _plain(response)
        if isinstance(payload, dict) and "result" in payload:
            payload = payload["result"]

    if not isinstance(payload, dict):
        raise RuntimeError("OpenRouter SDK returned an unexpected models response")
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("OpenRouter SDK models response has no valid data list")
    return payload


def _rest_get(path: str) -> dict[str, Any]:
    request = Request(
        f"{OPENROUTER_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Accept": "application/json",
            "User-Agent": "test2-expert-team/1.0",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed trusted host
        return json.loads(response.read().decode("utf-8"))


def fetch_catalog_via_sdk() -> dict[str, Any]:
    """Read the canonical model catalog through the official OpenRouter SDK."""
    with OpenRouter(api_key=_api_key()) as client:
        response = client.models.list(limit=1000)
    return _unwrap_models_response(response)


def fetch_ranked_models(sort: str, limit: int = 20) -> list[dict[str, Any]]:
    """Fetch one server-side ranking through the official OpenRouter SDK."""
    if sort not in RANKING_SORTS:
        raise ValueError(f"unsupported OpenRouter ranking sort: {sort}")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    with OpenRouter(api_key=_api_key()) as client:
        response = client.models.list(sort=sort, limit=limit)
    payload = _unwrap_models_response(response)
    return payload["data"][:limit]


def fetch_benchmarks() -> dict[str, Any]:
    """Fetch OpenRouter's unified benchmark feed."""
    payload = _rest_get("/benchmarks")
    if not isinstance(payload.get("data", []), list):
        raise RuntimeError("OpenRouter /benchmarks returned an invalid data field")
    return payload


def build_model_intelligence_snapshot(limit_per_ranking: int = 20) -> dict[str, Any]:
    """Build the full current OpenRouter intelligence snapshot for audit artifacts."""
    catalog = fetch_catalog_via_sdk()
    rankings = {
        sort: fetch_ranked_models(sort, limit=limit_per_ranking)
        for sort in RANKING_SORTS
    }
    benchmarks = fetch_benchmarks()
    return {
        "schema_version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "OpenRouter official Python SDK + official benchmark API",
        "selection_rule": (
            "Use these signals as decision evidence, not as a universal winner list. "
            "Web GPT must choose models according to the concrete task, role, budget, "
            "latency, context, supported parameters and uncertainty."
        ),
        "catalog": catalog,
        "rankings": rankings,
        "benchmarks": benchmarks,
    }


def build_compact_model_intelligence_snapshot(
    snapshot: dict[str, Any],
    limit_per_ranking: int = GPT_RANKING_LIMIT,
) -> dict[str, Any]:
    """Create a response-size-safe selection snapshot for Web GPT.

    The full catalog, raw benchmark feed, and full ranking rows remain in the audit
    artifact. The directly readable form keeps only bounded top candidates per ranking
    and the minimum metadata required for task-specific quality/cost/speed selection.
    """
    if limit_per_ranking < 1:
        raise ValueError("limit_per_ranking must be >= 1")

    ranking_ids: dict[str, list[str]] = {}
    models: dict[str, dict[str, Any]] = {}

    raw_rankings = snapshot.get("rankings", {})
    if not isinstance(raw_rankings, dict):
        raise ValueError("snapshot.rankings must be an object")

    for sort, items in raw_rankings.items():
        if not isinstance(items, list):
            continue
        ranking_ids[str(sort)] = []
        for item in items[:limit_per_ranking]:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            ranking_ids[str(sort)].append(model_id)
            if model_id in models:
                continue

            architecture = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
            pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
            raw_parameters = item.get("supported_parameters")
            supported_parameters = raw_parameters if isinstance(raw_parameters, list) else []
            selected_parameters = [
                str(parameter)
                for parameter in supported_parameters
                if str(parameter) in GPT_SELECTION_PARAMETERS
            ]

            models[model_id] = {
                "context_length": item.get("context_length"),
                "pricing": {
                    "prompt": pricing.get("prompt"),
                    "completion": pricing.get("completion"),
                },
                "supported_parameters": selected_parameters,
                "input_modalities": architecture.get("input_modalities"),
                "reasoning": bool(item.get("reasoning")) or "reasoning" in selected_parameters,
            }

    return {
        "schema_version": "3",
        "generated_at": snapshot.get("generated_at"),
        "source": snapshot.get("source"),
        "selection_rule": snapshot.get("selection_rule"),
        "ranking_limit": limit_per_ranking,
        "rankings": ranking_ids,
        "models": models,
    }


def write_model_intelligence_snapshot(
    output_path: str | Path,
    limit_per_ranking: int = 20,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = build_model_intelligence_snapshot(limit_per_ranking=limit_per_ranking)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
