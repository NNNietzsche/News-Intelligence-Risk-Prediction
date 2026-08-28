from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import router
from app.database.models import Base, IndustryDataSource, IndustryReport
from app.database.session import _ensure_sqlite_indexes, _migrate_sqlite_columns
from app.services.data_bridge import migrate_legacy_data
from app.services.industry_migration import migrate_main_db_industry_reports


class DatabaseMigrationTests(unittest.TestCase):
    def test_fresh_database_initializes_from_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'fresh.db'}")
            try:
                Base.metadata.create_all(engine)
                _migrate_sqlite_columns(engine)
                _ensure_sqlite_indexes(engine)
                names = set(inspect(engine).get_table_names())
                self.assertIn("industry_data_sources", names)
                self.assertIn("industry_source_chunks", names)
                self.assertIn("industry_evidence_extraction_runs", names)
                self.assertIn("industry_evidence_cards", names)
            finally:
                engine.dispose()

    def test_old_database_upgrade_is_idempotent_and_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'old.db'}")
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "CREATE TABLE industry_reports ("
                        "id INTEGER PRIMARY KEY, industry_name VARCHAR(256) NOT NULL, "
                        "company_name VARCHAR(256), status VARCHAR(32), report_html TEXT, "
                        "report_json TEXT, chart_specs TEXT, error_message TEXT, "
                        "legacy_report_id INTEGER, created_at DATETIME, updated_at DATETIME)"
                    )
                    conn.exec_driver_sql(
                        "CREATE TABLE industry_data_sources ("
                        "id INTEGER PRIMARY KEY, industry_name VARCHAR(256), name VARCHAR(256), "
                        "source_type VARCHAR(32), file_path VARCHAR(1024), "
                        "original_filename VARCHAR(512), url VARCHAR(1024), extracted_text TEXT, "
                        "is_active BOOLEAN, created_at DATETIME)"
                    )
                    conn.exec_driver_sql(
                        "INSERT INTO industry_reports "
                        "(id, industry_name, status, report_json) VALUES (1, '旧行业', 'completed', '{}')"
                    )
                    conn.exec_driver_sql(
                        "INSERT INTO industry_data_sources "
                        "(id, industry_name, name, source_type, extracted_text) "
                        "VALUES (1, '旧行业', '旧来源', 'file', '旧正文')"
                    )
                Base.metadata.create_all(engine)
                _migrate_sqlite_columns(engine)
                Base.metadata.create_all(engine)
                _ensure_sqlite_indexes(engine)
                _migrate_sqlite_columns(engine)
                _ensure_sqlite_indexes(engine)
                with engine.connect() as conn:
                    report = conn.exec_driver_sql(
                        "SELECT industry_name, report_json FROM industry_reports WHERE id=1"
                    ).one()
                    source = conn.exec_driver_sql(
                        "SELECT name, extracted_text, report_id, parse_status "
                        "FROM industry_data_sources WHERE id=1"
                    ).one()
                self.assertEqual(report, ("旧行业", "{}"))
                self.assertEqual(source[0:2], ("旧来源", "旧正文"))
                self.assertIsNone(source.report_id)
                self.assertIsNone(source.parse_status)
                with Session(engine) as session:
                    orm_report = session.get(IndustryReport, 1)
                    orm_source = session.get(IndustryDataSource, 1)
                    self.assertEqual(orm_report.industry_name, "旧行业")
                    self.assertEqual(orm_source.extracted_text, "旧正文")
            finally:
                engine.dispose()

    def test_existing_upload_and_generation_paths_are_unchanged(self) -> None:
        paths = {route.path for route in router.routes}
        self.assertIn(
            "/industry/reports/{report_id}/data-sources/upload", paths
        )
        self.assertIn("/industry/reports/{report_id}/data-sources/url", paths)
        self.assertIn("/industry/reports/{report_id}/generate", paths)
        self.assertIn("/industry/reports/{report_id}/evidence/extract", paths)

    def test_startup_migrations_release_main_database_before_native_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_path = root / "main.db"
            sector_path = root / "aviation.db"
            main_engine = create_engine(f"sqlite:///{main_path}")
            sector_engine = create_engine(f"sqlite:///{sector_path}")
            Base.metadata.create_all(main_engine)
            Base.metadata.create_all(sector_engine)
            db = sessionmaker(bind=main_engine)()
            try:
                migrate_legacy_data(db)
                self.assertFalse(db.in_transaction())

                report = IndustryReport(industry_name="航空运输", status="draft")
                db.add(report)
                db.commit()
                report_id = report.id

                # Reproduce a startup caller that already holds a read transaction.
                db.query(IndustryReport).all()
                self.assertTrue(db.in_transaction())

                with (
                    patch(
                        "app.services.industry_migration._main_db_path",
                        return_value=main_path,
                    ),
                    patch(
                        "app.services.industry_migration.industry_db_path",
                        return_value=sector_path,
                    ),
                    patch(
                        "app.services.industry_migration.init_all_industry_databases"
                    ),
                    patch("app.services.industry_migration._move_upload_dir"),
                ):
                    moved = migrate_main_db_industry_reports(db)

                self.assertEqual(moved["aviation"], 1)
                with main_engine.connect() as conn:
                    main_count = conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM industry_reports WHERE id = ?",
                        (report_id,),
                    ).scalar_one()
                with sector_engine.connect() as conn:
                    sector_count = conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM industry_reports WHERE id = ?",
                        (report_id,),
                    ).scalar_one()
                self.assertEqual(main_count, 0)
                self.assertEqual(sector_count, 1)
            finally:
                db.close()
                main_engine.dispose()
                sector_engine.dispose()


if __name__ == "__main__":
    unittest.main()
