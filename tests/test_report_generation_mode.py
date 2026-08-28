from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.config import Settings
from app.database.models import IndustryDataSource, IndustryReport
from app.services.industry_analysis import IndustryAnalysisService, IndustryGenerationError
from tests.formal_grounded_helpers import (
    FormalFakeAnalyzer, grounded_candidate, grounded_settings, legacy_settings, make_ready_report,
)
from tests.helpers import isolated_session


class ReportGenerationModeTests(unittest.TestCase):
    def test_default_mode_is_legacy_and_records_audit(self):
        self.assertEqual(Settings(_env_file=None).industry_report_generation_mode, "legacy")
        with isolated_session() as db:
            report = IndustryReport(
                industry_name="测试", status="draft", supplement_search=False,
            )
            db.add(report)
            db.flush()
            db.add(IndustryDataSource(
                report_id=report.id, name="source", source_type="file",
                extracted_text="legacy input", char_count=12,
            ))
            db.commit()
            analyzer = FormalFakeAnalyzer()
            result = IndustryAnalysisService(
                db, deepseek=analyzer, settings=legacy_settings(),
            ).generate_report(report.id)
            self.assertEqual((result.status, result.generation_mode), ("completed", "legacy"))
            self.assertEqual(result.citation_validation_status, "not_applicable")
            self.assertEqual((analyzer.legacy_calls, analyzer.generate_calls), (1, 0))

    def test_grounded_mode_uses_packet_and_waits_for_approval(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            analyzer = FormalFakeAnalyzer(generated=grounded_candidate(card))
            result = IndustryAnalysisService(
                db, deepseek=analyzer, settings=grounded_settings(approval=True),
            ).generate_report(report.id)
            self.assertEqual(result.status, "awaiting_approval")
            self.assertEqual(result.generation_mode, "grounded")
            self.assertIsNone(result.report_json)
            self.assertEqual((analyzer.generate_calls, analyzer.legacy_calls), (1, 0))

    def test_grounded_validation_failure_never_falls_back_or_overwrites(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            report.report_json = '{"previous":true}'
            report.report_html = "previous html"
            db.commit()
            invalid = grounded_candidate(card, cited=False)
            analyzer = FormalFakeAnalyzer(generated=invalid, repaired=invalid)
            with self.assertRaises(IndustryGenerationError) as caught:
                IndustryAnalysisService(
                    db, deepseek=analyzer, settings=grounded_settings(approval=False),
                ).generate_report(report.id)
            db.refresh(report)
            self.assertEqual(caught.exception.code, "GROUNDING_VALIDATION_FAILED")
            self.assertEqual((analyzer.generate_calls, analyzer.repair_calls, analyzer.legacy_calls), (1, 1, 0))
            self.assertEqual((report.report_json, report.report_html), ('{"previous":true}', "previous html"))

    def test_no_verified_evidence_never_calls_report_model(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            card.validation_status = "rejected"
            db.commit()
            analyzer = FormalFakeAnalyzer()
            with self.assertRaises(IndustryGenerationError):
                IndustryAnalysisService(
                    db, deepseek=analyzer, settings=grounded_settings(),
                ).generate_report(report.id)
            self.assertEqual((analyzer.generate_calls, analyzer.legacy_calls), (0, 0))

    def test_explicit_server_switch_can_replace_waiting_candidate_with_legacy(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            report.supplement_search = False
            db.commit()
            grounded_analyzer = FormalFakeAnalyzer(generated=grounded_candidate(card))
            waiting = IndustryAnalysisService(
                db, deepseek=grounded_analyzer, settings=grounded_settings(approval=True),
            ).generate_report(report.id)
            self.assertEqual(waiting.status, "awaiting_approval")

            legacy_analyzer = FormalFakeAnalyzer()
            completed = IndustryAnalysisService(
                db, deepseek=legacy_analyzer, settings=legacy_settings(),
            ).generate_report(report.id)
            self.assertEqual((completed.status, completed.generation_mode), ("completed", "legacy"))
            self.assertEqual(completed.citation_validation_status, "not_applicable")
            self.assertEqual(legacy_analyzer.legacy_calls, 1)

    def test_invalid_mode_and_enabled_fallback_are_configuration_errors(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, industry_report_generation_mode="unknown")
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, grounded_report_allow_legacy_fallback=True)


if __name__ == "__main__":
    unittest.main()
