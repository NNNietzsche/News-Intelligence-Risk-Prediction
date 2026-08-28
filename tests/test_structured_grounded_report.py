from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.api import routes
from app.database.models import IndustryReport
from app.exporters.docx_report import build_industry_report_docx
from app.services.deepseek_analyzer import (
    GROUNDED_REPORT_PROMPT_VERSION,
    STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION,
)
from app.services.grounded_report import GroundedPromotionError, GroundedReportService
from app.services.industry_analysis import report_json_to_html
from app.services.structured_report_compiler import compile_structured_report
from tests.formal_grounded_helpers import make_ready_report
from tests.helpers import isolated_session


def structured_candidate(code="E000001"):
    return {
        "title": "V2结构化报告",
        "structured_sections": [{
            "heading": "商业模式与盈利能力",
            "sentences": [
                {
                    "sentence_type": "evidence_fact",
                    "text": "ABC株式会社2025年营业收入为100JPY。",
                    "evidence_codes": [code],
                },
                {
                    "sentence_type": "bounded_analysis",
                    "text": "收入集中可能使现金流对相关价格变化较为敏感。",
                    "evidence_codes": [code, code],
                    "assumptions": ["收入结构保持不变"],
                },
            ],
        }],
        "key_metrics": [], "limitations": [], "unresolved_conflicts": [],
    }


class StructuredFakeAnalyzer:
    model = "fake-structured"

    def __init__(self, generated):
        self.generated = generated
        self.repair_calls = 0
        self.v1_calls = 0

    def generate_structured_grounded_report(self, *_args):
        return self.generated

    def repair_structured_grounded_report(self, *_args):
        self.repair_calls += 1
        return self.generated

    def generate_grounded_report(self, *_args):
        self.v1_calls += 1
        raise AssertionError("V2 must not fall back to V1")


class StructuredGroundedReportTests(unittest.TestCase):
    def test_compiler_adds_inline_references_and_unique_citations(self):
        compiled = compile_structured_report(structured_candidate()).report
        content = compiled["sections"][0]["content"]
        self.assertEqual(content.count("[E000001]"), 2)
        self.assertIn("100JPY[E000001]。", content)
        self.assertEqual(compiled["citations"], [{
            "evidence_code": "E000001", "location": "sections[0].content",
        }])
        self.assertNotIn("收入结构保持不变", content)

    def test_compiled_report_remains_readable_by_web_and_word_consumers(self):
        compiled = compile_structured_report(structured_candidate()).report
        html = report_json_to_html(compiled)
        self.assertIn("商业模式与盈利能力", html)
        report = IndustryReport(
            industry_name="A", status="completed", generation_mode="legacy", version=1,
            report_json=json.dumps(compiled, ensure_ascii=False),
        )
        word_text = "\n".join(
            paragraph.text for paragraph in build_industry_report_docx(report).paragraphs
        )
        self.assertIn("收入集中可能", word_text)

    def test_v2_reuses_shadow_run_and_cannot_be_promoted(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            fake = StructuredFakeAnalyzer(structured_candidate(card.evidence_code))
            run = GroundedReportService(db, fake).generate(
                report.id, prompt_version=STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION,
            )
            self.assertEqual((run.status, run.prompt_version), (
                "validated", STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION,
            ))
            stored = json.loads(run.candidate_report_json)
            self.assertIn("structured_sections", stored)
            with self.assertRaises(GroundedPromotionError) as caught:
                GroundedReportService(db).promote(
                    report.id, run.id, promotion_type="manual", promotion_note="approved",
                )
            self.assertEqual(caught.exception.code, "RUN_SNAPSHOT_STALE")

    def test_v1_prompt_version_constant_and_default_remain_unchanged(self):
        self.assertEqual(GROUNDED_REPORT_PROMPT_VERSION, "grounded-report-v1")
        self.assertEqual(
            STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION,
            "grounded-report-v2-structured",
        )

    def test_existing_shadow_endpoint_selects_v2_by_prompt_version(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            fake = StructuredFakeAnalyzer(structured_candidate(card.evidence_code))
            with patch.object(
                routes, "GroundedReportService",
                side_effect=lambda session: GroundedReportService(session, fake),
            ):
                payload = routes.generate_grounded_report_shadow(
                    report.id, db,
                    prompt_version=STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION,
                )
            self.assertEqual(payload["prompt_version"], STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION)

    def test_v2_validation_failure_does_not_fall_back_to_v1_or_legacy(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            invalid = structured_candidate(card.evidence_code)
            invalid["structured_sections"][0]["sentences"][1]["text"] = (
                "该结构意味着利润一定改善。"
            )
            fake = StructuredFakeAnalyzer(invalid)
            run = GroundedReportService(db, fake).generate(
                report.id, prompt_version=STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION,
            )
            self.assertEqual(run.status, "failed")
            self.assertEqual((fake.repair_calls, fake.v1_calls), (1, 0))


if __name__ == "__main__":
    unittest.main()
