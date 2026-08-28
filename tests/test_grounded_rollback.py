from __future__ import annotations

import unittest

from app.services.industry_analysis import IndustryAnalysisService, IndustryGenerationError
from tests.formal_grounded_helpers import (
    FormalFakeAnalyzer, grounded_candidate, grounded_settings, make_ready_report,
)
from tests.helpers import isolated_session


class _MutatingAnalyzer(FormalFakeAnalyzer):
    def __init__(self, db, source, generated):
        super().__init__(generated=generated)
        self.db = db
        self.source = source

    def generate_grounded_report(self, packet, *_args):
        result = super().generate_grounded_report(packet)
        self.source.extracted_text_hash = "b" * 64
        self.db.commit()
        return result


class GroundedRollbackTests(unittest.TestCase):
    def test_automatic_mode_promotes_only_validated_candidate(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            analyzer = FormalFakeAnalyzer(generated=grounded_candidate(card))
            result = IndustryAnalysisService(
                db, deepseek=analyzer, settings=grounded_settings(approval=False),
            ).generate_report(report.id)
            self.assertEqual((result.status, result.promotion_type), ("completed", "automatic"))
            self.assertIn("[E000001]", result.report_json)

    def test_snapshot_change_during_generation_does_not_overwrite_previous_formal_body(self):
        with isolated_session() as db:
            report, _, source, _, card = make_ready_report(db)
            report.report_json = '{"previous":"safe"}'
            report.report_html = "safe html"
            report.generation_mode = "legacy"
            report.prompt_version = "legacy-industry-v1"
            report.citation_validation_status = "not_applicable"
            db.commit()
            analyzer = _MutatingAnalyzer(db, source, grounded_candidate(card))
            with self.assertRaises(IndustryGenerationError) as caught:
                IndustryAnalysisService(
                    db, deepseek=analyzer, settings=grounded_settings(approval=False),
                ).generate_report(report.id)
            db.refresh(report)
            self.assertEqual(caught.exception.code, "RUN_SNAPSHOT_STALE")
            self.assertEqual(report.report_json, '{"previous":"safe"}')
            self.assertEqual(report.report_html, "safe html")
            self.assertEqual(report.generation_mode, "legacy")
            self.assertEqual(report.citation_validation_status, "not_applicable")
            self.assertEqual(report.status, "failed")


if __name__ == "__main__":
    unittest.main()
