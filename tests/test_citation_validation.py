from __future__ import annotations

import unittest

from app.services.citation_validation import validate_citations


def evidence(
    code="E000001", quote="公司2024年度收入为320亿日元。", claim="公司2024年度收入为320亿日元",
    raw="320", normalized="32000000000", unit=None, currency="JPY", period="2024年度",
    claim_type="fact", speaker=None,
):
    return {
        "evidence_code": code, "normalized_claim": claim, "original_quote": quote,
        "claim_type": claim_type, "subject": "公司", "metric_name": "营业收入",
        "raw_value": raw, "normalized_value": normalized, "unit": unit,
        "currency": currency, "period": period, "as_of_date": None, "speaker": speaker,
        "risk_tags": ["revenue_model"], "importance_score": 5,
        "source_origin": "customer_file", "evidence_grade": "full_text",
        "source_name": "年报", "locator": "PDF第1页", "chunk_content_hash": "h",
        "usage_policy": "usable",
    }


def packet(items=None, unresolved=None, resolved=None):
    return {
        "report_id": 1, "evidence": items or [evidence()],
        "unresolved_conflicts": unresolved or [], "resolved_conflicts": resolved or [],
        "limitations": [], "missing_information": [], "coverage": {},
    }


def report(sentence, *, location="summary", citations=True):
    payload = {
        "title": "报告", "sections": [], "summary": sentence if location == "summary" else "",
        "risk_outlook": sentence if location == "risk_outlook" else "", "key_metrics": [],
        "citations": ([{"evidence_code": "E000001", "location": location}] if citations else []),
        "limitations": [], "unresolved_conflicts": [], "evidence_coverage": {},
        "generation_metadata": {},
    }
    return payload


def error_codes(result):
    return {item["code"] for item in result.errors}


