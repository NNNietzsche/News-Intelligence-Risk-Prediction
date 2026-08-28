from __future__ import annotations

import hashlib
import json
import unittest

from app.database.models import (
    IndustryConflictDetectionRun, IndustryDataSource, IndustryEvidenceCard, IndustryEvidenceConflict,
    IndustryEvidenceConflictMember,
    IndustryEvidenceExtractionRun, IndustryReport, IndustrySourceChunk,
)
from app.services.conflict_detection import ConflictDetectionService
from tests.helpers import isolated_session


def setup_report(db, name="A"):
    report = IndustryReport(industry_name=name, status="draft")
    db.add(report); db.flush()
    run = IndustryEvidenceExtractionRun(
        report_id=report.id, status="completed", extractor_provider="fake", extractor_model="fake",
        prompt_version="evidence-v1", source_snapshot_hash=f"snapshot-{report.id}",
    )
    db.add(run); db.commit()
    return report, run


def add_card(
    db, report, run, code, value, *, unit=None, currency="JPY", period="2025年",
    subject="ABC株式会社", metric="营业收入", claim_type="fact", status="verified",
    manual=False, grade="full_text", used_ocr=False, raw_hash=None, url=None,
    risk_tags=None, importance=4, quote=None,
    source_origin="customer_file", published_at=None,
):
    text = quote or f"{subject}{period}{metric}为{value}{unit or currency or ''}。"
    digest = hashlib.sha256(text.encode()).hexdigest()
    source = IndustryDataSource(
        report_id=report.id, name=f"source-{code}", source_type="file", extracted_text=text,
        content_hash=digest, raw_content_hash=raw_hash or hashlib.sha256(code.encode()).hexdigest(),
        extracted_text_hash=digest, char_count=len(text),
        evidence_grade=grade, is_full_text=grade == "full_text", is_truncated=grade == "partial_text",
        used_ocr=used_ocr, url=url, source_origin=source_origin, published_at=published_at,
    )
    db.add(source); db.flush()
    chunk = IndustrySourceChunk(
        report_id=report.id, source_id=source.id, chunk_index=0, text=text, locator=f"source-{code}",
        content_hash=digest,
    )
    db.add(chunk); db.flush()
    card = IndustryEvidenceCard(
        evidence_code=code, report_id=report.id, source_id=source.id, chunk_id=chunk.id,
        extraction_run_id=run.id, dedupe_key=f"dedupe-{code}", chunk_content_hash=digest,
        locator=chunk.locator, original_quote=text, quote_start=0, quote_end=len(text),
        normalized_claim=text, claim_type=claim_type, subject=subject, metric_name=metric,
        raw_value=value, normalized_value=value, value_multiplier="1", unit=unit,
        currency=currency, period=period, importance_score=importance,
        risk_tags=json.dumps(risk_tags or []), extraction_confidence="1",
        validation_status=status, verification_scope="source_match",
        requires_manual_review=manual, source_origin=source.source_origin, evidence_grade=grade,
    )
    db.add(card); db.commit()
    return source, chunk, card


