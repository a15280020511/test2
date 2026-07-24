from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from expert_team.deepseek_official import (
    DEFAULT_STEWARD_MODEL,
    _api_key,
    select_strongest_official_model,
)


class DeepSeekOfficialTests(unittest.TestCase):
    def tearDown(self) -> None:
        select_strongest_official_model.cache_clear()
        os.environ.pop("DEEPSEEK_STEWARD_MODEL", None)
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_current_official_models_choose_v4_pro(self) -> None:
        select_strongest_official_model.cache_clear()
        with patch(
            "expert_team.deepseek_official.list_official_models",
            return_value=["deepseek-v4-flash", "deepseek-v4-pro"],
        ):
            self.assertEqual(select_strongest_official_model(), "deepseek-v4-pro")

    def test_newer_pro_model_wins_automatically(self) -> None:
        select_strongest_official_model.cache_clear()
        with patch(
            "expert_team.deepseek_official.list_official_models",
            return_value=["deepseek-v4-pro", "deepseek-v5-flash", "deepseek-v5-pro"],
        ):
            self.assertEqual(select_strongest_official_model(), "deepseek-v5-pro")

    def test_operator_override_is_respected(self) -> None:
        os.environ["DEEPSEEK_STEWARD_MODEL"] = "deepseek-v4-flash"
        select_strongest_official_model.cache_clear()
        self.assertEqual(select_strongest_official_model(), "deepseek-v4-flash")

    def test_model_discovery_failure_uses_current_strongest_baseline(self) -> None:
        select_strongest_official_model.cache_clear()
        with patch(
            "expert_team.deepseek_official.list_official_models",
            side_effect=RuntimeError("temporary failure"),
        ):
            self.assertEqual(select_strongest_official_model(), DEFAULT_STEWARD_MODEL)

    def test_official_api_key_is_required(self) -> None:
        os.environ.pop("DEEPSEEK_API_KEY", None)
        with self.assertRaisesRegex(RuntimeError, "never falls back to OpenRouter"):
            _api_key()


if __name__ == "__main__":
    unittest.main()
