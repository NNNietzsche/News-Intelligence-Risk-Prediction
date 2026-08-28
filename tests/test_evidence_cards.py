from __future__ import annotations

import hashlib
import unittest

from app.database.models import (
    IndustryDataSource, IndustryEvidenceCard, IndustryEvidenceExtractionRun,
    IndustryReport, IndustrySourceChunk,
)
from app.services.evidence_cards import EvidenceCardService, EvidenceExtractionError
from tests.helpers import isolated_session


class FakeAnalyzer:
    model = "fake-evidence-model"

    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error
        self.calls = 0

    def extract_evidence_candidates(self, _text):
        self.calls += 1
        if self.error:
            raise self.error
        return {"candidates": self.candidates}


def add_source(db, report, text, *, origin="customer_file", grade="full_text"):
    digest = hashlib.sha256(text.encode()).hexdigest()
    source = IndustryDataSource(
        report_id=report.id, name="source", source_type="file", extracted_text=text,
        content_hash=digest, extracted_text_hash=digest, char_count=len(text),
        source_origin=origin, evidence_grade=grade, is_full_text=grade == "full_text",
    )
    db.add(source)
    db.flush()
    chunk = IndustrySourceChunk(
        report_id=report.id, source_id=source.id, chunk_index=0, text=text,
        locator="TXT字符0-20", char_start=0, char_end=len(text), content_hash=digest,
    )
    db.add(chunk)
    db.commit()
    return source, chunk


class EvidenceCardServiceTests(unittest.TestCase):
    def test_verified_card_is_bound_to_report_source_chunk_and_quote(self):
        with isolated_session() as db:
            report = IndustryReport(industry_name="储能", status="draft")
            db.add(report); db.commit()
            source, chunk = add_source(db, report, "公司2024年度收入为3.2亿日元。")
            analyzer = FakeAnalyzer([{
                "original_quote": "公司2024年度收入为3.2亿日元。", "normalized_claim": "公司收入为3.2亿日元",
                "claim_type": "fact", "raw_value": "3.2", "currency": "JPY", "period": "2024年度",
                "importance_score": 5, "risk_tags": ["revenue_model"], "extraction_confidence": 0.9,
            }])
            run = EvidenceCardService(db, analyzer).extract(report.id)
            card = db.query(IndustryEvidenceCard).one()
            self.assertEqual(run.status, "completed")
            self.assertEqual((card.report_id, card.source_id, card.chunk_id), (report.id, source.id, chunk.id))
            self.assertEqual(chunk.text[card.quote_start:card.quote_end], card.original_quote)
            self.assertEqual(card.normalized_value, "320000000")
            self.assertEqual(card.verification_scope, "source_match")

    def test_same_snapshot_and_prompt_reuses_run_without_duplicates(self):
        with isolated_session() as db:
            report = IndustryReport(industry_name="储能", status="draft")
            db.add(report); db.commit()
            add_source(db, report, "市场规模为100亿元。")
            analyzer = FakeAnalyzer([{
                "original_quote": "市场规模为100亿元。", "normalized_claim": "市场规模100亿元",
                "claim_type": "fact", "raw_value": "100", "importance_score": 4,
                "risk_tags": ["market_size"],
            }])
            service = EvidenceCardService(db, analyzer)
            first = service.extract(report.id)
            second = service.extract(report.id)
            self.assertEqual(first.id, second.id)
            self.assertEqual(analyzer.calls, 1)
            self.assertEqual(db.query(IndustryEvidenceCard).count(), 1)

    def test_reports_never_share_sources_or_cards(self):
        with isolated_session() as db:
            first = IndustryReport(industry_name="A", status="draft")
            second = IndustryReport(industry_name="B", status="draft")
            db.add_all([first, second]); db.commit()
            source, _ = add_source(db, first, "收入100万元。")
            with self.assertRaisesRegex(ValueError, "source_not_found_for_report"):
                EvidenceCardService(db, FakeAnalyzer()).extract(second.id, source.id)

    def test_changed_chunk_hash_marks_existing_card_stale(self):
        with isolated_session() as db:
            report = IndustryReport(industry_name="A", status="draft")
            db.add(report); db.commit()
            _, chunk = add_source(db, report, "收入100万元。")
            analyzer = FakeAnalyzer([{
                "original_quote": "收入100万元。", "normalized_claim": "收入100万元", "claim_type": "fact",
                "raw_value": "100", "importance_score": 4, "risk_tags": ["revenue_model"],
            }])
            service = EvidenceCardService(db, analyzer)
            service.extract(report.id)
            chunk.content_hash = "0" * 64
            db.commit()
            card = service.list_cards(report.id)[0]
            self.assertEqual(card.validation_status, "stale")

    def test_failed_run_rolls_back_cards_but_not_sources(self):
        with isolated_session() as db:
            report = IndustryReport(industry_name="A", status="draft")
            db.add(report); db.commit()
            source, _ = add_source(db, report, "收入100万元。")
            with self.assertRaises(EvidenceExtractionError):
                EvidenceCardService(db, FakeAnalyzer(error=ValueError("bad schema secret"))).extract(report.id)
            self.assertIsNotNone(db.get(IndustryDataSource, source.id))
            self.assertEqual(db.query(IndustryEvidenceCard).count(), 0)
            run = db.query(IndustryEvidenceExtractionRun).one()
            self.assertEqual(run.status, "failed")
            self.assertNotIn("secret", run.error_message)

    def test_low_importance_is_skipped_but_negative_event_is_kept(self):
        with isolated_session() as db:
            report = IndustryReport(industry_name="A", status="draft")
            db.add(report); db.commit()
            add_source(db, report, "背景信息。发生安全事故。")
            analyzer = FakeAnalyzer([
                {"original_quote": "背景信息。", "normalized_claim": "背景", "claim_type": "fact", "importance_score": 1, "risk_tags": []},
                {"original_quote": "发生安全事故。", "normalized_claim": "发生安全事故", "claim_type": "fact", "importance_score": 2, "risk_tags": ["safety_accident"]},
            ])
            run = EvidenceCardService(db, analyzer).extract(report.id)
            self.assertEqual(run.candidate_count, 2)
            self.assertEqual(db.query(IndustryEvidenceCard).count(), 1)


if __name__ == "__main__":
    unittest.main()