class CitationValidationTests(unittest.TestCase):
    def test_valid_sentence_level_citation(self):
        result = validate_citations(report("公司2024年度收入为320亿日元[E000001]。"), packet())
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.coverage["citation_count"], 1)

    def test_missing_or_foreign_evidence_code_fails(self):
        missing = report("公司收入为320亿日元[E999999]。")
        missing["citations"] = [{"evidence_code": "E999999", "location": "summary"}]
        result = validate_citations(missing, packet())
        self.assertIn("INVALID_EVIDENCE_CODE", error_codes(result))

    def test_numeric_fact_without_same_sentence_citation_fails(self):
        result = validate_citations(
            report("公司2024年度收入为320亿日元。证据见后文[E000001]。", citations=True), packet()
        )
        self.assertIn("UNCITED_FACT", error_codes(result))

    def test_number_currency_unit_and_period_rewrites_fail(self):
        cases = [
            ("公司2024年度收入为321亿日元[E000001]。", "UNSUPPORTED_NUMBER", evidence()),
            ("公司2024年度收入为320亿人民币[E000001]。", "UNSUPPORTED_CURRENCY", evidence()),
            ("公司增长率为8.2倍[E000001]。", "UNSUPPORTED_UNIT", evidence(quote="公司增长率为8.2%。", claim="公司增长率为8.2%", raw="8.2", normalized="0.082", unit="%", currency=None, period="2024年度")),
            ("公司2024年度产能为100MWh[E000001]。", "UNSUPPORTED_UNIT", evidence(quote="公司2024年度产能为100MW。", claim="公司2024年度产能为100MW", raw="100", normalized="100", unit="MW", currency=None)),
            ("公司2025年度收入为320亿日元[E000001]。", "UNSUPPORTED_PERIOD", evidence()),
        ]
        for sentence, expected, item in cases:
            with self.subTest(expected=expected):
                result = validate_citations(report(sentence), packet([item]))
                self.assertIn(expected, error_codes(result))

    def test_forecast_and_opinion_attribution_are_enforced(self):
        forecast = evidence(
            quote="公司预计2025年产量为100MW。", claim="公司预计2025年产量为100MW",
            raw="100", normalized="100", unit="MW", currency=None, period="2025年",
            claim_type="forecast",
        )
        result = validate_citations(report("公司2025年产量为100MW[E000001]。"), packet([forecast]))
        self.assertIn("FORECAST_AS_FACT", error_codes(result))
        opinion = evidence(
            quote="管理层认为需求将增长。", claim="管理层认为需求将增长",
            raw=None, normalized=None, currency=None, period=None,
            claim_type="reported_opinion", speaker="管理层",
        )
        result = validate_citations(report("需求将增长[E000001]。"), packet([opinion]))
        self.assertIn("OPINION_ATTRIBUTION_MISSING", error_codes(result))

    def test_key_metrics_require_valid_evidence_code(self):
        candidate = report("")
        candidate["key_metrics"] = [{"name": "收入", "value": "320亿日元"}]
        result = validate_citations(candidate, packet())
        self.assertIn("KEY_METRIC_CITATION_MISSING", error_codes(result))

    def test_inline_and_citations_list_must_match_location(self):
        candidate = report("公司2024年度收入为320亿日元[E000001]。")
        candidate["citations"] = [{"evidence_code": "E000001", "location": "sections[0].content"}]
        result = validate_citations(candidate, packet())
        self.assertIn("CITATION_LIST_MISMATCH", error_codes(result))

    def test_unresolved_conflict_cannot_be_silently_selected(self):
        second = evidence("E000002", "公司2024年度收入为420亿日元。", "公司2024年度收入为420亿日元", "420", "42000000000")
        conflict = {
            "conflict_code": "C000001", "severity": "critical", "description": "收入冲突",
            "member_evidence_codes": ["E000001", "E000002"], "resolution_status": "open",
        }
        result = validate_citations(report("公司2024年度收入为320亿日元[E000001]。"), packet([evidence(), second], [conflict]))
        codes = error_codes(result)
        self.assertIn("UNRESOLVED_CONFLICT_ASSERTED", codes)
        self.assertIn("MATERIAL_CONFLICT_NOT_DISCLOSED", codes)

    def test_fabricated_event_fails_even_with_valid_code(self):
        result = validate_citations(report("公司已发生债务违约[E000001]。"), packet())
        self.assertIn("UNSUPPORTED_EVENT", error_codes(result))

    def test_company_name_cannot_be_rewritten(self):
        item = evidence(
            quote="甲公司2024年度收入为320亿日元。", claim="甲公司2024年度收入为320亿日元"
        )
        result = validate_citations(report("乙公司2024年度收入为320亿日元[E000001]。"), packet([item]))
        self.assertIn("UNSUPPORTED_ENTITY", error_codes(result))

    def test_partial_text_limitation_must_be_disclosed(self):
        item = evidence()
        item["evidence_grade"] = "partial_text"
        result = validate_citations(report("公司2024年度收入为320亿日元[E000001]。"), packet([item]))
        self.assertIn("SOURCE_LIMITATION_NOT_DISCLOSED", error_codes(result))

    def test_money_magnitude_cannot_be_rewritten(self):
        result = validate_citations(
            report("公司2024年度收入为320万日元[E000001]。"), packet(),
        )
        self.assertIn("UNSUPPORTED_UNIT", error_codes(result))

    def test_negative_event_status_cannot_be_reversed(self):
        item = evidence(
            quote="公司未发生债务违约。", claim="公司未发生债务违约",
            raw=None, normalized=None, currency=None, period=None,
        )
        result = validate_citations(report("公司发生债务违约[E000001]。"), packet([item]))
        self.assertIn("UNSUPPORTED_EVENT", error_codes(result))

    def test_non_numeric_body_fact_still_requires_citation(self):
        result = validate_citations(report("公司总部位于东京。", citations=False), packet())
        self.assertIn("UNCITED_FACT", error_codes(result))

    def test_unused_partial_evidence_does_not_force_limitation(self):
        item = evidence()
        item["evidence_grade"] = "partial_text"
        result = validate_citations(report("", citations=False), packet([item]))
        self.assertNotIn("SOURCE_LIMITATION_NOT_DISCLOSED", error_codes(result))


if __name__ == "__main__":
    unittest.main()
