from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.http_retry import is_retryable_error
from app.services.llm_web_search import _parse_gemini_search, _parse_items_json
from app.services.mita_search import (
    MitaQuotaError,
    MitaSearchClient,
    MitaSearchResponse,
    MitaSearchResultItem,
    is_mita_fallback_worthy,
    is_mita_quota_error,
    mita_direct_skipped,
    reset_mita_quota_state,
)


class MitaQuotaDetectionTests(unittest.TestCase):
    def test_err_code_3000_is_quota(self) -> None:
        self.assertTrue(is_mita_quota_error({"errCode": 3000, "errMsg": "余额不足"}))

    def test_other_business_error_is_not_quota(self) -> None:
        self.assertFalse(is_mita_quota_error({"errCode": 4001, "errMsg": "参数错误"}))

    def test_quota_is_not_retryable(self) -> None:
        self.assertFalse(is_retryable_error(MitaQuotaError("余额不足")))
        self.assertFalse(is_retryable_error(RuntimeError("秘塔搜索请求失败: 余额不足")))


class MitaSearchFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_mita_quota_state()

    def tearDown(self) -> None:
        reset_mita_quota_state()

    def test_quota_error_does_not_fall_back_to_llm_search(self) -> None:
        client = MitaSearchClient(api_key="mk-test-key")
        with patch.object(
            client,
            "_search_metaso",
            side_effect=MitaQuotaError("余额不足"),
        ), patch(
            "app.services.llm_web_search.search_web_fallback",
        ) as fallback:
            with self.assertRaises(MitaQuotaError):
                client.search("三菱商事 新闻", max_results=5)
        fallback.assert_not_called()

    def test_non_quota_error_is_not_redirected(self) -> None:
        client = MitaSearchClient(api_key="mk-test-key")
        with patch.object(
            client,
            "_search_metaso",
            side_effect=RuntimeError("秘塔搜索失败: 参数错误"),
        ), patch(
            "app.services.llm_web_search.search_web_fallback",
        ) as fallback:
            with self.assertRaises(RuntimeError) as ctx:
                client.search("查询")
        self.assertIn("参数错误", str(ctx.exception))
        fallback.assert_not_called()
        self.assertFalse(is_mita_fallback_worthy(RuntimeError("秘塔搜索失败: 参数错误")))

    def test_network_error_does_not_fall_back_to_llm_search(self) -> None:
        client = MitaSearchClient(api_key="mk-test-key")
        with patch.object(
            client,
            "_search_metaso",
            side_effect=RuntimeError("秘塔搜索请求失败: ConnectTimeout"),
        ), patch(
            "app.services.llm_web_search.search_web_fallback",
        ) as fallback:
            with self.assertRaisesRegex(RuntimeError, "ConnectTimeout"):
                client.search("三菱商事 新闻", max_results=5)
        fallback.assert_not_called()
        self.assertTrue(mita_direct_skipped())

    def test_missing_key_does_not_fall_back_to_llm_search(self) -> None:
        client = MitaSearchClient(api_key="mk-test-key")
        with patch.object(
            client,
            "_search_metaso",
            side_effect=RuntimeError("未配置有效的 MITA_API_KEY，请在 .env 中填写秘塔 API 密钥"),
        ), patch(
            "app.services.llm_web_search.search_web_fallback",
        ) as fallback:
            with self.assertRaisesRegex(RuntimeError, "MITA_API_KEY"):
                client.search("查询")
        fallback.assert_not_called()
        self.assertTrue(is_mita_fallback_worthy(RuntimeError("未配置有效的 MITA_API_KEY")))

    def test_successful_mita_does_not_call_fallback(self) -> None:
        client = MitaSearchClient(api_key="mk-test-key")
        ok = MitaSearchResponse(
            query="q",
            items=[MitaSearchResultItem(title="a", url="https://a.example/x", snippet="s")],
            provider="mita",
        )
        with patch.object(client, "_search_metaso", return_value=ok), patch(
            "app.services.llm_web_search.search_web_fallback",
        ) as fallback:
            result = client.search("查询")
        self.assertEqual(result.provider, "mita")
        fallback.assert_not_called()


class LlmWebSearchParseTests(unittest.TestCase):
    def test_parse_gemini_grounding_chunks(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"items":[]}' }]},
                    "groundingMetadata": {
                        "groundingChunks": [
                            {"web": {"uri": "https://www.nikkei.com/a", "title": "日经新闻"}}
                        ],
                        "groundingSupports": [
                            {
                                "segment": {"text": "三菱商事发布财报"},
                                "groundingChunkIndices": [0],
                            }
                        ],
                    },
                }
            ]
        }
        items = _parse_gemini_search(data, limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://www.nikkei.com/a")
        self.assertIn("财报", items[0]["snippet"])

    def test_parse_items_json_from_fenced_text(self) -> None:
        rows = _parse_items_json(
            '```json\n{"items":[{"title":"A","url":"https://reuters.com/x"}]}\n```'
        )
        self.assertEqual(rows[0]["title"], "A")

    def test_gemini_failure_falls_to_deepseek(self) -> None:
        from app.services import llm_web_search

        class _Settings:
            gemini_api_key = "AQ.test-key"
            deepseek_api_key = "sk-test-key"

        items = [
            {
                "title": "路透",
                "url": "https://www.reuters.com/a",
                "snippet": "摘要",
                "published_at": None,
                "source_domain": "reuters.com",
            }
        ]
        with patch.object(llm_web_search, "get_settings", return_value=_Settings()), patch.object(
            llm_web_search, "_search_gemini", side_effect=RuntimeError("no grounding")
        ), patch.object(llm_web_search, "_search_deepseek", return_value=items):
            rows, provider = llm_web_search.search_web_fallback("查询", max_results=3)
        self.assertEqual(provider, "deepseek")
        self.assertEqual(rows[0]["url"], "https://www.reuters.com/a")
