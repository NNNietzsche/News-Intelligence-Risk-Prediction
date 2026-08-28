from __future__ import annotations

import unittest

from app.api import routes
from app.services.grounded_report import GroundedReportService
from tests.formal_grounded_helpers import FormalFakeAnalyzer, grounded_candidate, make_ready_report
from tests.helpers import isolated_session


class CitationApiTests(unittest.TestCase):
    def test_quote_limit_does_not_split_numeric_unit_or_negation_token(self):
        from app.services.citation_rendering import _bounded_quote, _safe_url

        prefix = "甲" * 1397
        shown, truncated = _bounded_quote(prefix + "100亿元尚未确认" + "乙" * 100)
        self.assertTrue(truncated)
        self.assertFalse(shown.endswith("10（摘录已截断）"))
        self.assertNotIn("100亿（摘录已截断）", shown)
        self.assertIsNone(_safe_url("javascript:alert(1)"))
        self.assertEqual(_safe_url("https://example.com/a"), "https://example.com/a")

    def test_report_scoped_detail_omits_internal_storage_fields(self):
        with isolated_session() as db:
            report, _, source, _, card = make_ready_report(db)
            source.url = "https://example.com/report"
            source.file_path = "C:/private/customer.pdf"
            db.commit()
            from app.services.conflict_detection import ConflictDetectionService
            ConflictDetectionService(db).detect(report.id)
            run = GroundedReportService(
                db, FormalFakeAnalyzer(generated=grounded_candidate(card)),
            ).generate(report.id)
            GroundedReportService(db).promote(
                report.id, run.id, promotion_type="manual", promotion_note="approved",
            )
            detail = routes.get_industry_report_citation(report.id, card.evidence_code, db)
            self.assertEqual(detail["url"], "https://example.com/report")
            self.assertNotIn("file_path", detail)
            self.assertNotIn("content_hash", detail)
            self.assertNotIn("chunk", detail)

    def test_foreign_report_cannot_read_evidence_code(self):
        from fastapi import HTTPException

        with isolated_session() as db:
            first, *_ = make_ready_report(db)
            second, _, _, _, card = make_ready_report(db, value="200")
            self.assertNotEqual(first.id, second.id)
            with self.assertRaises(HTTPException):
                routes.get_industry_report_citation(first.id, card.evidence_code, db)


if __name__ == "__main__":
    unittest.main()
