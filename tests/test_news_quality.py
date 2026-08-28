import unittest

from app.services.news_quality import is_substantive_news_item
from app.services.display_zh import format_news_overview
from app.services.news_section_router import item_in_module_scope


class NewsQualityTests(unittest.TestCase):
    def test_rejects_generic_listing_title_and_date_index(self) -> None:
        self.assertFalse(
            is_substantive_news_item(
                {
                    "title": "公布事项",
                    "snippet": "2026-05-21 2026-05-09 2026-05-08 2026-04-01",
                    "url": "https://example.com/news/list",
                }
            )
        )

    def test_keeps_real_item_with_one_published_date(self) -> None:
        self.assertTrue(
            is_substantive_news_item(
                {
                    "title": "公司公布新融资安排",
                    "snippet": "公司于 2026-06-24 公布新的长期融资安排及资金用途。",
                    "url": "https://example.com/news/financing",
                }
            )
        )

    def test_overview_hides_summary_when_it_repeats_title(self) -> None:
        overview = format_news_overview(
            title="公司公布新融资安排",
            summary="公司公布新融资安排",
            source_name="公司官网",
            source_url="https://example.com/news/financing",
            published_at="2026-06-24",
            subject="示例公司",
        )
        self.assertEqual(overview, "")

    def test_overview_keeps_detailed_summary(self) -> None:
        overview = format_news_overview(
            title="市场动态",
            summary="相关企业公布新的融资安排，资金将用于扩建物流设施并支持多个地区项目推进。后续将披露更详细的执行计划。",
            source_name="公司官网",
            source_url="https://example.com/news/financing",
        )
        self.assertEqual(overview, "相关企业公布新的融资安排，资金将用于扩建物流设施并支持多个地区项目推进。后续将披露更详细的执行计划。")

    def test_daily_scope_rejects_non_financial_story(self) -> None:
        ok, reason = item_in_module_scope(
            "B",
            title="中东文化节举办开幕活动",
            content="活动展示当地艺术与旅游资源。",
        )
        self.assertFalse(ok)
        self.assertIn("排除", reason)

    def test_daily_scope_keeps_financial_middle_east_story(self) -> None:
        ok, _reason = item_in_module_scope(
            "D",
            title="红海航运风险推升原油与运输成本",
            content="市场关注油价和物流费用上升。",
        )
        self.assertTrue(ok)

    def test_japan_enterprise_requires_primary_source(self) -> None:
        ok, reason = item_in_module_scope(
            "C",
            title="三菱商事发布季度业绩",
            content="公司披露营业收入、利润和现金流变化。",
            source="sohu.com",
        )
        self.assertFalse(ok)
        self.assertIn("可信财经媒体", reason)

    def test_japan_enterprise_keeps_company_ir_source(self) -> None:
        ok, _reason = item_in_module_scope(
            "C",
            title="三菱商事发布季度业绩",
            content="公司披露营业收入、利润和现金流变化。",
            source="https://www.mitsubishicorp.com/jp/en/news/release/2026/example.html",
        )
        self.assertTrue(ok)

    def test_japan_enterprise_keeps_trusted_financial_media(self) -> None:
        ok, _reason = item_in_module_scope(
            "C",
            title="三井物产公布季度利润与股票回购计划",
            content="公司披露利润、现金流及股份回购安排。",
            source="日本経済新聞",
        )
        self.assertTrue(ok)
