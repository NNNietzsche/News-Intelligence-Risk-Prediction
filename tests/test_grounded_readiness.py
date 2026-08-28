from __future__ import annotations

import unittest

from app.services.grounded_readiness import check_grounded_readiness
from tests.formal_grounded_helpers import make_ready_report
from tests.helpers import isolated_session


class GroundedReadinessTests(unittest.TestCase):
    def test_current_extraction_conflict_and_verified_evidence_are_ready(self):
        with isolated_session() as db:
            report, *_ = make_ready_report(db)
            result = check_grounded_readiness(db, report.id)
            self.assertTrue(result["ready"])
            self.assertEqual(result["evidence_count"], 1)

    def test_no_verified_evidence_blocks_without_model_work(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            card.validation_status = "rejected"
            db.commit()
            result = check_grounded_readiness(db, report.id)
            codes = {item["code"] for item in result["blocking_errors"]}
            self.assertIn("NO_VERIFIED_EVIDENCE", codes)

    def test_source_change_marks_extraction_and_conflict_snapshots_stale(self):
        with isolated_session() as db:
            report, _, source, _, _ = make_ready_report(db)
            source.extracted_text_hash = "f" * 64
            db.commit()
            result = check_grounded_readiness(db, report.id)
            codes = {item["code"] for item in result["blocking_errors"]}
            self.assertIn("EVIDENCE_SNAPSHOT_STALE", codes)

    def test_new_evidence_after_detection_marks_conflict_snapshot_stale(self):
        from tests.test_conflict_detection import add_card

        with isolated_session() as db:
            report, extraction_run, *_ = make_ready_report(db)
            add_card(db, report, extraction_run, "E000002", "200", metric="利润")
            result = check_grounded_readiness(db, report.id)
            codes = {item["code"] for item in result["blocking_errors"]}
            self.assertIn("CONFLICT_SNAPSHOT_STALE", codes)

    def test_cross_source_chunk_binding_is_blocked(self):
        from tests.test_conflict_detection import add_card

        with isolated_session() as db:
            report, extraction_run, _, _, card = make_ready_report(db)
            other_source, _, _ = add_card(
                db, report, extraction_run, "E000002", "200", metric="利润",
            )
            card.source_id = other_source.id
            db.commit()
            result = check_grounded_readiness(db, report.id)
            self.assertFalse(result["ready"])
            self.assertTrue(any(
                item["code"] in {"CRITICAL_EVIDENCE_INVALID", "EVIDENCE_SNAPSHOT_STALE"}
                for item in result["blocking_errors"]
            ))

    def test_resolved_selected_evidence_becoming_stale_blocks(self):
        from app.services.conflict_detection import ConflictDetectionService
        from app.services.evidence_cards import compute_source_snapshot_hash
        from app.database.models import IndustryDataSource, IndustrySourceChunk
        from tests.test_conflict_detection import add_card

        with isolated_session() as db:
            report, extraction_run, *_ = make_ready_report(db)
            _, _, selected = add_card(db, report, extraction_run, "E000002", "200")
            sources = db.query(IndustryDataSource).filter_by(report_id=report.id).all()
            chunks = db.query(IndustrySourceChunk).filter_by(report_id=report.id).all()
            extraction_run.source_snapshot_hash = compute_source_snapshot_hash(sources, chunks)
            db.commit()
            service = ConflictDetectionService(db)
            service.detect(report.id)
            conflict = service.list_conflicts(report.id)[0]
            service.resolve(
                report.id, conflict.conflict_code, "resolved_selected", "人工选择", selected.evidence_code,
            )
            selected.validation_status = "stale"
            selected.requires_manual_review = True
            db.commit()
            result = check_grounded_readiness(db, report.id)
            self.assertFalse(result["ready"])
            codes = {item["code"] for item in result["blocking_errors"]}
            self.assertTrue({"CONFLICT_SNAPSHOT_STALE", "CRITICAL_EVIDENCE_INVALID"} & codes)


if __name__ == "__main__":
    unittest.main()
