from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock

from app.services.ddg_search import DuckDuckGoNewsClient
from app.services.mita_search import MitaSearchResponse, MitaSearchResultItem
from app.services.pipeline import RiskPipeline
from app.services.search_fallback import (
    is_empty_search_error,
    rewrite_ddg_news_query,
    should_fallback_to_ddg,
)
from tests.helpers import isolated_session


class SearchFallbackPolicyTests(unittest.TestCase):
    def test_empty_error_markers(self) -> None:
        self.assertTrue(is_empty_search_error(RuntimeError("秘塔搜索失败: 未找到相关数据")))
        self.assertFalse(is_empty_search_error(RuntimeError("秘塔搜索失败: 参数错误")))

    def test_only_module_c_and_japan_entities(self) -> None:
        self.assertTrue(should_fallback_to_ddg("C", metadata={"company": "デンソー"}))
        self.assertTrue(
            should_fallback_to_ddg("A", metadata={"target": "三菱商事"})
        )
        self.assertFalse(should_fallback_to_ddg("A", metadata={"target": "Godiva"}))
        self.assertFalse(should_fallback_to_ddg("B"))
        self.assertFalse(should_fallback_to_ddg("D"))
        self.assertFalse(
            should_fallback_to_ddg("C", calendar_day=date(2026, 8, 1))
        )
        self.assertFalse(should_fallback_to_ddg("C", enabled=False))

    def test_rewrite_strips_chinese_categories(self) -> None:
        query = "デンソー 司法与行政监管 新闻 动态 最新 过去24小时 OR today OR 速报"
        rewritten = rewrite_ddg_news_query(
            query, "A", metadata={"target": "デンソー"}
        )
        self.assertNotIn("司法与行政监管", rewritten)
        self.assertNotIn("过去24小时", rewritten)
        self.assertIn("デンソー", rewritten)
        self.assertIn("DENSO", rewritten)
        self.assertIn("適時開示", rewritten)

    def test_rewrite_module_c_uses_jp_en_names(self) -> None:
        rewritten = rewrite_ddg_news_query(
            "三菱商事 OR Mitsubishi Corporation 適時開示 OR IR 最新 过去24小时 OR today OR 速报",
            "C",
            metadata={"company": "三菱商事"},
        )
        self.assertEqual(
            rewritten,
            '三菱商事 OR "Mitsubishi Corporation" 適時開示 OR ニュースリリース OR IR OR 決算 OR earnings',
        )


class DuckDuckGoNewsClientTests(unittest.TestCase):
    def test_maps_news_fields_and_forces_duckduckgo_backend(self) -> None:
        client = DuckDuckGoNewsClient(region="jp-jp")
        client._news = Mock(  # type: ignore[method-assign]
            return_value=[
                {
                    "title": "Denso results",
                    "url": "https://www.nikkei.com/a",
                    "body": "決算",
                    "date": "2026-08-17T01:00:00+09:00",
                    "source": "Nikkei",
                }
            ]
        )
        result = client.search("デンソー IR", max_results=5, window_hours=24)
        self.assertEqual(result.provider, "ddg")
        self.assertEqual(result.items[0].url, "https://www.nikkei.com/a")
        self.assertEqual(result.items[0].source_domain, "nikkei.com")
        kwargs = client._news.call_args.kwargs
        self.assertEqual(kwargs["timelimit"], "d")
        self.assertEqual(client.backend, "duckduckgo")


