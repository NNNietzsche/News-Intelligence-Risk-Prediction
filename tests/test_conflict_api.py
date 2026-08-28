from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.api import routes
from app.schemas import ConflictResolutionRequest
from tests.helpers import isolated_session
from tests.test_conflict_detection import add_card, setup_report


class ConflictApiTests(unittest.TestCase):
    def test_required_routes_exist_and_report_generation_is_unchanged(self):
        paths = {route.path for route in routes.router.routes}
        for path in (
            "/industry/reports/{report_id}/conflicts/detect",
            "/industry/reports/{report_id}/conflicts",
            "/industry/reports/{report_id}/conflicts/{conflict_code}",
            "/industry/reports/{report_id}/conflict-runs",
        ):
            self.assertIn(path, paths)
        self.assertIn("/industry/reports/{report_id}/generate", paths)

    def test_detect_list_detail_filter_and_resolve(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            first_source, _, first = add_card(db, report, extraction_run, "E000001", "100")
            add_card(db, report, extraction_run, "E000002", "120")
            extra_source, _, extra = add_card(
                db, report, extraction_run, "E000003", "30", metric="净利润"
            )
            run = routes.detect_industry_conflicts(report.id, db)
            rows = routes.list_industry_conflicts(
                report.id, conflict_type="numeric_mismatch", severity=None,
                resolution_status=None, source_id=first_source.id,
                evidence_code=first.evidence_code, db=db,
            )
            detail = routes.get_industry_conflict(report.id, rows[0]["conflict_code"], db)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(len(detail["members"]), 2)
            with self.assertRaises(HTTPException):
                routes.resolve_industry_conflict(
                    report.id, rows[0]["conflict_code"],
                    ConflictResolutionRequest(
                        resolution_status="resolved_selected", resolution_note="采用经审计披露值",
                        selected_evidence_code=extra.evidence_code,
                    ), db,
                )
            resolved = routes.resolve_industry_conflict(
                report.id, rows[0]["conflict_code"],
                ConflictResolutionRequest(
                    resolution_status="resolved_selected", resolution_note="采用经审计披露值",
                    selected_evidence_code=first.evidence_code,
                ), db,
            )
            self.assertEqual(resolved["resolution_status"], "resolved_selected")
            self.assertEqual(len(routes.list_industry_conflict_runs(report.id, db)), 1)

    def test_resolution_note_is_required_by_schema(self):
        with self.assertRaises(ValueError):
            ConflictResolutionRequest(
                resolution_status="not_a_conflict", resolution_note=""
            )


if __name__ == "__main__":
    unittest.main()
