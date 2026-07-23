from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import expert_team
from expert_team.dynamic_team import validate_execution_plan
from expert_team.model_intelligence import RANKING_SORTS, build_model_intelligence_snapshot


VALID_PLAN = {
    "version": "1",
    "task": "Analyze a complex business decision.",
    "rationale": "Use independent experts, then adversarial review and arbitration.",
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

    def test_package_imports_current_api(self) -> None:
        self.assertTrue(callable(expert_team.run_dynamic_team))
        self.assertTrue(callable(expert_team.validate_execution_plan))
        self.assertFalse(hasattr(expert_team, "plan_team"))

    def test_valid_dynamic_plan(self) -> None:
        plan = validate_execution_plan(VALID_PLAN)
        self.assertEqual(len(plan.experts), 2)
        self.assertEqual(plan.stages[0].mode, "parallel")
        self.assertTrue(plan.red_team.enabled)
        self.assertTrue(plan.judge.enabled)

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
        path = Path("expert_team/execution_plan.schema.json")
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["version"]["const"], "1")
        self.assertIn("experts", schema["required"])
        self.assertIn("stages", schema["required"])


class ModelIntelligenceTests(unittest.TestCase):
    @patch("expert_team.model_intelligence.fetch_benchmarks")
    @patch("expert_team.model_intelligence.fetch_ranked_models")
    @patch("expert_team.model_intelligence.fetch_catalog_via_sdk")
    def test_snapshot_contains_all_selection_signals(
        self,
        catalog_mock,
        ranked_mock,
        benchmarks_mock,
    ) -> None:
        catalog_mock.return_value = {"data": [{"id": "provider/model-a"}]}
        ranked_mock.side_effect = lambda sort, limit=20: [{"id": f"{sort}/winner"}]
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
        self.assertEqual(snapshot["benchmarks"]["data"][0]["coding_index"], 80)
        self.assertIn("concrete task", snapshot["selection_rule"])


if __name__ == "__main__":
    unittest.main()
