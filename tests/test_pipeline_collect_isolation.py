"""采集期间不占写锁、配置冻结到下次任务。"""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.api.routes import _source_mutation_payload
from app.database.models import DomainBlacklist, DomainWhitelist, SearchLog
from app.services.mita_search import MitaSearchResponse
from app.services.pipeline import RiskPipeline
from app.services.pipeline_runner import _build_job_snapshot
from app.services.rss_config import reload_rss_config
from app.services.rss_news import RssNewsCollector
from tests.helpers import isolated_session


class PipelineCollectIsolationTests(unittest.TestCase):
    def test_collect_mita_does_not_write_before_search_returns(self) -> None:
        with isolated_session() as db:
            seen: dict[str, object] = {}

            def fake_search(**_kwargs):
                seen["new"] = list(db.new)
                seen["dirty"] = list(db.dirty)
                seen["logs"] = db.query(SearchLog).count()
                return MitaSearchResponse(query="q", items=[])

            mita = Mock()
            mita.search.side_effect = fake_search
            pipeline = RiskPipeline(db, mita=mita, rss=Mock())
            funnel = {"mita_fetched": 0}
            batch = pipeline._collect_mita(
                module_code="D",
                query="宏观 市场",
                metadata={},
                whitelist=["example.com"],
                blacklist=[],
                funnel=funnel,
            )
            self.assertEqual(seen["new"], [])
            self.assertEqual(seen["dirty"], [])
            self.assertEqual(seen["logs"], 0)
            self.assertEqual(db.query(SearchLog).count(), 1)
            self.assertTrue(batch["search_log_id"])

    def test_job_snapshot_freezes_domain_lists(self) -> None:
        with isolated_session() as db:
            db.add(DomainWhitelist(domain="frozen.example", module_code="D", is_active=True))
            db.add(DomainBlacklist(domain="blocked.example", is_active=True))
            db.commit()
            snap = _build_job_snapshot(db)
            db.add(DomainWhitelist(domain="later.example", module_code="D", is_active=True))
            db.commit()
            self.assertIn("frozen.example", snap["whitelist_by_module"]["D"])
            self.assertNotIn("later.example", snap["whitelist_by_module"]["D"])
            self.assertIn("blocked.example", snap["blacklist"])

    def test_rss_reload_does_not_mutate_running_collector_config(self) -> None:
        collector = RssNewsCollector()
        frozen = collector.config
        reloaded = reload_rss_config()
        self.assertIs(collector.config, frozen)
        self.assertIsNot(reloaded, frozen)

    def test_source_mutation_defers_to_next_run_when_collecting(self) -> None:
        row = SimpleNamespace(
            id=1,
            name="src",
            source_type="file",
            original_filename="a.txt",
            url=None,
            priority=0,
            extracted_text="text",
            created_at=None,
        )
        with patch("app.api.routes.get_running_job_id", return_value="job123"):
            payload = _source_mutation_payload(row, message="数据源已上传")
        self.assertTrue(payload["deferred_to_next_run"])
        self.assertEqual(payload["running_job_id"], "job123")
        self.assertIn("下次采集", payload["message"])


class GoogleSupplementFallbackTests(unittest.TestCase):
    def test_google_supplement_retries_without_day_scope(self) -> None:
        from datetime import datetime, timezone

        from app.services.rss_news import CollectResult, RssNewsItem

        calls: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()

        def fake_collect(*_args, **kwargs):
            calls.append(kwargs)
            if int(kwargs.get("hours") or 24) <= 24:
                return CollectResult(fetch_errors=2, fetch_ok=0, items=[])
            item = RssNewsItem(
                title="美联储维持利率不变",
                url="https://www.reuters.com/fed",
                snippet="全球资本市场关注利率决议",
                published_at=now,
                source_domain="reuters.com",
                feed_label="google_fill_1",
            )
            return CollectResult(items=[item], fetch_ok=1)

        with isolated_session() as db:
            rss = Mock()
            rss.collect_detailed.side_effect = fake_collect
            pipeline = RiskPipeline(db, mita=Mock(), rss=rss)
            batch = pipeline._collect_google_query_supplement(
                module_code="D",
                report_date=date(2026, 8, 14),
                funnel={},
                fill_budget=4,
            )
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(batch["items"][0]["url"], "https://www.reuters.com/fed")

    def test_google_supplement_uses_llm_when_rss_fails(self) -> None:
        from app.services.rss_news import CollectResult

        def fake_collect(*_args, **_kwargs):
            return CollectResult(fetch_errors=3, fetch_ok=0, items=[])

        with isolated_session() as db:
            rss = Mock()
            rss.collect_detailed.side_effect = fake_collect
            pipeline = RiskPipeline(db, mita=Mock(), rss=rss)
            with patch(
                "app.services.llm_web_search.search_web_fallback",
                return_value=(
                    [
                        {
                            "title": "美联储维持利率不变",
                            "url": "https://www.reuters.com/fed",
                            "snippet": "全球资本市场关注利率决议",
                            "published_at": None,
                            "source_domain": "reuters.com",
                        }
                    ],
                    "gemini",
                ),
            ):
                batch = pipeline._collect_google_query_supplement(
                    module_code="D",
                    report_date=date(2026, 8, 14),
                    funnel={},
                    fill_budget=4,
                    allow_llm=True,
                )
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(batch["metadata"]["provider"], "gemini")
        self.assertNotIn("error", batch["metadata"])


if __name__ == "__main__":
    unittest.main()
