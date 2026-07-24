from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import expert_team
from expert_team.dynamic_team import validate_execution_plan
from expert_team.model_intelligence import (
    GPT_RANKING_LIMIT,
    RANKING_SORTS,
    build_compact_model_intelligence_snapshot,
    build_model_intelligence_snapshot,
)

SELECTION_POLICY = (
    "DeepSeek ASSIST 必须作为最高优先级入口，先审计任务、插件需求和预算方案；网页 GPT 必须把经济、均衡、质量三档预算反馈给用户并取得明确选择。"
    "执行时先满足用户批准的质量和成本边界，再在边界内动态选择专家、模型和工作流。所有专业工具采用任务级临时插头，用时安装、结束销毁。"
    "重大任务能力优先，普通任务性价比优先，具体问题具体分析。"
)

VALID_PLAN = {
    "version": "2",
    "selection_policy": SELECTION_POLICY,
    "task": "Analyze a complex business decision.",
    "rationale": "DeepSeek reviewed the task; the user selected the balanced budget.",
    "deepseek_entry": {
        "status": "READY",
        "operation_id": "assist-budget-001",
        "budget_options_presented": True,
    },
    "budget": {
        "approval_status": "approved_by_user",
        "tier": "balanced",
        "currency": "USD",
        "max_cost_usd": 2.0,
        "estimated_cost_usd": {"low": 0.25, "high": 1.5},
        "max_model_calls": 4,
        "max_output_tokens_per_call": 1200,
        "approval_reference": "User selected balanced, maximum USD 2.00.",
    },
    "experts": [
        {
            "name": "market",
            "mission": "Assess market demand.",
            "instructions": "Separate facts from assumptions.",
            "model": "provider/model-a",
        },
        {
            "name": "finance",
            "mission": "Assess unit economics.",
            "instructions": "Stress-test key variables.",
            "model": "provider/model-b",
        },
    ],
    "stages": [
        {
            "id": "analysis",
            "mode": "parallel",
            "members": ["market", "finance"],
            "input_from": ["task"],
        }
    ],
    "red_team": {
        "enabled": True,
        "name": "red_team",
        "model": "provider/model-c",
        "instructions": "Attack unsupported assumptions.",
    },
    "judge": {
        "enabled": True,
        "name": "final_judge",
        "model": "provider/model-d",
        "instructions": "Synthesize by evidence, not voting.",
    },
}


class ExpertTeamContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_pool = os.environ.pop("OPENROUTER_MODEL_POOL", None)

    def tearDown(self) -> None:
        if self.old_pool is not None:
            os.environ["OPENROUTER_MODEL_POOL"] = self.old_pool
        else:
            os.environ.pop("OPENROUTER_MODEL_POOL", None)

    def test_package_lazy_exports_current_api(self) -> None:
        self.assertTrue(callable(expert_team.run_dynamic_team))
        self.assertTrue(callable(expert_team.validate_execution_plan))
        self.assertTrue(callable(expert_team.run_deepseek_steward))
        self.assertFalse(hasattr(expert_team, "plan_team"))

    def test_valid_dynamic_plan(self) -> None:
        plan = validate_execution_plan(VALID_PLAN)
        self.assertEqual(len(plan.experts), 2)
        self.assertEqual(plan.stages[0].mode, "parallel")
        self.assertTrue(plan.red_team.enabled)
        self.assertTrue(plan.judge.enabled)
        self.assertEqual(plan.deepseek_entry.status, "READY")
        self.assertEqual(plan.budget.tier, "balanced")
        self.assertEqual(plan.budget.max_model_calls, 4)

    def test_rejects_missing_user_budget_approval(self) -> None:
        payload = json.loads(json.dumps(VALID_PLAN))
        payload["budget"]["approval_status"] = "pending"
        with self.assertRaisesRegex(ValueError, "approved_by_user"):
            validate_execution_plan(payload)

    def test_rejects_unpresented_budget_options(self) -> None:
        payload = json.loads(json.dumps(VALID_PLAN))
        payload["deepseek_entry"]["budget_options_presented"] = False
        with self.assertRaisesRegex(ValueError, "presented to the user"):
            validate_execution_plan(payload)

    def test_rejects_estimate_above_approved_maximum(self) -> None:
        payload = json.loads(json.dumps(VALID_PLAN))
        payload["budget"]["estimated_cost_usd"]["high"] = 2.5
        with self.assertRaisesRegex(ValueError, "exceeds the user-approved maximum"):
            validate_execution_plan(payload)

    def test_rejects_model_calls_above_approved_limit(self) -> None:
        payload = json.loads(json.dumps(VALID_PLAN))
        payload["budget"]["max_model_calls"] = 3
        with self.assertRaisesRegex(ValueError, "planned model calls exceed"):
            validate_execution_plan(payload)

    def test_rejects_forward_stage_dependency(self) -> None:
        payload = json.loads(json.dumps(VALID_PLAN))
        payload["stages"][0]["input_from"] = ["future_stage"]
        with self.assertRaisesRegex(ValueError, "earlier stages"):
            validate_execution_plan(payload)

    def test_optional_model_allowlist(self) -> None:
        os.environ["OPENROUTER_MODEL_POOL"] = "provider/model-a"
        with self.assertRaisesRegex(ValueError, "not in OPENROUTER_MODEL_POOL"):
            validate_execution_plan(VALID_PLAN)

    def test_execution_plan_schema_file_is_present_and_json(self) -> None:
        schema = json.loads(Path("execution_plan.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["version"]["const"], "2")
        self.assertIn("deepseek_entry", schema["required"])
        self.assertIn("budget", schema["required"])
        self.assertEqual(schema["properties"]["selection_policy"]["const"], SELECTION_POLICY)

    def test_execution_plan_form_exposes_budget_and_entry(self) -> None:
        form = json.loads(Path("expert_team/execution_plan_form.json").read_text(encoding="utf-8"))
        self.assertEqual(form["selection_policy"], SELECTION_POLICY)
        self.assertEqual(form["deepseek_entry"]["status"], "READY")
        self.assertEqual(form["budget"]["approval_status"], "approved_by_user")


class ModelIntelligenceTests(unittest.TestCase):
    @patch("expert_team.model_intelligence.fetch_benchmarks")
    @patch("expert_team.model_intelligence.fetch_ranked_models")
    @patch("expert_team.model_intelligence.fetch_catalog_via_sdk")
    def test_snapshot_contains_all_selection_signals(self, catalog_mock, ranked_mock, benchmarks_mock) -> None:
        catalog_mock.return_value = {"data": [{"id": "provider/model-a"}]}
        ranked_mock.side_effect = lambda sort, limit=20: [
            {
                "id": f"{sort}/winner",
                "name": "Winner",
                "context_length": 128000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "supported_parameters": ["tools", "structured_outputs", "irrelevant-large-field"],
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "reasoning": {"default_enabled": True},
                "benchmarks": {
                    "artificial_analysis": {
                        "intelligence_index": 70,
                        "coding_index": 80,
                        "agentic_index": 75,
                    }
                },
            }
        ]
        benchmarks_mock.return_value = {
            "data": [
                {
                    "model_permaslug": "provider/model-a",
                    "intelligence_index": 70,
                    "coding_index": 80,
                    "agentic_index": 75,
                }
            ]
        }
        snapshot = build_model_intelligence_snapshot(limit_per_ranking=5)
        self.assertEqual(set(snapshot["rankings"]), set(RANKING_SORTS))
        self.assertEqual(snapshot["catalog"]["data"][0]["id"], "provider/model-a")
        self.assertIn("concrete task", snapshot["selection_rule"])
        compact = build_compact_model_intelligence_snapshot(snapshot)
        self.assertEqual(compact["schema_version"], "3")
        first_sort = RANKING_SORTS[0]
        model_id = compact["rankings"][first_sort][0]
        self.assertEqual(compact["models"][model_id]["pricing"]["prompt"], "0.000001")
        self.assertEqual(compact["models"][model_id]["supported_parameters"], ["tools", "structured_outputs"])
        self.assertTrue(compact["models"][model_id]["reasoning"])
        self.assertNotIn("catalog", compact)

    def test_gpt_snapshot_is_bounded_per_ranking(self) -> None:
        rows = [
            {
                "id": f"provider/model-{index}",
                "context_length": 1000 + index,
                "pricing": {"prompt": "0.1", "completion": "0.2", "image": "999"},
                "supported_parameters": ["tools", "structured_outputs", "many-extra-fields"],
                "architecture": {"input_modalities": ["text"]},
            }
            for index in range(GPT_RANKING_LIMIT + 10)
        ]
        snapshot = {
            "generated_at": "now",
            "source": "test",
            "selection_rule": "test",
            "rankings": {sort: rows for sort in RANKING_SORTS},
        }
        compact = build_compact_model_intelligence_snapshot(snapshot)
        self.assertTrue(all(len(items) <= GPT_RANKING_LIMIT for items in compact["rankings"].values()))
        self.assertLessEqual(len(compact["models"]), GPT_RANKING_LIMIT)
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(encoded.encode("utf-8")), 50_000)


if __name__ == "__main__":
    unittest.main()
