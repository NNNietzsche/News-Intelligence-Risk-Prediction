from __future__ import annotations

import json
import unittest

from app.database.models import IndustryGroundedReportRun
from app.services.grounded_report import GroundedPromotionError, GroundedReportService
from tests.formal_grounded_helpers import FormalFakeAnalyzer, grounded_candidate, make_ready_report
from tests.helpers import isolated_session


class GroundedPromotionTests(unittest.TestCase):
    def _validated(self, db):
        report, _, source, chunk, card = make_ready_report(db)
        analyzer = FormalFakeAnalyzer(generated=grounded_candidate(card))
        run = GroundedReportService(db, analyzer).generate(report.id)
        self.assertEqual(run.status, "validated")
        return report, source, chunk, card, run

    def test_validated_candidate_promotes_and_is_idempotent(self):
        with isolated_session() as db:
            report, _, _, _, run = self._validated(db)
            service = GroundedReportService(db, FormalFakeAnalyzer())
            promoted = service.promote(
                report.id, run.id, promotion_type="manual", promotion_note="approved",
            )
            again = service.promote(report.id, run.id, promotion_type="manual")
            self.assertEqual(promoted.id, again.id)
            self.assertEqual(promoted.status, "completed")
            self.assertEqual(promoted.citation_validation_status, "validated")
            self.assertIn("[E000001]", promoted.report_json)
            self.assertIn("[E000001]", promoted.report_html)

    def test_failed_or_foreign_run_cannot_promote(self):
        with isolated_session() as db:
            report, _, _, _, run = self._validated(db)
            run.status = "failed"
            db.commit()
            with self.assertRaises(GroundedPromotionError) as caught:
                GroundedReportService(db).promote(report.id, run.id, promotion_type="manual")
            self.assertEqual(caught.exception.code, "RUN_NOT_VALIDATED")

            other, *_ = make_ready_report(db, value="300")
            with self.assertRaises(GroundedPromotionError):
                GroundedReportService(db).promote(other.id, run.id, promotion_type="manual")

    def test_snapshot_change_blocks_promotion(self):
        with isolated_session() as db:
            report, source, _, _, run = self._validated(db)
            source.extracted_text_hash = "a" * 64
            db.commit()
            with self.assertRaises(GroundedPromotionError) as caught:
                GroundedReportService(db).promote(report.id, run.id, promotion_type="manual")
            self.assertEqual(caught.exception.code, "RUN_SNAPSHOT_STALE")

    def test_candidate_is_schema_and_citation_revalidated_before_promotion(self):
        with isolated_session() as db:
            report, _, _, card, run = self._validated(db)
            run.candidate_report_json = json.dumps(grounded_candidate(card, cited=False), ensure_ascii=False)
            db.commit()
            with self.assertRaises(GroundedPromotionError) as caught:
                GroundedReportService(db).promote(report.id, run.id, promotion_type="manual")
            self.assertEqual(caught.exception.code, "PROMOTION_VALIDATION_FAILED")
            self.assertIsNone(report.report_json)

    def test_completed_report_cannot_be_overwritten_by_another_run(self):
        with isolated_session() as db:
            report, _, _, _, run = self._validated(db)
            service = GroundedReportService(db)
            service.promote(
                report.id, run.id, promotion_type="manual", promotion_note="approved",
            )
            other = IndustryGroundedReportRun(
                report_id=report.id, evidence_snapshot_hash=run.evidence_snapshot_hash,
                conflict_snapshot_hash=run.conflict_snapshot_hash, prompt_version=run.prompt_version,
                provider="fake", model="fake", status="validated",
                candidate_report_json=run.candidate_report_json,
            )
            db.add(other)
            db.commit()
            with self.assertRaises(GroundedPromotionError):
                service.promote(report.id, other.id, promotion_type="manual")

    def test_promoted_report_uses_existing_revision_rules(self):
        from app.services.industry_analysis import IndustryAnalysisService

        with isolated_session() as db:
            report, _, _, _, run = self._validated(db)
            GroundedReportService(db).promote(
                report.id, run.id, promotion_type="manual", promotion_note="approved",
            )
            revision = IndustryAnalysisService(db).fork_report(report.id)
            self.assertEqual(revision.parent_report_id, report.id)
            self.assertEqual(revision.version, report.version + 1)
            self.assertEqual(revision.status, "draft")
            self.assertIsNone(revision.grounded_run_id)


if __name__ == "__main__":
    unittest.main()
