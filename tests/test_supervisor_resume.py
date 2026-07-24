from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.supervisor_resume import _plan_resume


class SupervisorResumeTests(unittest.TestCase):
    def _run_plan(
        self,
        runs: list[dict],
        failed_run_id: str = "",
        retry_operation_overrides: dict | None = None,
        operation: str = "model_intelligence",
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
                            "operation": operation,
                            "receipt_comment_id": "55",
                            "plan_json": "{}",
                            "ranking_limit": "20",
                        },
                    }
                )
                with patch("scripts.supervisor_resume._matching_runs", return_value=runs):
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

    def test_missing_non_paid_run_allows_one_resume(self) -> None:
        plan = self._run_plan([])
        self.assertEqual(plan["action"], "dispatch")

    def test_deepseek_non_paid_override_is_applied_before_resume(self) -> None:
        replacement_plan = {
            "task": "same task",
            "rationale": "lower-cost technical retry",
            "experts": [],
            "stages": [],
        }
        plan = self._run_plan(
            [],
            retry_operation_overrides={"plan_json": replacement_plan, "ranking_limit": "8"},
        )
        payload = plan["retry_dispatch_payload"]
        self.assertEqual(payload["inputs"]["ranking_limit"], "8")
        self.assertEqual(json.loads(payload["inputs"]["plan_json"]), replacement_plan)
        self.assertEqual(plan["applied_retry_overrides"]["plan_json"], "replaced")

    def test_paid_expert_run_never_redispatches_under_old_budget(self) -> None:
        plan = self._run_plan([], operation="execute_team")
        self.assertEqual(plan["action"], "none")
        self.assertEqual(plan["reason"], "paid_operation_requires_new_user_budget_approval")
        self.assertTrue(plan["budget_reapproval_required"])
        self.assertTrue(plan["new_operation_id_required"])
        self.assertTrue(plan["new_budget_receipt_required"])
        self.assertNotIn("retry_dispatch_payload", plan)

    def test_paid_expert_override_is_ignored(self) -> None:
        plan = self._run_plan(
            [],
            retry_operation_overrides={"ranking_limit": "1", "plan_json": {"task": "changed"}},
            operation="execute_team",
        )
        self.assertEqual(plan["applied_retry_overrides"], {})
        self.assertEqual(plan["action"], "none")

    def test_active_matching_non_paid_run_blocks_duplicate(self) -> None:
        plan = self._run_plan(
            [{"id": 100, "status": "queued", "conclusion": None, "display_title": "expert-op-1-model_intelligence"}]
        )
        self.assertEqual(plan["action"], "none")
        self.assertEqual(plan["reason"], "matching_run_already_active")

    def test_known_failed_parent_non_paid_run_does_not_block_resume(self) -> None:
        plan = self._run_plan(
            [{"id": 100, "status": "in_progress", "conclusion": None, "display_title": "expert-op-1-model_intelligence"}],
            failed_run_id="100",
        )
        self.assertEqual(plan["action"], "dispatch")

    def test_two_matching_non_paid_runs_block_third_dispatch(self) -> None:
        plan = self._run_plan(
            [
                {"id": 100, "status": "completed", "conclusion": "failure", "display_title": "expert-op-1-model_intelligence"},
                {"id": 101, "status": "completed", "conclusion": "failure", "display_title": "expert-op-1-model_intelligence"},
            ]
        )
        self.assertEqual(plan["action"], "none")
        self.assertEqual(plan["reason"], "bounded_retry_limit_reached")


if __name__ == "__main__":
    unittest.main()
