from __future__ import annotations

import json
import unittest

from scripts.operation_controller import _normalize_request, _production_payload


class OperationControllerTests(unittest.TestCase):
    def test_normalizes_embedded_plan_and_receipt(self) -> None:
        request = _normalize_request(
            {
                "command": "submit_operation",
                "operation_id": "op-1",
                "operation": "execute_team",
                "task_label": "business analysis",
                "plan_json": {"task": "x"},
                "support_packet_json": {},
            },
            123,
        )
        self.assertEqual(request["operation_id"], "op-1")
        self.assertEqual(request["receipt_comment_id"], "123")
        self.assertEqual(json.loads(request["plan_json"])["task"], "x")
        payload = _production_payload(request)
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["inputs"]["receipt_comment_id"], "123")

    def test_rejects_unknown_operation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "operation must be"):
            _normalize_request(
                {
                    "command": "submit_operation",
                    "operation_id": "op-1",
                    "operation": "unknown",
                },
                123,
            )

    def test_controller_workflow_has_startup_watchdog_permissions(self) -> None:
        text = open(".github/workflows/operation-controller.yml", encoding="utf-8").read()
        self.assertIn("issue_comment", text)
        self.assertIn("contains(github.event.comment.body, 'submit_operation')", text)
        self.assertIn("actions: write", text)
        self.assertIn("scripts.operation_controller", text)
        controller = open("scripts/operation_controller.py", encoding="utf-8").read()
        self.assertIn("startup_timeout", controller)
        self.assertIn("range(7)", controller)
        self.assertIn("SUPERVISOR_WORKFLOW", controller)


if __name__ == "__main__":
    unittest.main()
