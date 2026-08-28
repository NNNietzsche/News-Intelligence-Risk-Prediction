from __future__ import annotations

import json
import unittest

from app.services.deepseek_analyzer import GroundedReportOutputError
from app.services.grounded_report import GroundedReportService
from tests.helpers import isolated_session
from tests.test_conflict_detection import add_card, setup_report


class FakeGroundedAnalyzer:
    model = "fake-grounded"

    def __init__(self, generated=None, repaired=None, generate_error=None, repair_error=None):
        self.generated = generated
        self.repaired = repaired
        self.generate_error = generate_error
        self.repair_error = repair_error
        self.generate_calls = 0
        self.repair_calls = 0

    def generate_grounded_report(self, *_args):
        self.generate_calls += 1
        if self.generate_error:
            raise self.generate_error
        return self.generated

    def repair_grounded_report(self, *_args):
        self.repair_calls += 1
        if self.repair_error:
            raise self.repair_error
        return self.repaired


def candidate_for(card, *, cited=True):
    sentence = card.original_quote.rstrip("。")
    if cited:
        sentence += f"[{card.evidence_code}]"
    sentence += "。"
    return {
        "title": "影子报告", "sections": [], "summary": sentence, "risk_outlook": "",
        "key_metrics": [],
        "citations": ([{"evidence_code": card.evidence_code, "location": "summary"}] if cited else []),
        "limitations": [], "unresolved_conflicts": [], "evidence_coverage": {},
        "generation_metadata": {"mode": "shadow"},
    }


class GroundedReportFlowTests(unittest.TestCase):
    def test_no_eligible_evidence_fails_without_model_call(self):
        with isolated_session() as db:
            report, _ = setup_report(db)
            report.report_json = '{"formal":true}'
            db.commit()
            analyzer = FakeGroundedAnalyzer()
            run = GroundedReportService(db, analyzer).generate(report.id)
            self.assertEqual((run.status, run.failure_code), ("failed", "insufficient_evidence"))
            self.assertEqual(analyzer.generate_calls, 0)
            self.assertEqual(report.report_json, '{"formal":true}')

    def test_valid_candidate_is_stored_in_shadow_only(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            report.report_json = '{"formal":true}'
            db.commit()
            _, _, card = add_card(db, report, extraction_run, "E000001", "100")
            analyzer = FakeGroundedAnalyzer(generated=candidate_for(card))
            service = GroundedReportService(db, analyzer)
            run = service.generate(report.id)
            db.refresh(report)
            self.assertEqual(run.status, "validated")
            self.assertEqual(run.repair_count, 0)
            self.assertEqual(report.report_json, '{"formal":true}')
            self.assertEqual(service.generate(report.id).id, run.id)
            self.assertEqual(analyzer.generate_calls, 1)

    def test_first_validation_failure_gets_one_targeted_repair(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            _, _, card = add_card(db, report, extraction_run, "E000001", "100")
            analyzer = FakeGroundedAnalyzer(
                generated=candidate_for(card, cited=False), repaired=candidate_for(card)
            )
            run = GroundedReportService(db, analyzer).generate(report.id)
            self.assertEqual((run.status, run.repair_count), ("validated", 1))
            self.assertEqual((analyzer.generate_calls, analyzer.repair_calls), (1, 1))
            validation = json.loads(run.validation_errors_json)
            self.assertTrue(validation["repair_history"][0]["errors"])

    def test_second_validation_failure_marks_failed_and_keeps_formal_report(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            report.report_json = '{"formal":"unchanged"}'
            db.commit()
            _, _, card = add_card(db, report, extraction_run, "E000001", "100")
            invalid = candidate_for(card, cited=False)
            analyzer = FakeGroundedAnalyzer(generated=invalid, repaired=invalid)
            run = GroundedReportService(db, analyzer).generate(report.id)
            db.refresh(report)
            self.assertEqual((run.status, run.failure_code, run.repair_count), ("failed", "validation_failed", 1))
            self.assertEqual(analyzer.repair_calls, 1)
            self.assertEqual(report.report_json, '{"formal":"unchanged"}')

    def test_schema_failure_also_uses_same_single_repair_budget(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            _, _, card = add_card(db, report, extraction_run, "E000001", "100")
            analyzer = FakeGroundedAnalyzer(
                generate_error=GroundedReportOutputError("not-json", "schema invalid"),
                repaired=candidate_for(card),
            )
            run = GroundedReportService(db, analyzer).generate(report.id)
            self.assertEqual((run.status, run.repair_count, analyzer.repair_calls), ("validated", 1, 1))

    def test_service_revalidates_fake_analyzer_schema(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            _, _, card = add_card(db, report, extraction_run, "E000001", "100")
            invalid = candidate_for(card) | {"unexpected": True}
            analyzer = FakeGroundedAnalyzer(generated=invalid, repaired=candidate_for(card))
            run = GroundedReportService(db, analyzer).generate(report.id)
            self.assertEqual((run.status, run.repair_count), ("validated", 1))

    def test_evidence_snapshot_change_creates_new_run(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            _, _, first = add_card(db, report, extraction_run, "E000001", "100")
            analyzer = FakeGroundedAnalyzer(generated=candidate_for(first))
            service = GroundedReportService(db, analyzer)
            old = service.generate(report.id)
            add_card(db, report, extraction_run, "E000002", "30", metric="净利润")
            analyzer.generated = candidate_for(first)
            new = service.generate(report.id)
            self.assertNotEqual(old.id, new.id)

    def test_report_context_change_invalidates_shadow_run(self):
        with isolated_session() as db:
            report, extraction_run = setup_report(db)
            _, _, card = add_card(db, report, extraction_run, "E000001", "100")
            analyzer = FakeGroundedAnalyzer(generated=candidate_for(card))
            service = GroundedReportService(db, analyzer)
            first = service.generate(report.id)
            report.company_name = "新的企业名称"
            db.commit()
            second = service.generate(report.id)
            self.assertNotEqual(first.id, second.id)


if __name__ == "__main__":
    unittest.main()
