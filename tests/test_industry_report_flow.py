from __future__ import annotations

import unittest

from app.database.models import IndustryReport
from app.services.industry_analysis import IndustryAnalysisService
from tests.helpers import isolated_session


class _FakeAnalyzer:
    def analyze_industry(self, raw_text, industry_name, company_name=None, context=None):
        if "测试信源正文" not in raw_text:
            raise AssertionError("现有报告生成流程未收到信源正文")
        return {
            "title": f"{industry_name}测试报告",
            "summary": "固定摘要",
            "sections": [{"heading": "行业概况", "content": "固定正文"}],
            "risk_outlook": "固定展望",
            "key_metrics": [],
        }


class IndustryReportCompatibilityTests(unittest.TestCase):
    def test_history_report_delete_removes_report(self) -> None:
        with isolated_session() as db:
            report = IndustryReport(industry_name="能源", status="completed", version=1)
            db.add(report)
            db.commit()
            report_id = report.id
            self.assertTrue(IndustryAnalysisService(db).delete_report(report_id))
            self.assertIsNone(db.get(IndustryReport, report_id))

    def test_existing_report_generation_and_html_remain_compatible(self) -> None:
        from app.database.models import IndustryDataSource

        with isolated_session() as db:
            report = IndustryReport(
                industry_name="测试行业",
                company_name="测试企业",
                status="draft",
                supplement_search=False,
                version=1,
            )
            db.add(report)
            db.flush()
            db.add(
                IndustryDataSource(
                    report_id=report.id,
                    name="测试来源",
                    source_type="file",
                    extracted_text="测试信源正文",
                    char_count=6,
                )
            )
            db.commit()
            result = IndustryAnalysisService(db, deepseek=_FakeAnalyzer()).generate_report(
                report.id
            )
            self.assertEqual(result.status, "completed")
            self.assertIn("固定摘要", result.report_html)
            self.assertIn("行业概况", result.report_html)
            self.assertIn("来源列表", result.report_html)
            self.assertIn("测试来源", result.report_html)
            self.assertIn('"title": "测试行业测试报告"', result.report_json)

    def test_network_search_results_appear_in_source_list_even_if_translation_fails(self) -> None:
        from app.database.models import IndustryDataSource
        from app.services.mita_search import MitaSearchResponse, MitaSearchResultItem

        class _FailingTranslator(_FakeAnalyzer):
            def analyze_industry(self, raw_text, industry_name, company_name=None, context=None):
                if "Credit outlook 2026" not in raw_text:
                    raise AssertionError("网络搜索结果未进入报告生成输入")
                return {
                    "title": f"{industry_name}测试报告",
                    "summary": "固定摘要",
                    "sections": [{"heading": "行业概况", "content": "固定正文"}],
                    "risk_outlook": "固定展望",
                    "key_metrics": [],
                }

            def translate_network_source_to_chinese(self, title, snippet):
                raise ValueError("network_source_translation_not_chinese")

        class _FakeMita:
            def search(self, query, max_results=8, **kwargs):
                return MitaSearchResponse(
                    query=query,
                    items=[
                        MitaSearchResultItem(
                            title="Credit outlook 2026",
                            url="https://example.com/outlook",
                            snippet="Sector default risk remains elevated.",
                            source_domain="example.com",
                        )
                    ],
                    provider="mita",
                )

        with isolated_session() as db:
            report = IndustryReport(
                industry_name="测试行业",
                company_name="测试企业",
                status="draft",
                supplement_search=True,
                version=1,
            )
            db.add(report)
            db.commit()
            result = IndustryAnalysisService(
                db, deepseek=_FailingTranslator(), mita=_FakeMita(),
            ).generate_report(report.id)
            sources = db.query(IndustryDataSource).filter_by(report_id=result.id).all()
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].source_type, "network_search")
            self.assertIn("来源列表", result.report_html)
            self.assertIn("Credit outlook 2026", result.report_html)
            self.assertIn("https://example.com/outlook", result.report_html)


if __name__ == "__main__":
    unittest.main()
