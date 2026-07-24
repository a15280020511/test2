"""Dynamic expert-team package.

Web GPT plans user tasks; DeepSeek Steward manages repository service and repair;
Microsoft Agent Framework executes expert teams; OpenRouter supplies model inference
and read-only model intelligence.
"""

from .deepseek_steward import DEFAULT_STEWARD_MODEL, run_deepseek_steward
from .dynamic_team import (
    ExecutionPlan,
    ExpertSpec,
    OptionalAgentSpec,
    StageSpec,
    run_dynamic_team,
    validate_execution_plan,
)
from .model_intelligence import (
    RANKING_SORTS,
    build_model_intelligence_snapshot,
    fetch_benchmarks,
    fetch_catalog_via_sdk,
    fetch_ranked_models,
    write_model_intelligence_snapshot,
)

__all__ = [
    "DEFAULT_STEWARD_MODEL",
    "run_deepseek_steward",
    "ExecutionPlan",
    "ExpertSpec",
    "OptionalAgentSpec",
    "StageSpec",
    "run_dynamic_team",
    "validate_execution_plan",
    "RANKING_SORTS",
    "build_model_intelligence_snapshot",
    "fetch_benchmarks",
    "fetch_catalog_via_sdk",
    "fetch_ranked_models",
    "write_model_intelligence_snapshot",
]
