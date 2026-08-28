from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.schemas import GroundedPromotionRequest
from app.services.grounded_report import GroundedPromotionError, GroundedReportService
from tests.formal_grounded_helpers import FormalFakeAnalyzer, grounded_candidate, make_ready_report
from tests.helpers import isolated_session


class GroundedApprovalUiTests(unittest.TestCase):
    def test_manual_approval_note_is_required_by_api_schema_and_service(self):
        with self.assertRaises(ValidationError):
            GroundedPromotionRequest(promotion_note="")
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            run = GroundedReportService(
                db, FormalFakeAnalyzer(generated=grounded_candidate(card)),
            ).generate(report.id)
            with self.assertRaises(GroundedPromotionError) as caught:
                GroundedReportService(db).promote(
                    report.id, run.id, promotion_type="manual", promotion_note=" ",
                )
            self.assertEqual(caught.exception.code, "PROMOTION_NOTE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
