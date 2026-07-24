from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from expert_team.deepseek_steward import (
    _validate_repair_path,
    run_deepseek_steward,
)


class DeepSeekStewardTests(unittest.TestCase):
    def test_repair_path_rejects_protected_targets(self) -> None:
        for path in ("tests/test_x.py", "runtime_results/a.json", "artifacts/x.json", ".git/config"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "protected repair path"):
                    _validate_repair_path(path)

    def test_assist_never_applies_repository_edits(self) -> None:
        payload = {
            "mode": "ASSIST",
            "status": "READY",
            "diagnosis": "Plan structure is valid.",
            "guidance": ["Read current model intelligence."],
            "execution_plan_guidance": {
                "expert_count_guidance": "Use the minimum sufficient team.",
                "role_guidance": ["Separate market and finance if both matter."],
                "stage_guidance": "Parallelize independent work.",
                "red_team_guidance": "Use for high-risk decisions.",
                "judge_guidance": "Use when synthesis/arbitration is needed.",
                "model_selection_guidance": "Use current OpenRouter evidence.",
            },
            "missing_information": [],
            "message_to_web_gpt": "READY",
        }
        fake_generate = AsyncMock(return_value=("deepseek-v4-pro", json.dumps(payload, ensure_ascii=False)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "marker.txt"
            marker.write_text("unchanged", encoding="utf-8")
            with patch("expert_team.deepseek_steward.generate_official_deepseek_json", fake_generate):
                result = asyncio.run(run_deepseek_steward("ASSIST", "{}", root=root))
            self.assertEqual(result["status"], "READY")
            self.assertEqual(result["steward_model"], "deepseek-v4-pro")
            self.assertEqual(result["steward_provider"], "DeepSeek official API")
            self.assertEqual(result["repair_application"]["applied_files"], [])
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_repair_applies_bounded_full_file_edit(self) -> None:
        payload = {
            "mode": "REPAIR",
            "decision": "EDIT",
            "diagnosis": "A source configuration is stale.",
            "confidence": 0.9,
            "edits": [{"path": "config/example.json", "content": "{\"fixed\": true}\n"}],
            "delete_files": [],
            "verification": ["Run tests"],
            "resume": "STOP",
            "message_to_web_gpt": "Wait for verification.",
        }
        fake_generate = AsyncMock(return_value=("deepseek-v4-pro", json.dumps(payload, ensure_ascii=False)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("expert_team.deepseek_steward.generate_official_deepseek_json", fake_generate):
                result = asyncio.run(run_deepseek_steward("REPAIR", "{}", root=root))
            target = root / "config/example.json"
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "{\"fixed\": true}\n")
            self.assertEqual(result["repair_application"]["applied_files"], ["config/example.json"])
            self.assertEqual(result["resume"], "STOP")


if __name__ == "__main__":
    unittest.main()
