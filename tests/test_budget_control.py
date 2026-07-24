from __future__ import annotations

import unittest

from expert_team.budget import BudgetPreflightError, ModelPrice, preflight_execution_plan

SELECTION_POLICY = (
    "默认采用质量约束下的动态最优组合：先保证任务所需质量，再在满足质量的候选模型中优化成本和速度；"
    "随着任务复杂度、风险、价值和不确定性提高，自动增加专家数量、模型多样性和红队强度；"
    "重大任务以能力优先，普通任务以性价比优先。不得固定专家数量、固定模型或固定工作流，必须具体问题具体分析。"
)


def plan(max_total: float = 1.0, expert_tokens: int = 1000, judge_tokens: int = 1200) -> dict:
    return {
        "version": "1",
        "selection_policy": SELECTION_POLICY,
        "task": "Analyze one business decision from supplied evidence.",
        "rationale": "Use two independent perspectives and one judge.",
        "budget": {"max_total_usd": max_total, "recovery_reserve_ratio": 0.3},
        "experts": [
            {
                "name": "market",
                "mission": "market",
                "instructions": "facts only",
                "model": "provider/model-a",
                "max_completion_tokens": expert_tokens,
            },
            {
                "name": "finance",
                "mission": "finance",
                "instructions": "formulas only",
                "model": "provider/model-b",
                "max_completion_tokens": expert_tokens,
            },
        ],
        "stages": [{"id": "s1", "mode": "parallel", "members": ["market", "finance"], "input_from": ["task"]}],
        "red_team": {"enabled": False, "name": "red", "model": "", "instructions": ""},
        "judge": {
            "enabled": True,
            "name": "judge",
            "model": "provider/model-c",
            "instructions": "synthesize",
            "max_completion_tokens": judge_tokens,
        },
    }


class BudgetControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prices = {
            "provider/model-a": ModelPrice(0.000001, 0.000002),
            "provider/model-b": ModelPrice(0.000001, 0.000002),
            "provider/model-c": ModelPrice(0.000001, 0.000002),
        }

    def test_default_one_dollar_budget_reserves_thirty_percent(self) -> None:
        result = preflight_execution_plan(plan(), pricing_by_model=self.prices)
        self.assertEqual(result["execution_phase"], "normal")
        self.assertEqual(result["max_total_usd"], 1.0)
        self.assertEqual(result["normal_execution_budget_usd"], 0.7)
        self.assertEqual(result["reserved_recovery_budget_usd"], 0.3)
        self.assertEqual(result["available_execution_budget_usd"], 0.7)
        self.assertTrue(result["includes_primary_transient_retry"])
        self.assertTrue(result["within_budget"])
        self.assertEqual(len(result["items"]), 3)

    def test_deepseek_plan_uses_only_recovery_reserve(self) -> None:
        payload = plan()
        payload["provenance"] = {"effective_plan_source": "deepseek_top_supervisor"}
        result = preflight_execution_plan(payload, pricing_by_model=self.prices)
        self.assertEqual(result["execution_phase"], "recovery")
        self.assertEqual(result["available_execution_budget_usd"], 0.3)

    def test_over_budget_stops_before_paid_execution(self) -> None:
        expensive = {key: ModelPrice(0.001, 0.002) for key in self.prices}
        with self.assertRaisesRegex(BudgetPreflightError, "exceeds available budget"):
            preflight_execution_plan(plan(max_total=0.2), pricing_by_model=expensive)

    def test_missing_model_price_is_hard_stop(self) -> None:
        with self.assertRaisesRegex(BudgetPreflightError, "No current pricing metadata"):
            preflight_execution_plan(plan(), pricing_by_model={})

    def test_invalid_operator_budget_is_rejected(self) -> None:
        with self.assertRaisesRegex(BudgetPreflightError, "max_total_usd"):
            preflight_execution_plan(plan(max_total=0), pricing_by_model=self.prices)


if __name__ == "__main__":
    unittest.main()
