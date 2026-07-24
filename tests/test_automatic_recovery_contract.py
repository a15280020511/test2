from __future__ import annotations

import unittest
from pathlib import Path


class AutomaticRecoveryContractTests(unittest.TestCase):
    def test_production_workflow_uses_managed_operation_and_permanent_status_publication(self) -> None:
        text = Path(".github/workflows/expert-team-production.yml").read_text(encoding="utf-8")
        self.assertIn("scripts.managed_operation", text)
        self.assertIn("scripts.repair_delivery", text)
        self.assertIn("scripts.publish_runtime_result", text)
        self.assertIn("scripts.publish_operation_status", text)
        self.assertIn("--phase start", text)
        self.assertIn("--phase final", text)
        self.assertIn("--result-published", text)
        self.assertIn("id: publish_result", text)
        self.assertIn("DEEPSEEK_API_KEY", text)

    def test_recovery_policy_has_single_retry_hard_stop_and_permanent_control_plane(self) -> None:
        text = Path("ACTION_RECOVERY.md").read_text(encoding="utf-8")
        self.assertIn("one repair attempt and one retry", text)
        self.assertIn("DeepSeek unavailability is a hard stop", text)
        self.assertIn("Never route Steward repair through OpenRouter", text)
        self.assertIn("runtime_results/current_operation_status.json", text)
        self.assertIn("operation_id", text)
        self.assertIn("result_published", text)
        self.assertIn("Do not list workflow runs", text)

    def test_action_schema_uses_permanent_current_status_not_run_list_or_history_status(self) -> None:
        text = Path("gpt_action_openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 1.5.0", text)
        self.assertIn("operationId: dispatchExpertTeamOperation", text)
        self.assertIn("operationId: getCurrentOperationStatus", text)
        self.assertIn("runtime_results/current_operation_status.json", text)
        self.assertIn("operationId: getAutoRepairResult", text)
        self.assertIn("operationId: getActionRecoveryPolicy", text)
        self.assertIn("enum: [runtime-results]", text)
        self.assertIn("enum: [ASSIST, REPAIR]", text)
        self.assertNotIn("operationId: listExpertTeamRuns", text)
        self.assertNotIn("operationId: getOperationStatus", text)
        self.assertNotIn("runtime_results/status/{operation_id}.json", text)

    def test_managed_operation_limits_repair_to_one_cycle_and_publishes_transitions(self) -> None:
        text = Path("scripts/managed_operation.py").read_text(encoding="utf-8")
        self.assertIn("one repair cycle and one retry", text)
        self.assertEqual(text.count("repair_command = _entrypoint_command("), 1)
        self.assertEqual(text.count("second = _run(original_command"), 1)
        self.assertIn("DeepSeek Steward failed or was unavailable. Hard stop", text)
        self.assertIn('"repairing"', text)
        self.assertIn('"retrying"', text)


if __name__ == "__main__":
    unittest.main()
