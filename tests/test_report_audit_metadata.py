from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.database.models import Base, IndustryReport
from app.database.session import _migrate_sqlite_columns
from app.exporters.docx_report import build_industry_report_docx
from app.services.industry_analysis import IndustryAnalysisService
from tests.formal_grounded_helpers import (
    FormalFakeAnalyzer, grounded_candidate, grounded_settings, legacy_settings, make_ready_report,
)
from tests.helpers import isolated_session


class ReportAuditMetadataTests(unittest.TestCase):
    def test_grounded_formal_report_has_safe_audit_metadata(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            analyzer = FormalFakeAnalyzer(generated=grounded_candidate(card))
            result = IndustryAnalysisService(
                db, deepseek=analyzer, settings=grounded_settings(approval=False),
            ).generate_report(report.id)
            metadata = json.loads(result.grounded_generation_metadata)
            payload = json.loads(result.report_json)
            self.assertEqual(metadata["generation_mode"], "grounded")
            self.assertEqual(metadata["citation_validation_status"], "validated")
            self.assertNotIn("system_prompt", str(metadata).lower())
            self.assertEqual(payload["generation_metadata"], metadata)
            self.assertEqual(payload["evidence_coverage"]["verified_evidence_count"], 1)
            self.assertNotIn("original_quote", result.source_manifest_json)

    def test_legacy_report_can_never_be_marked_citation_validated(self):
        with isolated_session() as db:
            from app.database.models import IndustryDataSource
            report = IndustryReport(industry_name="legacy", status="draft", supplement_search=False)
            db.add(report)
            db.flush()
            db.add(IndustryDataSource(
                report_id=report.id, name="source", source_type="file",
                extracted_text="legacy text", char_count=11,
            ))
            db.commit()
            result = IndustryAnalysisService(
                db, deepseek=FormalFakeAnalyzer(), settings=legacy_settings(),
            ).generate_report(report.id)
            self.assertEqual(result.citation_validation_status, "not_applicable")
            self.assertIsNone(result.grounded_run_id)

    def test_word_body_preserves_inline_evidence_codes(self):
        report = IndustryReport(
            id=99, industry_name="test", status="completed",
            report_json=json.dumps({
                "summary": "事实[E000001]。", "sections": [], "risk_outlook": "",
            }, ensure_ascii=False),
        )
        document = build_industry_report_docx(report)
        self.assertIn("[E000001]", "\n".join(paragraph.text for paragraph in document.paragraphs))

    def test_old_database_gets_audit_columns_without_losing_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "old.db"
            engine = create_engine(f"sqlite:///{path}")
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE industry_reports (id INTEGER PRIMARY KEY, industry_name VARCHAR(256), "
                    "status VARCHAR(32), report_json TEXT)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO industry_reports(id, industry_name, status, report_json) "
                    "VALUES (1, 'old', 'completed', '{\"safe\":true}')"
                )
            _migrate_sqlite_columns(engine)
            _migrate_sqlite_columns(engine)
            columns = {item["name"] for item in inspect(engine).get_columns("industry_reports")}
            self.assertIn("generation_mode", columns)
            self.assertIn("grounded_run_id", columns)
            with engine.connect() as conn:
                self.assertEqual(
                    conn.exec_driver_sql("SELECT report_json FROM industry_reports WHERE id=1").scalar(),
                    '{"safe":true}',
                )
            engine.dispose()

    def test_fresh_database_contains_audit_columns(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        columns = {item["name"] for item in inspect(engine).get_columns("industry_reports")}
        self.assertIn("citation_validation_status", columns)
        self.assertIn("grounded_generation_metadata", columns)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
