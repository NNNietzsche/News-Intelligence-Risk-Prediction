from __future__ import annotations

import unittest
from unittest.mock import patch

from app.api import routes
from app.services.grounded_report import GroundedReportService
from app.services.deepseek_analyzer import GroundedReportOutputError
from tests.helpers import isolated_session
from tests.test_conflict_detection import add_card, setup_report
from tests.test_grounded_report_flow import FakeGroundedAnalyzer, candidate_for


class GroundedReportApiTests(unittest.TestCase):
    def test_shadow_routes_exist_and_formal_generate_route_remains(self):
        paths = {route.path for route in routes.router.routes}
        for path in (
            "/industry/reports/{report_id}/grounded-runs/generate",
            "/industry/reports/{report_id}/grounded-runs",
            "/industry/reports/{report_id}/grounded-runs/{run_id}",
            "/industry/reports/{report_id}/grounded-runs/{run_id}/validation",
            "/industry/reports/{report_id}/generate",
        ):
            self.assertIn(path, paths)

    def test_generate_list_detail_and_validation_do_not_return_packet(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            _, _, card = add_card(db, report, extraction_run, "E000001", "100")
            fake = FakeGroundedAnalyzer(generated=candidate_for(card))
            with patch.object(
                routes, "GroundedReportService",
                side_effect=lambda session: GroundedReportService(session, fake),
            ):
                created = routes.generate_grounded_report_shadow(report.id, db)
                listed = routes.list_grounded_report_runs(report.id, db)
                detail = routes.get_grounded_report_run(report.id, created["id"], db)
                validation = routes.get_grounded_report_validation(report.id, created["id"], db)
            self.assertEqual(created["status"], "validated")
            self.assertEqual(len(listed), 1)
            self.assertIn("candidate_report", detail)
            self.assertIn("validation", validation)
            for payload in (created, listed[0], detail, validation):
                self.assertNotIn("evidence_packet", payload)

    def test_invalid_raw_model_output_is_not_returned_by_api(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            add_card(db, report, extraction_run, "E000001", "100")
            fake = FakeGroundedAnalyzer(
                generate_error=GroundedReportOutputError("secret customer text", "bad"),
                repair_error=GroundedReportOutputError("secret repaired text", "bad"),
            )
            with patch.object(
                routes, "GroundedReportService",
                side_effect=lambda session: GroundedReportService(session, fake),
            ):
                created = routes.generate_grounded_report_shadow(report.id, db)
                detail = routes.get_grounded_report_run(report.id, created["id"], db)
            self.assertEqual(created["status"], "failed")
            self.assertNotIn("secret", str(detail))


if __name__ == "__main__":
    unittest.main()
