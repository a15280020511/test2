from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.supervisor_resume import _plan_resume

SELECTION_POLICY = (
    "默认采用质量约束下的动态最优组合：先保证任务所需质量，再在满足质量的候选模型中优化成本和速度；"
    "随着任务复杂度、风险、价值和不确定性提高，自动增加专家数量、模型多样性和红队强度；"
    "重大任务以能力优先，普通任务以性价比优先。不得固定专家数量、固定模型或固定工作流，必须具体问题具体分析。"
)


class SupervisorResumeTests(unittest.TestCase):
    def _run_plan(
        self,
        runs: list[dict],
        failed_run_id: str = "",
        retry_operation_overrides: dict | None = None,
        original_plan: dict | None = None,
    ) -> dict:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                output = Path("artifacts/sup-1")
                output.mkdir(parents=True)
                (output / "deepseek_steward_result.json").write_text(
                    json.dumps(
                        {
                            "resume": "READY",
                            "decision": "NO_EDIT",
                            "diagnosis": "budget recovery",
                            "retry_operation_overrides": retry_operation_overrides or {},
                        }
                    ),
                    encoding="utf-8",
                )
                retry_payload = json.dumps(
                    {
                        "ref": "main",
                        "inputs": {
                            "operation_id": "op-1",
                            "operation": "execute_team",
                            "receipt_comment_id": "55",
                            "plan_json": json.dumps(original_plan or {}),
                            "ranking_limit": "20",
                        },
                    }
                )
                with (
                    patch("scripts.supervisor_resume._matching_runs", return_value=runs),
                    patch(
                        "scripts.supervisor_resume.preflight_execution_plan",
                        return_value={"within_budget": True, "estimated_worst_case_usd": 0.2},
                    ),
                ):
                    plan = _plan_resume(
                        supervisor_operation_id="sup-1",
                        original_operation_id="op-1",
                        failed_run_id=failed_run_id,
                        retry_dispatch_json=retry_payload,
                        repository="a15280020511/test2",
                        token="dummy",
                    )
            finally:
                os.chdir(previous)
        return plan

    def test_missing_run_allows_one_resume(self) -> None:
        plan = self._run_plan([])
        self.assertEqual(plan["action"], "dispatch")

    def test_deepseek_plan_override_is_validated_and_provenance_added(self) -> None:
        original_plan = {
            "version": "1",
            "selection_policy": SELECTION_POLICY,
            "task": "same task",
            "rationale": "original",
            "budget": {"max_total_usd": 1.0, "recovery_reserve_ratio": 0.3},
            "experts": [
                {
                    "name": "expert",
                    "mission": "analyze",
                    "instructions": "use evidence",
                    "model": "provider/model-a",
                }
            ],
            "stages": [{"id": "s1", "mode": "parallel", "members": ["expert"], "input_from": ["task"]}],
            "red_team": {"enabled": False, "name": "red", "model": "", "instructions": ""},
            "judge": {"enabled": False, "name": "judge", "model": "", "instructions": ""},
        }
        replacement_plan = json.loads(json.dumps(original_plan))
        replacement_plan["rationale"] = "lower-cost technical retry"
        replacement_plan["experts"][0]["max_completion_tokens"] = 512
        plan = self._run_plan(
            [],
            retry_operation_overrides={"plan_json": replacement_plan, "ranking_limit": "8"},
            original_plan=original_plan,
        )
        payload = plan["retry_dispatch_payload"]
        effective = json.loads(payload["inputs"]["plan_json"])
        self.assertEqual(payload["inputs"]["ranking_limit"], "8")
        self.assertEqual(effective["provenance"]["effective_plan_source"], "deepseek_top_supervisor")
        self.assertEqual(effective["provenance"]["supervisor_operation_id"], "sup-1")
        self.assertEqual(plan["applied_retry_overrides"]["plan_json"], "replaced_validated_budget_compliant")

    def test_replacement_cannot_change_user_task(self) -> None:
        original = {
            "version": "1",
            "selection_policy": SELECTION_POLICY,
            "task": "original task",
            "rationale": "original",
            "experts": [{"name": "e", "mission": "m", "instructions": "i", "model": "p/m"}],
            "stages": [{"id": "s", "mode": "parallel", "members": ["e"], "input_from": ["task"]}],
            "red_team": {"enabled": False, "name": "red", "model": "", "instructions": ""},
            "judge": {"enabled": False, "name": "judge", "model": "", "instructions": ""},
        }
        replacement = json.loads(json.dumps(original))
        replacement["task"] = "changed task"
        with self.assertRaisesRegex(RuntimeError, "changed the user's substantive task"):
            self._run_plan([], retry_operation_overrides={"plan_json": replacement}, original_plan=original)

    def test_active_matching_run_blocks_duplicate(self) -> None:
        plan = self._run_plan(
            [{"id": 100, "status": "queued", "conclusion": None, "display_title": "expert-op-1-execute_team"}]
        )
        self.assertEqual(plan["action"], "none")
        self.assertEqual(plan["reason"], "matching_run_already_active")

    def test_known_failed_parent_run_does_not_block_resume(self) -> None:
        plan = self._run_plan(
            [{"id": 100, "status": "in_progress", "conclusion": None, "display_title": "expert-op-1-execute_team"}],
            failed_run_id="100",
        )
        self.assertEqual(plan["action"], "dispatch")

    def test_two_matching_runs_block_third_dispatch(self) -> None:
        plan = self._run_plan(
            [
                {"id": 100, "status": "completed", "conclusion": "failure", "display_title": "expert-op-1-execute_team"},
                {"id": 101, "status": "completed", "conclusion": "failure", "display_title": "expert-op-1-execute_team"},
            ]
        )
        self.assertEqual(plan["action"], "none")
        self.assertEqual(plan["reason"], "bounded_retry_limit_reached")


if __name__ == "__main__":
    unittest.main()
