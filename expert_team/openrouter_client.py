"""Minimal OpenRouter adapter for Microsoft Agent Framework.

The project intentionally uses Agent Framework's OpenAI-compatible client
instead of adding a second OpenRouter SDK dependency.
"""

from __future__ import annotations

import os

from agent_framework.openai import OpenAIChatCompletionClient

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def create_model_client(model: str | None = None) -> OpenAIChatCompletionClient:
    """Create an Agent Framework client routed through OpenRouter.

    Required environment variable:
        OPENROUTER_API_KEY

    Model selection:
        Pass ``model`` explicitly, or set ``OPENROUTER_MODEL``.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    selected_model = model or os.getenv("OPENROUTER_MODEL")
    if not selected_model:
        raise RuntimeError("No model selected; pass model or set OPENROUTER_MODEL")

    return OpenAIChatCompletionClient(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        model=selected_model,
    )
