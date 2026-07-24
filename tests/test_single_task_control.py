from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from scripts.publish_operation_status import _should_update_current
from scripts.single_task_lock import _is_stale


class SingleTaskControlTests(unittest.TestCase):
    def test_active_lock_is_not_stale(self) -> None:
        now = datetime.now(timezone.utc)
        lock = {"state": "held", "expires_at": (now + timedelta(minutes=10)).isoformat()}
        self.assertFalse(_is_stale(lock, now))

    def test_expired_lock_is_stale(self) -> None:
        now = datetime.now(timezone.utc)
        lock = {"state": "held", "expires_at": (now - timedelta(seconds=1)).isoformat()}
        self.assertTrue(_is_stale(lock, now))

    def test_busy_record_never_overwrites_current_active_task(self) -> None:
        current = {"operation_id": "active-op", "status": "running"}
        busy = {"operation_id": "new-op", "status": "BUSY"}
        self.assertFalse(_should_update_current(current, busy, "never"))
        self.assertFalse(_should_update_current(current, busy, "if-owner"))

    def test_owner_can_update_current(self) -> None:
        current = {"operation_id": "op-1", "status": "running"}
        final = {"operation_id": "op-1", "status": "success"}
        self.assertTrue(_should_update_current(current, final, "if-owner"))

    def test_workflows_use_operation_level_cancel_and_no_paid_queue(self) -> None:
        production = open(".github/workflows/expert-team-production.yml", encoding="utf-8").read()
        cancellation = open(".github/workflows/cancel-operation.yml", encoding="utf-8").read()
        self.assertIn("Acquire global single-task execution lock", production)
        self.assertIn("Record BUSY without starting a paid task", production)
        self.assertNotIn("group: expert-team-production", production)
        self.assertIn("scripts.cancel_operation", cancellation)


if __name__ == "__main__":
    unittest.main()
