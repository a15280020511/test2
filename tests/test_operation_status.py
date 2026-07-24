from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.publish_operation_status import _build_status


class OperationStatusTests(unittest.TestCase):
    def test_start_status_is_small_running_and_receipt_correlated(self) -> None:
        with patch.dict(
            os.environ,
            {"GITHUB_RUN_ID": "123456", "RECEIPT_COMMENT_ID": "789"},
            clear=False,
        ):
            payload = _build_status("op-1", "execute_team", "start", "")
        self.assertEqual(payload["schema_version"], "3")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["run_id"], "123456")
        self.assertEqual(payload["receipt_comment_id"], "789")
        self.assertFalse(payload["result_ready"])
        self.assertFalse(payload["result_published"])
        self.assertEqual(payload["repair_status"], "none")
        self.assertLess(len(json.dumps(payload, separators=(",", ":"))), 1400)

    def test_supervisor_status_names_original_operation(self) -> None:
        payload = _build_status(
            "supervisor-op-1",
            "deepseek_supervisor",
            "start",
            receipt_comment_id="789",
            supervisor_for_operation_id="op-1",
        )
        self.assertEqual(payload["operation_id"], "supervisor-op-1")
        self.assertEqual(payload["supervisor_for_operation_id"], "op-1")
        self.assertEqual(payload["receipt_comment_id"], "789")
        self.assertEqual(payload["status"], "running")

    def test_final_success_requires_published_result(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                output = Path("artifacts/op-2")
                output.mkdir(parents=True)
                (output / "expert_team_result.json").write_text("{}", encoding="utf-8")
                (output / "metadata.json").write_text(
                    json.dumps(
                        {
                            "status": "success",
                            "readable_result_file": "expert_team_result.json",
                        }
                    ),
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {"GITHUB_RUN_ID": "999"}, clear=False):
                    payload = _build_status(
                        "op-2",
                        "execute_team",
                        "final",
                        "success",
                        "success",
                    )
            finally:
                os.chdir(previous)

        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["result_ready"])
        self.assertTrue(payload["result_published"])
        self.assertEqual(payload["run_id"], "999")

    def test_final_unpublished_result_is_not_ready(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                output = Path("artifacts/op-3")
                output.mkdir(parents=True)
                (output / "expert_team_result.json").write_text("{}", encoding="utf-8")
                (output / "metadata.json").write_text(
                    json.dumps(
                        {
                            "status": "success",
                            "readable_result_file": "expert_team_result.json",
                        }
                    ),
                    encoding="utf-8",
                )
                payload = _build_status(
                    "op-3",
                    "execute_team",
                    "final",
                    "success",
                    "failure",
                )
            finally:
                os.chdir(previous)

        self.assertEqual(payload["status"], "failure")
        self.assertFalse(payload["result_ready"])
        self.assertFalse(payload["result_published"])


if __name__ == "__main__":
    unittest.main()
