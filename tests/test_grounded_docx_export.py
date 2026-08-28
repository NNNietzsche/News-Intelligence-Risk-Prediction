from __future__ import annotations

import unittest

from app.exporters.docx_report import build_industry_report_docx
from app.services.citation_rendering import build_citation_context
from app.services.grounded_report import GroundedReportService
from tests.formal_grounded_helpers import FormalFakeAnalyzer, grounded_candidate, make_ready_report
from tests.helpers import isolated_session


class GroundedDocxExportTests(unittest.TestCase):
    def test_grounded_docx_uses_display_citations_and_three_appendices(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            run = GroundedReportService(
                db, FormalFakeAnalyzer(generated=grounded_candidate(card)),
            ).generate(report.id)
            GroundedReportService(db).promote(
                report.id, run.id, promotion_type="manual", promotion_note="approved",
            )
            document = build_industry_report_docx(report, build_citation_context(db, report))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            self.assertIn("[1]", text)
            self.assertNotIn("[E000001]", text)
            self.assertIn("附录一：引用与来源", text)
            self.assertIn("附录二：冲突与限制", text)
            self.assertIn("附录三：证据覆盖", text)
            self.assertNotIn("C:/", text)

    def test_legacy_docx_has_explicit_notice_without_grounded_appendices(self):
        from app.database.models import IndustryReport

        report = IndustryReport(
            industry_name="传统报告", status="completed", generation_mode="legacy",
            report_json='{"summary":"传统正文","sections":[],"risk_outlook":"","key_metrics":[]}',
            version=1,
        )
        text = "\n".join(p.text for p in build_industry_report_docx(report).paragraphs)
        self.assertIn("不是证据约束报告", text)
        self.assertNotIn("附录一：引用与来源", text)

    def test_legacy_docx_includes_saved_source_list(self) -> None:
        from app.database.models import IndustryDataSource, IndustryReport

        report = IndustryReport(
            industry_name="传统报告", status="completed", generation_mode="legacy",
            report_json='{"summary":"传统正文","sections":[],"risk_outlook":"","key_metrics":[]}',
            version=1,
        )
        sources = [
            IndustryDataSource(
                id=1, report_id=1, name="行业展望", source_type="network_search",
                source_origin="network_search", url="https://example.com/a",
            )
        ]
        text = "\n".join(
            p.text for p in build_industry_report_docx(report, sources=sources).paragraphs
        )
        self.assertIn("来源列表", text)
        self.assertIn("网络搜索", text)
        self.assertIn("行业展望", text)


if __name__ == "__main__":
    unittest.main()
