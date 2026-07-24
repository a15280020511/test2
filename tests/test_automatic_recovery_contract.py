from __future__ import annotations

import unittest
from pathlib import Path


class AutomaticRecoveryContractTests(unittest.TestCase):
    def test_production_workflow_uses_managed_operation_and_repair_delivery(self) -> None:
        text = Path(".github/workflows/expert-team-production.yml").read_text(encoding="utf-8")
        self.assertIn("scripts.managed_operation", text)
        self.assertIn("scripts.repair_delivery", text)
        self.assertIn("scripts.publish_runtime_result", text)
        self.assertIn("DEEPSEEK_API_KEY", text)

    def test_recovery_policy_has_single_retry_and_deepseek_hard_stop(self) -> None:
        text = Path("ACTION_RECOVERY.md").read_text(encoding="utf-8")
        self.assertIn("one repair attempt and one retry", text)
        self.assertIn("DeepSeek unavailability is a hard stop", text)
        self.assertIn("Never route Steward repair through OpenRouter", text)

    def test_action_schema_requires_runtime_results_ref(self) -> None:
        text = Path("gpt_action_openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 1.3.0", text)
        self.assertIn("operationId: getAutoRepairResult", text)
        self.assertIn("operationId: getActionRecoveryPolicy", text)
        self.assertIn("enum: [runtime-results]", text)
        self.assertIn("automatically dispatch deepseek_steward", text)

    def test_managed_operation_limits_repair_to_one_cycle(self) -> None:
        text = Path("scripts/managed_operation.py").read_text(encoding="utf-8")
        self.assertIn("one repair cycle and one retry", text)
        self.assertEqual(text.count("repair_command = _entrypoint_command("), 1)
        self.assertEqual(text.count("second = _run(original_command"), 1)
        self.assertIn("DeepSeek Steward failed or was unavailable. Hard stop", text)


if __name__ == "__main__":
    unittest.main()
