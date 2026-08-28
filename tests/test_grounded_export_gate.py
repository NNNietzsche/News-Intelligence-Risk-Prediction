from __future__ import annotations

import unittest

from app.services.citation_rendering import CitationPresentationError, build_citation_context
from app.services.grounded_report import GroundedReportService
from tests.formal_grounded_helpers import FormalFakeAnalyzer, grounded_candidate, make_ready_report
from tests.helpers import isolated_session


class GroundedExportGateTests(unittest.TestCase):
    def test_current_promoted_report_passes_and_changed_snapshot_is_blocked(self):
        with isolated_session() as db:
            report, _, source, _, card = make_ready_report(db)
            run = GroundedReportService(
                db, FormalFakeAnalyzer(generated=grounded_candidate(card)),
            ).generate(report.id)
            GroundedReportService(db).promote(
                report.id, run.id, promotion_type="manual", promotion_note="approved",
            )
            build_citation_context(db, report, enforce_export_gate=True)
            source.extracted_text_hash = "f" * 64
            db.commit()
            with self.assertRaises(CitationPresentationError) as caught:
                build_citation_context(db, report, enforce_export_gate=True)
            self.assertEqual(caught.exception.code, "GROUNDED_EXPORT_SNAPSHOT_STALE")


if __name__ == "__main__":
    unittest.main()