class ConflictDetectionTests(unittest.TestCase):
    def test_equivalent_power_values_do_not_conflict(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "1.2", unit="GW", currency=None, metric="装机容量")
            add_card(db, report, run, "E000002", "1200", unit="MW", currency=None, metric="装机容量")
            detected = ConflictDetectionService(db).detect(report.id)
            self.assertEqual(detected.conflict_count + detected.review_count, 0)

    def test_numeric_mismatch_requires_independent_sources(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "32000000000")
            add_card(db, report, run, "E000002", "42000000000")
            detected = ConflictDetectionService(db).detect(report.id)
            conflict = db.query(IndustryEvidenceConflict).one()
            self.assertEqual((detected.conflict_count, conflict.conflict_type), (1, "numeric_mismatch"))
            self.assertEqual(len(conflict.members), 2)

    def test_mw_and_mwh_are_uncomparable_not_numeric_conflict(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100", unit="MW", currency=None, metric="产能")
            add_card(db, report, run, "E000002", "100", unit="MWh", currency=None, metric="产能")
            ConflictDetectionService(db).detect(report.id)
            conflict = db.query(IndustryEvidenceConflict).one()
            self.assertEqual((conflict.conflict_type, conflict.resolution_status), ("unit_mismatch", "needs_review"))

    def test_currency_and_ratio_mismatches_are_not_directly_compared(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "32000000000", currency="JPY")
            add_card(db, report, run, "E000002", "32000000000", currency="CNY")
            add_card(db, report, run, "E000003", "0.082", unit="%", currency=None, metric="增长率")
            add_card(db, report, run, "E000004", "8.2", unit="倍", currency=None, metric="增长率")
            ConflictDetectionService(db).detect(report.id)
            types = {row.conflict_type for row in db.query(IndustryEvidenceConflict).all()}
            self.assertEqual(types, {"currency_mismatch", "unit_mismatch"})

    def test_percent_and_percentage_points_are_distinct_dimensions(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "0.082", unit="%", currency=None, metric="增长率", quote="ABC株式会社2025年增长率为8.2%。")
            add_card(db, report, run, "E000002", "0.082", unit="%", currency=None, metric="增长率", quote="ABC株式会社2025年增长率提高8.2个百分点。")
            ConflictDetectionService(db).detect(report.id)
            conflict = db.query(IndustryEvidenceConflict).one()
            self.assertEqual(conflict.conflict_type, "unit_mismatch")

    def test_calendar_and_fiscal_year_only_form_review_record(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100", period="2025年")
            add_card(db, report, run, "E000002", "120", period="2025财年")
            ConflictDetectionService(db).detect(report.id)
            conflict = db.query(IndustryEvidenceConflict).one()
            self.assertEqual((conflict.conflict_type, conflict.resolution_status), ("period_mismatch", "needs_review"))

    def test_actual_and_forecast_do_not_create_numeric_mismatch(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100")
            add_card(db, report, run, "E000002", "120", claim_type="forecast", quote="ABC株式会社预计2025年营业收入为120JPY。")
            ConflictDetectionService(db).detect(report.id)
            conflict = db.query(IndustryEvidenceConflict).one()
            self.assertEqual(conflict.conflict_type, "actual_forecast_mismatch")

    def test_duplicates_are_recorded_but_not_independent_numeric_conflicts(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            same_hash = "a" * 64
            add_card(db, report, run, "E000001", "100", raw_hash=same_hash)
            add_card(db, report, run, "E000002", "120", raw_hash=same_hash)
            ConflictDetectionService(db).detect(report.id)
            conflicts = db.query(IndustryEvidenceConflict).all()
            self.assertEqual([row.conflict_type for row in conflicts], ["duplicate_source"])

    def test_updated_same_url_is_recorded_as_source_version_mismatch(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100", url="https://example.com/report", published_at="2025-01-01")
            add_card(db, report, run, "E000002", "120", url="https://example.com/report", published_at="2025-02-01")
            ConflictDetectionService(db).detect(report.id)
            conflict = db.query(IndustryEvidenceConflict).one()
            self.assertEqual((conflict.conflict_type, conflict.resolution_status), ("source_version_mismatch", "needs_review"))

    def test_approximate_or_small_difference_requires_review(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100", quote="ABC株式会社2025年营业收入约100JPY。")
            add_card(db, report, run, "E000002", "100")
            ConflictDetectionService(db).detect(report.id)
            conflict = db.query(IndustryEvidenceConflict).one()
            self.assertEqual(conflict.conflict_type, "possible_rounding_difference")

    def test_network_snippets_with_same_url_are_duplicate_sources(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100", url="https://example.com/a", source_origin="network_search")
            add_card(db, report, run, "E000002", "120", url="https://example.com/a#section", source_origin="network_search")
            ConflictDetectionService(db).detect(report.id)
            self.assertEqual(
                [row.conflict_type for row in db.query(IndustryEvidenceConflict).all()],
                ["duplicate_source"],
            )

    def test_restricted_source_discrepancy_and_exclusions(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100")
            add_card(db, report, run, "E000002", "120", status="lead_only", grade="lead_only", manual=True)
            add_card(db, report, run, "E000003", "130", status="rejected")
            add_card(db, report, run, "E000004", "140", claim_type="inference")
            add_card(db, report, run, "E000005", "150", used_ocr=True, manual=True)
            detected = ConflictDetectionService(db).detect(report.id)
            types = {row.conflict_type for row in db.query(IndustryEvidenceConflict).all()}
            self.assertIn("restricted_source_discrepancy", types)
            self.assertEqual(detected.excluded_count, 2)
            self.assertEqual(detected.conflict_count, 0)

    def test_restricted_third_source_does_not_downgrade_full_source_conflict(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100")
            add_card(db, report, run, "E000002", "120")
            add_card(db, report, run, "E000003", "130", status="lead_only", grade="lead_only", manual=True)
            detected = ConflictDetectionService(db).detect(report.id)
            types = {row.conflict_type for row in db.query(IndustryEvidenceConflict).all()}
            self.assertEqual(types, {"numeric_mismatch", "restricted_source_discrepancy"})
            self.assertEqual((detected.conflict_count, detected.review_count), (1, 1))

    def test_report_scope_snapshot_reuse_and_changed_snapshot(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            other, other_run = setup_report(db, "B")
            add_card(db, report, run, "E000001", "100")
            _, _, second = add_card(db, report, run, "E000002", "120")
            add_card(db, other, other_run, "E000001", "999")
            service = ConflictDetectionService(db)
            first_run = service.detect(report.id)
            self.assertEqual(first_run.id, service.detect(report.id).id)
            self.assertTrue(all(member.evidence_card.report_id == report.id for conflict in first_run.conflicts for member in conflict.members))
            second.normalized_value = "130"
            db.commit()
            new_run = service.detect(report.id)
            self.assertNotEqual(first_run.id, new_run.id)
            self.assertTrue(all(conflict.resolution_status == "superseded" for conflict in first_run.conflicts))

    def test_current_chunk_hash_change_creates_new_detection_run(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100")
            _, changed_chunk, _ = add_card(db, report, run, "E000002", "120")
            service = ConflictDetectionService(db)
            first = service.detect(report.id)
            changed_chunk.text += "更新"
            changed_chunk.content_hash = hashlib.sha256(changed_chunk.text.encode()).hexdigest()
            db.commit()
            second = service.detect(report.id)
            self.assertNotEqual(first.id, second.id)
            self.assertGreaterEqual(second.excluded_count, 1)

    def test_deleting_detection_run_cascades_results_not_evidence(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100")
            add_card(db, report, run, "E000002", "120")
            detected = ConflictDetectionService(db).detect(report.id)
            card_count = db.query(IndustryEvidenceCard).count()
            db.delete(detected)
            db.commit()
            self.assertEqual(db.query(IndustryConflictDetectionRun).count(), 0)
            self.assertEqual(db.query(IndustryEvidenceConflict).count(), 0)
            self.assertEqual(db.query(IndustryEvidenceConflictMember).count(), 0)
            self.assertEqual(db.query(IndustryEvidenceCard).count(), card_count)


if __name__ == "__main__":
    unittest.main()
