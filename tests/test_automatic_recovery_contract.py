from __future__ import annotations

import unittest
from pathlib import Path


class AutomaticRecoveryContractTests(unittest.TestCase):
    def test_production_workflow_requires_receipt_and_auto_escalates_failures(self) -> None:
        text = Path(".github/workflows/expert-team-production.yml").read_text(encoding="utf-8")
        self.assertIn("run-name: expert-${{ inputs.operation_id }}-${{ inputs.operation }}", text)
        self.assertIn("receipt_comment_id:", text)
        self.assertIn("required: true", text)
        self.assertIn("scripts.managed_operation", text)
        self.assertIn("scripts.publish_operation_status", text)
        self.assertIn("Escalate failed production run to DeepSeek Top Supervisor", text)
        self.assertIn("deepseek-supervisor.yml/dispatches", text)
        self.assertIn("needs.run.result == 'failure'", text)
        self.assertIn("original_plan_json", text)

    def test_independent_supervisor_has_separate_concurrency_and_bounded_resume(self) -> None:
        text = Path(".github/workflows/deepseek-supervisor.yml").read_text(encoding="utf-8")
        self.assertIn("group: deepseek-top-supervisor", text)
        self.assertIn("Run highest-level DeepSeek technical supervisor", text)
        self.assertIn("scripts.top_supervisor_entrypoint", text)
        self.assertIn("scripts.enrich_supervisor_packet", text)
        self.assertIn("scripts.supervisor_resume", text)
        self.assertIn("--mode plan", text)
        self.assertIn("--mode execute", text)

    def test_top_supervisor_can_adapt_resource_and_model_failures_without_changing_user_intent(self) -> None:
        text = Path("expert_team/deepseek_top_supervisor.py").read_text(encoding="utf-8")
        self.assertIn("OpenRouter 402", text)
        self.assertIn("retry_operation_overrides", text)
        self.assertIn("preserving user intent", text)
        self.assertIn("lower-cost compatible models", text)
        resume = Path("scripts/supervisor_resume.py").read_text(encoding="utf-8")
        self.assertIn("_apply_retry_overrides", resume)
        self.assertIn("plan_json", resume)
        self.assertIn("ranking_limit", resume)

    def test_recovery_policy_places_deepseek_at_highest_technical_layer(self) -> None:
        text = Path("ACTION_RECOVERY.md").read_text(encoding="utf-8")
        self.assertIn("highest technical control layer", text)
        self.assertIn("Durable operation receipt", text)
        self.assertIn("two consecutive control reads", text)
        self.assertIn("deepseek-supervisor.yml", text)
        self.assertIn("one bounded production redispatch", text)
        self.assertIn("runtime_results/current_operation_status.json", text)

    def test_action_schema_exposes_receipt_and_top_supervisor(self) -> None:
        text = Path("gpt_action_openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 1.6.0", text)
        self.assertIn("operationId: createOperationReceipt", text)
        self.assertIn("operationId: dispatchExpertTeamOperation", text)
        self.assertIn("operationId: dispatchDeepSeekSupervisor", text)
        self.assertIn("operationId: getCurrentOperationStatus", text)
        self.assertIn("receipt_comment_id", text)
        self.assertIn("issues/15/comments", text)
        self.assertNotIn("operationId: listExpertTeamRuns", text)
        self.assertNotIn("operationId: getOperationStatus", text)
        self.assertNotIn("runtime_results/status/{operation_id}.json", text)

    def test_managed_operation_keeps_single_internal_repair_cycle(self) -> None:
        text = Path("scripts/managed_operation.py").read_text(encoding="utf-8")
        self.assertIn("one repair cycle and one retry", text)
        self.assertEqual(text.count("repair_command = _entrypoint_command("), 1)
        self.assertEqual(text.count("second = _run(original_command"), 1)
        self.assertIn("DeepSeek Steward failed or was unavailable. Hard stop", text)
        self.assertIn('"repairing"', text)
        self.assertIn('"retrying"', text)


if __name__ == "__main__":
    unittest.main()
