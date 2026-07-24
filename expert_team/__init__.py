"""DeepSeek control core with optional task-scoped expert-team plugs.

DeepSeek Steward is permanently available through the Python standard-library control
core. Microsoft Agent Framework and OpenRouter capabilities are imported only while the
``expert-team`` plug is installed for the current task.
"""

from __future__ import annotations

from .deepseek_steward import DEFAULT_STEWARD_MODEL, run_deepseek_steward

_DYNAMIC_EXPORTS = {
    "ExecutionPlan",
    "ExpertSpec",
    "OptionalAgentSpec",
    "StageSpec",
    "BudgetSpec",
    "DeepSeekEntrySpec",
    "run_dynamic_team",
    "validate_execution_plan",
}
_MODEL_EXPORTS = {
    "RANKING_SORTS",
    "build_model_intelligence_snapshot",
    "fetch_benchmarks",
    "fetch_catalog_via_sdk",
    "fetch_ranked_models",
    "write_model_intelligence_snapshot",
}


def __getattr__(name: str):
    if name in _DYNAMIC_EXPORTS:
        from . import dynamic_team

        return getattr(dynamic_team, name)
    if name in _MODEL_EXPORTS:
        from . import model_intelligence

        return getattr(model_intelligence, name)
    raise AttributeError(name)


__all__ = [
    "DEFAULT_STEWARD_MODEL",
    "run_deepseek_steward",
    *_DYNAMIC_EXPORTS,
    *_MODEL_EXPORTS,
]
