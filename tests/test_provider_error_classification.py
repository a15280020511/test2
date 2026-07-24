from __future__ import annotations

import asyncio
import unittest

from expert_team.dynamic_team import _classify_error


class ProviderErrorClassificationTests(unittest.TestCase):
    def test_empty_model_text_is_transient(self) -> None:
        self.assertEqual(_classify_error(RuntimeError("model returned empty text")), "transient_provider")

    def test_402_is_budget_and_never_transient(self) -> None:
        self.assertEqual(
            _classify_error(RuntimeError("OpenRouter 402 insufficient credits")),
            "budget_or_credit",
        )

    def test_timeout_is_timeout(self) -> None:
        self.assertEqual(_classify_error(asyncio.TimeoutError()), "timeout")

    def test_unknown_error_is_not_retried_by_default(self) -> None:
        self.assertEqual(_classify_error(RuntimeError("deterministic validation failure")), "permanent_or_unknown")


if __name__ == "__main__":
    unittest.main()
