from __future__ import annotations

import unittest
from unittest.mock import patch

from app.api import routes
from app.database.models import IndustryReport
from app.schemas import EvidenceExtractRequest
from app.services.evidence_cards import EvidenceCardService
from tests.helpers import isolated_session
from tests.test_evidence_cards import FakeAnalyzer, add_source


class EvidenceApiTests(unittest.TestCase):
    def test_required_routes_exist_without_changing_report_routes(self):
        paths = {route.path for route in routes.router.routes}
        self.assertIn("/industry/reports/{report_id}/evidence/extract", paths)
        self.assertIn("/industry/reports/{report_id}/evidence", paths)
        self.assertIn("/industry/reports/{report_id}/evidence/{evidence_code}", paths)
        self.assertIn("/industry/reports/{report_id}/evidence-runs", paths)
        self.assertIn("/industry/reports/{report_id}/generate", paths)

    def test_extract_list_detail_and_runs_are_report_scoped(self):
        with isolated_session() as db:
            report = IndustryReport(industry_name="A", status="draft")
            other = IndustryReport(industry_name="B", status="draft")
            db.add_all([report, other]); db.commit()
            add_source(db, report, "收入100万元。")
            fake = FakeAnalyzer([{
                "original_quote": "收入100万元。", "normalized_claim": "收入100万元", "claim_type": "fact",
                "raw_value": "100", "importance_score": 4, "risk_tags": ["revenue_model"],
            }])
            with patch.object(routes, "EvidenceCardService", side_effect=lambda session: EvidenceCardService(session, fake)):
                run = routes.extract_industry_evidence(report.id, EvidenceExtractRequest(), db)
                cards = routes.list_industry_evidence(
                    report.id, source_id=None, validation_status=None, claim_type=None,
                    risk_tag="revenue_model", db=db,
                )
                detail = routes.get_industry_evidence_card(report.id, cards[0]["evidence_code"], db)
            runs = routes.list_industry_evidence_runs(report.id, db)
            other_cards = EvidenceCardService(db, FakeAnalyzer()).list_cards(other.id)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(detail["report_id"], report.id)
            self.assertEqual(len(runs), 1)
            self.assertEqual(other_cards, [])


if __name__ == "__main__":
    unittest.main()
