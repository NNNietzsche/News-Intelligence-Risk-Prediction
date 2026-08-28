from __future__ import annotations

import unittest

from app.database.models import IndustryDataSource, IndustryEvidenceCard
from app.services.evidence_normalization import (
    normalize_evidence, normalize_metric, normalize_period, normalize_subject,
)


def card(value="1.2", unit="GW", currency=None, period="2025年", **kwargs):
    defaults = dict(
        id=1, evidence_code="E000001", report_id=1, source_id=1, chunk_id=1,
        extraction_run_id=1, dedupe_key="d", chunk_content_hash="h", locator="p1",
        original_quote="公司装机容量为1.2GW。", normalized_claim="公司装机容量为1.2GW",
        claim_type="fact", subject="株式会社 ABC", metric_name="装机容量",
        normalized_value=value, unit=unit, currency=currency, period=period,
        importance_score=4, risk_tags="[]", validation_status="verified",
        verification_scope="source_match", requires_manual_review=False,
        source_origin="customer_file", evidence_grade="full_text",
    )
    defaults.update(kwargs)
    return IndustryEvidenceCard(**defaults)


class EvidenceNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.source = IndustryDataSource(
            id=1, report_id=1, name="s", source_type="file", source_origin="customer_file",
            evidence_grade="full_text", is_truncated=False, used_ocr=False,
        )

    def test_power_and_energy_use_distinct_decimal_base_units(self):
        gw = normalize_evidence(card("1.2", "GW"), self.source)
        mw = normalize_evidence(card("1200", "MW"), self.source)
        gwh = normalize_evidence(card("0.1", "GWh", metric_name="产量"), self.source)
        mwh = normalize_evidence(card("100", "MWh", metric_name="产量"), self.source)
        self.assertEqual(gw.comparison_value, mw.comparison_value)
        self.assertEqual(gwh.comparison_value, mwh.comparison_value)
        self.assertNotEqual(gw.dimension_key, gwh.dimension_key)

    def test_money_and_percentage_dimensions_do_not_mix(self):
        money = normalize_evidence(card("32000000000", None, "JPY", metric_name="营业收入"), self.source)
        percent = normalize_evidence(card("0.082", "%", None, metric_name="增长率"), self.source)
        ratio = normalize_evidence(card("8.2", "倍", None, metric_name="增长率"), self.source)
        self.assertEqual((money.dimension_key, money.base_unit), ("money", "JPY"))
        self.assertEqual(percent.dimension_key, "percentage")
        self.assertEqual(ratio.dimension_key, "ratio")
        points = normalize_evidence(
            card("0.082", "%", None, metric_name="增长率", original_quote="增长率提高8.2个百分点"),
            self.source,
        )
        self.assertEqual(points.dimension_key, "percentage_point")

    def test_periods_are_conservative(self):
        self.assertEqual(normalize_period("2025年"), "CY2025")
        self.assertEqual(normalize_period("2025财年"), "FY2025")
        self.assertEqual(normalize_period("2025年度"), "ANNUAL2025")
        self.assertNotEqual(normalize_period("2025年"), normalize_period("2025财年"))

    def test_subject_and_metric_only_use_explicit_rules(self):
        self.assertEqual(normalize_subject("株式会社 ABC"), normalize_subject("ABC株式会社"))
        self.assertEqual(normalize_metric("营收"), "revenue")
        self.assertNotEqual(normalize_metric("营收"), normalize_metric("净利润"))
        self.assertTrue(normalize_metric("特定口径指标").startswith("raw:"))


if __name__ == "__main__":
    unittest.main()
