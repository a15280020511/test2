"""Dynamic expert-team package.

Web GPT plans the team; Microsoft Agent Framework executes it; OpenRouter model
intelligence is exposed read-only for task-specific model selection.
"""

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
