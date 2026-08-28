"""界面一采集韧性与历史日按日检索。"""

import unittest
from datetime import date

from app.config import module_search_queries
from app.services.rss_news import (
    feed_request_headers,
    google_date_range_clause,
    google_day_range_clause,
    strip_google_day_scope,
    with_google_date_range,
    with_google_day_scope,
)


class NewsDayScopeTests(unittest.TestCase):
    def test_google_date_range_clause(self):
        start = date(2026, 5, 15)
        end = date(2026, 8, 13)
        self.assertEqual(
            google_date_range_clause(start, end),
            "after:2026-05-15 before:2026-08-14",
        )

    def test_with_google_date_range_idempotent(self):
        start = date(2026, 5, 15)
        end = date(2026, 8, 13)
        q = with_google_date_range("Godiva", start, end)
        self.assertIn("after:2026-05-15", q)
        self.assertEqual(with_google_date_range(q, start, end), q)

    def test_google_day_range_clause(self):
        d = date(2026, 8, 1)
        self.assertEqual(
            google_day_range_clause(d), "after:2026-08-01 before:2026-08-02"
        )

    def test_with_google_day_scope_idempotent(self):
        d = date(2026, 8, 1)
        q = with_google_day_scope("中东 新闻", d)
        self.assertIn("after:2026-08-01", q)
        self.assertEqual(with_google_day_scope(q, d), q)

    def test_strip_google_day_scope(self):
        d = date(2026, 8, 1)
        q = with_google_day_scope("中东 新闻", d)
        self.assertEqual(strip_google_day_scope(q), "中东 新闻")

    def test_google_news_uses_browser_user_agent(self):
        headers = feed_request_headers(
            "https://news.google.com/rss/search?q=test&hl=en-US&gl=US&ceid=US:en"
        )
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertNotIn("RiskIntelBot", headers["User-Agent"])
        direct = feed_request_headers("https://www.nhk.or.jp/rss/news/cat0.xml")
        self.assertIn("RiskIntelBot", direct["User-Agent"])

    def test_module_search_queries_calendar_day(self):
        d = date(2026, 8, 1)
        qs = module_search_queries("B", d.isoformat(), calendar_day=d)
        self.assertTrue(qs)
        self.assertIn("2026-08-01", qs[0]["query"])
        self.assertNotIn("过去24小时", qs[0]["query"])

    def test_module_search_queries_recent_default(self):
        qs = module_search_queries("B", date.today().isoformat(), window_hours=24)
        self.assertIn("过去24小时", qs[0]["query"])


if __name__ == "__main__":
    unittest.main()
