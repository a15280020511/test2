from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.publish_operation_status import _build_status


class PermanentStatusControlPlaneTests(unittest.TestCase):
    def test_start_status_is_running_and_not_ready(self) -> None:
        with patch.dict(
            os.environ,
            {"GITHUB_RUN_ID": "12345", "RECEIPT_COMMENT_ID": "77"},
            clear=False,
        ):
            payload = _build_status("op-1", "execute_team", "start")
        self.assertEqual(payload["schema_version"], "4")
        self.assertEqual(payload["operation_id"], "op-1")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["run_id"], "12345")
        self.assertEqual(payload["receipt_comment_id"], "77")
        self.assertIsNone(payload["supervisor_for_operation_id"])
        self.assertFalse(payload["result_ready"])
        self.assertFalse(payload["result_published"])
        self.assertIn("heartbeat_at", payload)

    def test_repair_and_retry_statuses_are_explicit(self) -> None:
        repairing = _build_status("op-2", "execute_team", "repairing")
        retrying = _build_status("op-2", "execute_team", "retrying")
        self.assertEqual(repairing["status"], "repairing")
        self.assertEqual(repairing["repair_status"], "diagnosing")
        self.assertEqual(retrying["status"], "retrying")
        self.assertEqual(retrying["repair_status"], "retry_authorized")

    def test_cancel_states_are_explicit(self) -> None:
        requested = _build_status("op-c", "execute_team", "cancel_requested")
        cancelled = _build_status("op-c", "execute_team", "cancelled")
        self.assertEqual(requested["status"], "cancel_requested")
        self.assertTrue(requested["cancel_requested"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["cancel_requested"])

    def test_final_success_requires_remote_publication_success(self) -> None:
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                output = Path("artifacts/op-3")
                output.mkdir(parents=True)
                (output / "expert_team_result.json").write_text("{}", encoding="utf-8")
                (output / "metadata.json").write_text(
                    json.dumps({"status": "success", "readable_result_file": "expert_team_result.json"}),
                    encoding="utf-8",
                )
                unpublished = _build_status(
                    "op-3", "execute_team", "final", job_status="success", result_published="failure"
                )
                published = _build_status(
                    "op-3", "execute_team", "final", job_status="success", result_published="success"
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(unpublished["status"], "failure")
        self.assertFalse(unpublished["result_ready"])
        self.assertEqual(published["status"], "success")
        self.assertTrue(published["result_ready"])
        self.assertTrue(published["result_published"])

    def test_final_stop_is_not_mislabeled_recovered(self) -> None:
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                output = Path("artifacts/op-4")
                output.mkdir(parents=True)
                (output / "metadata.json").write_text(json.dumps({"status": "failure"}), encoding="utf-8")
                (output / "managed_operation.json").write_text(
                    json.dumps({"status": "STOP", "auto_repair_triggered": True}),
                    encoding="utf-8",
                )
                payload = _build_status(
                    "op-4", "execute_team", "final", job_status="failure", result_published="success"
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(payload["status"], "STOP")
        self.assertFalse(payload["result_ready"])
        self.assertEqual(payload["repair_status"], "diagnosed")


if __name__ == "__main__":
    unittest.main()
