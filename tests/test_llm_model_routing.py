from __future__ import annotations

import unittest

from app.config import Settings, resolve_gemini_model
from app.services.deepseek_analyzer import AUTHORITY_FIRST_PROMPT_TEMPLATE, SYSTEM_PROMPT_TEMPLATE


class GeminiModelRoutingTests(unittest.TestCase):
    def test_news_prompts_require_full_body_summaries(self) -> None:
        self.assertIn("3–6 句、约 300–800 个汉字", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("3–6 句、约 300–800 个汉字", AUTHORITY_FIRST_PROMPT_TEMPLATE)

    def test_empty_task_models_fall_back_to_gemini_model(self) -> None:
        settings = Settings(
            _env_file=None,
            gemini_model="gemini-3.1-flash-lite",
            gemini_fast_model="",
            gemini_flash_model="",
            gemini_strong_model="",
        )
        self.assertEqual(resolve_gemini_model("briefing", settings), "gemini-3.1-flash-lite")
        self.assertEqual(resolve_gemini_model("finance", settings), "gemini-3.1-flash-lite")
        self.assertEqual(resolve_gemini_model("entity", settings), "gemini-3.1-flash-lite")

    def test_task_specific_models_are_used(self) -> None:
        settings = Settings(
            _env_file=None,
            gemini_model="gemini-3.1-flash-lite",
            gemini_fast_model="gemini-3.1-flash-lite",
            gemini_flash_model="gemini-3-flash-preview",
            gemini_strong_model="gemini-3.1-pro-preview",
        )
        self.assertEqual(resolve_gemini_model("briefing", settings), "gemini-3.1-flash-lite")
        self.assertEqual(resolve_gemini_model("evidence", settings), "gemini-3-flash-preview")
        self.assertEqual(resolve_gemini_model("search", settings), "gemini-3-flash-preview")
        self.assertEqual(resolve_gemini_model("industry", settings), "gemini-3.1-pro-preview")
        self.assertEqual(resolve_gemini_model("entity", settings), "gemini-3.1-pro-preview")
