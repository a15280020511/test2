from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from scripts.verify_deepseek_entry import EntryVerificationError, verify

BUDGET = {
    "approval_status": "approved_by_user",
    "tier": "balanced",
    "currency": "USD",
    "max_cost_usd": 2.0,
    "estimated_cost_usd": {"low": 0.25, "high": 1.5},
    "max_model_calls": 4,
    "max_output_tokens_per_call": 1200,
    "approval_reference": "User selected balanced, maximum USD 2.00.",
}
PLAN = {
    "deepseek_entry": {
        "status": "READY",
        "operation_id": "assist-budget-001",
        "budget_options_presented": True,
    },
    "budget": BUDGET,
}
RECEIPT = {
    "operation_id": "execute-budget-001",
    "operation": "execute_team",
    "deepseek_assist_operation_id": "assist-budget-001",
    "budget_approval": BUDGET,
    "source": "web_gpt_after_user_budget_selection",
}
ASSIST_RESULT = {
    "mode": "ASSIST",
    "status": "READY",
    "budget_options": [
        {"tier": "economy"},
        {"tier": "balanced"},
        {"tier": "quality"},
    ],
    "budget_question_to_user": "Choose economy, balanced, quality, or a custom maximum.",
    "steward_model": "deepseek-v4-pro",
    "steward_provider": "DeepSeek official API",
}


class DurableBudgetReceiptTests(unittest.TestCase):
    @patch("scripts.verify_deepseek_entry._read_runtime_result", return_value=ASSIST_RESULT)
    def test_matching_receipt_is_verified(self, _mock) -> None:
        audit = verify(
            copy.deepcopy(PLAN),
            copy.deepcopy(RECEIPT),
            execution_operation_id="execute-budget-001",
            receipt_comment_id="12345",
        )
        self.assertEqual(audit["status"], "VERIFIED")
        self.assertEqual(audit["budget_receipt_comment_id"], "12345")
        self.assertEqual(audit["approved_budget"]["max_cost_usd"], 2.0)

    @patch("scripts.verify_deepseek_entry._read_runtime_result", return_value=ASSIST_RESULT)
    def test_receipt_budget_must_exactly_match_plan(self, _mock) -> None:
        receipt = copy.deepcopy(RECEIPT)
        receipt["budget_approval"]["max_cost_usd"] = 10.0
        with self.assertRaisesRegex(EntryVerificationError, "does not exactly match"):
            verify(
                copy.deepcopy(PLAN),
                receipt,
                execution_operation_id="execute-budget-001",
                receipt_comment_id="12345",
            )

    @patch("scripts.verify_deepseek_entry._read_runtime_result", return_value=ASSIST_RESULT)
    def test_receipt_must_be_created_after_user_selection(self, _mock) -> None:
        receipt = copy.deepcopy(RECEIPT)
        receipt["source"] = "server-side-fallback"
        with self.assertRaisesRegex(EntryVerificationError, "source is invalid"):
            verify(
                copy.deepcopy(PLAN),
                receipt,
                execution_operation_id="execute-budget-001",
                receipt_comment_id="12345",
            )


if __name__ == "__main__":
    unittest.main()
