from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api import routes
from app.schemas import GroundedPromotionRequest
from app.services.grounded_report import GroundedReportService
from app.services.industry_analysis import IndustryAnalysisService
from tests.formal_grounded_helpers import (
    FormalFakeAnalyzer, grounded_candidate, grounded_settings, make_ready_report,
)
from tests.helpers import isolated_session


class FormalGenerationApiTests(unittest.TestCase):
    def test_readiness_and_promotion_routes_exist(self):
        paths = {route.path for route in routes.router.routes}
        self.assertIn("/industry/reports/{report_id}/grounded-readiness", paths)
        self.assertIn("/industry/reports/{report_id}/grounded-runs/{run_id}/promote", paths)
        self.assertIn("/industry/reports/{report_id}", paths)

    def test_readiness_endpoint_is_read_only(self):
        with isolated_session() as db:
            report, *_ = make_ready_report(db)
            before = report.status
            result = routes.get_grounded_readiness(report.id, db)
            self.assertTrue(result["ready"])
            self.assertEqual(report.status, before)

    def test_formal_generate_uses_server_grounded_policy(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            settings = grounded_settings(approval=True)
            analyzer = FormalFakeAnalyzer(generated=grounded_candidate(card))
            with patch.object(
                routes, "IndustryAnalysisService",
                side_effect=lambda session: IndustryAnalysisService(
                    session, deepseek=analyzer, settings=settings,
                ),
            ):
                result = routes.generate_industry_report(report.id, db)
            self.assertEqual(result.status, "awaiting_approval")
            self.assertEqual(analyzer.legacy_calls, 0)

    def test_precondition_error_has_stable_code_and_next_step(self):
        with isolated_session() as db:
            from app.database.models import IndustryReport
            report = IndustryReport(industry_name="empty", status="draft")
            db.add(report)
            db.commit()
            settings = grounded_settings()
            with patch.object(
                routes, "IndustryAnalysisService",
                side_effect=lambda session: IndustryAnalysisService(session, settings=settings),
            ):
                with self.assertRaises(HTTPException) as caught:
                    routes.generate_industry_report(report.id, db)
            self.assertEqual(caught.exception.status_code, 412)
            self.assertIn("code", caught.exception.detail)
            self.assertIn("next_step", caught.exception.detail)

    def test_manual_promote_endpoint_uses_server_mode(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            fake = FormalFakeAnalyzer(generated=grounded_candidate(card))
            service = GroundedReportService(db, fake)
            run = service.generate(report.id)
            with patch.object(routes, "get_settings", return_value=grounded_settings()), patch.object(
                routes, "GroundedReportService", side_effect=lambda session: GroundedReportService(session, fake),
            ):
                result = routes.promote_grounded_report_run(
                    report.id, run.id, GroundedPromotionRequest(promotion_note="approved"), db,
                )
            self.assertEqual((result.status, result.promotion_type), ("completed", "manual"))


if __name__ == "__main__":
    unittest.main()
