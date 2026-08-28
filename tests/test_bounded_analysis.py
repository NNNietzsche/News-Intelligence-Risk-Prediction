from __future__ import annotations

import unittest

from app.services.bounded_analysis_validation import validate_structured_grounded_report
from app.services.evidence_packet import build_evidence_packet
from tests.formal_grounded_helpers import make_ready_report
from tests.helpers import isolated_session


def candidate(sentence_type, text, codes, *, assumptions=None):
    sentence = {
        "sentence_type": sentence_type,
        "text": text,
        "evidence_codes": codes,
    }
    if sentence_type == "bounded_analysis":
        sentence["assumptions"] = assumptions or []
    return {
        "title": "V2结构化报告",
        "structured_sections": [{"heading": "风险分析", "sentences": [sentence]}],
        "key_metrics": [], "limitations": [], "unresolved_conflicts": [],
    }


class BoundedAnalysisTests(unittest.TestCase):
    def test_evidence_fact_keeps_original_strict_support_validation(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            result = validate_structured_grounded_report(
                candidate("evidence_fact", "ABC株式会社2025年营业收入为200JPY。", [card.evidence_code]),
                build_evidence_packet(db, report.id),
            ).result
            self.assertFalse(result.valid)
            self.assertIn("UNSUPPORTED_NUMBER", {item["code"] for item in result.errors})

    def test_supported_bounded_analysis_can_explain_risk_without_lexical_copy(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(
                db, quote="项目收入主要来自供需调节市场。",
            )
            result = validate_structured_grounded_report(
                candidate(
                    "bounded_analysis",
                    "收入来源集中可能使现金流对调节市场价格变化较为敏感。",
                    [card.evidence_code],
                ),
                build_evidence_packet(db, report.id),
            ).result
            self.assertTrue(result.valid, result.errors)

    def test_new_number_or_derived_financial_result_is_rejected(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            result = validate_structured_grounded_report(
                candidate("bounded_analysis", "利润可能下降20%。", [card.evidence_code]),
                build_evidence_packet(db, report.id),
            ).result
            codes = {item["code"] for item in result.errors}
            self.assertIn("UNSUPPORTED_NUMBER", codes)
            self.assertIn("BOUNDED_ANALYSIS_NEW_DERIVED_RESULT", codes)

    def test_new_entity_project_and_event_are_rejected(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            result = validate_structured_grounded_report(
                candidate("bounded_analysis", "新星公司可能收购乙项目。", [card.evidence_code]),
                build_evidence_packet(db, report.id),
            ).result
            codes = {item["code"] for item in result.errors}
            self.assertIn("BOUNDED_ANALYSIS_NEW_ENTITY", codes)
            self.assertIn("BOUNDED_ANALYSIS_NEW_EVENT", codes)

    def test_forecast_cannot_be_rewritten_as_completed_event(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(
                db, quote="ABC株式会社计划于2025年投产。",
            )
            card.claim_type = "forecast"
            db.commit()
            result = validate_structured_grounded_report(
                candidate(
                    "bounded_analysis", "ABC株式会社已经投产，可能改善经营。",
                    [card.evidence_code],
                ),
                build_evidence_packet(db, report.id),
            ).result
            self.assertIn("FORECAST_AS_FACT", {item["code"] for item in result.errors})

    def test_certainty_language_is_rejected(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            result = validate_structured_grounded_report(
                candidate(
                    "bounded_analysis", "该收入结构意味着利润一定改善。",
                    [card.evidence_code],
                ),
                build_evidence_packet(db, report.id),
            ).result
            self.assertIn(
                "BOUNDED_ANALYSIS_CERTAINTY_FORBIDDEN",
                {item["code"] for item in result.errors},
            )


if __name__ == "__main__":
    unittest.main()