class PipelineDdgFallbackTests(unittest.TestCase):
    def _item(self) -> MitaSearchResultItem:
        return MitaSearchResultItem(
            title="デンソー 決算",
            url="https://www.nikkei.com/denso",
            snippet="決算発表",
            published_at=datetime.now(timezone.utc).isoformat(),
            source_domain="www.nikkei.com",
        )

    def test_module_c_empty_mita_calls_ddg(self) -> None:
        with isolated_session() as db:
            mita = Mock()
            mita.search.return_value = MitaSearchResponse(query="q", items=[])
            ddg = Mock()
            ddg.search.return_value = MitaSearchResponse(
                query="デンソー OR Denso IR",
                items=[self._item()],
                provider="ddg",
            )
            pipeline = RiskPipeline(db, mita=mita, rss=Mock(), ddg=ddg)
            pipeline.mita_pause = 0
            funnel = {"mita_fetched": 0, "ddg_fallback": 0, "ddg_fetched": 0}
            batch = pipeline._collect_mita(
                module_code="C",
                query="デンソー OR Denso 適時開示 最新 过去24小时 OR today OR 速报",
                metadata={"company": "デンソー"},
                whitelist=[],
                blacklist=[],
                funnel=funnel,
            )
            ddg.search.assert_called_once()
            self.assertEqual(batch["metadata"]["search_provider"], "ddg")
            self.assertEqual(len(batch["items"]), 1)
            self.assertEqual(funnel["ddg_fallback"], 1)
            called_query = ddg.search.call_args.kwargs["query"]
            self.assertNotIn("过去24小时", called_query)
            self.assertIn("デンソー", called_query)

    def test_module_d_empty_mita_does_not_call_ddg(self) -> None:
        with isolated_session() as db:
            mita = Mock()
            mita.search.return_value = MitaSearchResponse(query="q", items=[])
            ddg = Mock()
            pipeline = RiskPipeline(db, mita=mita, rss=Mock(), ddg=ddg)
            pipeline.mita_pause = 0
            funnel = {"mita_fetched": 0, "ddg_fallback": 0, "ddg_fetched": 0}
            batch = pipeline._collect_mita(
                module_code="D",
                query="原油 市场 新闻",
                metadata={},
                whitelist=[],
                blacklist=[],
                funnel=funnel,
            )
            ddg.search.assert_not_called()
            self.assertEqual(batch["items"], [])
            self.assertEqual(funnel["ddg_fallback"], 0)

    def test_japan_entity_not_found_error_calls_ddg(self) -> None:
        with isolated_session() as db:
            mita = Mock()
            mita.search.side_effect = RuntimeError("秘塔搜索失败: 未找到相关数据")
            ddg = Mock()
            ddg.search.return_value = MitaSearchResponse(
                query="rewritten",
                items=[self._item()],
                provider="ddg",
            )
            pipeline = RiskPipeline(db, mita=mita, rss=Mock(), ddg=ddg)
            pipeline.mita_pause = 0
            funnel = {"mita_fetched": 0, "ddg_fallback": 0, "ddg_fetched": 0}
            batch = pipeline._collect_mita(
                module_code="A",
                query="三菱商事 司法与行政监管 新闻 动态 最新 过去24小时 OR today OR 速报",
                metadata={"target": "三菱商事", "category": "司法与行政监管"},
                whitelist=[],
                blacklist=[],
                funnel=funnel,
            )
            ddg.search.assert_called_once()
            self.assertEqual(batch["metadata"]["search_provider"], "ddg")
            self.assertNotIn("司法与行政监管", ddg.search.call_args.kwargs["query"])

    def test_godiva_does_not_call_ddg(self) -> None:
        with isolated_session() as db:
            mita = Mock()
            mita.search.side_effect = RuntimeError("秘塔搜索失败: 未找到相关数据")
            ddg = Mock()
            pipeline = RiskPipeline(db, mita=mita, rss=Mock(), ddg=ddg)
            pipeline.mita_pause = 0
            funnel = {"mita_fetched": 0}
            with self.assertRaises(RuntimeError):
                pipeline._collect_mita(
                    module_code="A",
                    query="Godiva 司法与行政监管 新闻 动态 最新 过去24小时 OR today OR 速报",
                    metadata={"target": "Godiva"},
                    whitelist=[],
                    blacklist=[],
                    funnel=funnel,
                )
            ddg.search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
