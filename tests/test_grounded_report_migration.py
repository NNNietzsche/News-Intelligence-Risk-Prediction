from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.database.models import Base
from app.database.session import _ensure_sqlite_indexes, _migrate_sqlite_columns


class GroundedReportMigrationTests(unittest.TestCase):
    def test_fresh_database_has_grounded_run_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'fresh.db'}")
            try:
                Base.metadata.create_all(engine)
                _ensure_sqlite_indexes(engine)
                self.assertIn("industry_grounded_report_runs", inspect(engine).get_table_names())
                columns = {item["name"] for item in inspect(engine).get_columns("industry_grounded_report_runs")}
                self.assertIn("candidate_report_json", columns)
                self.assertIn("validation_errors_json", columns)
            finally:
                engine.dispose()

    def test_old_database_upgrade_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'old.db'}")
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        "CREATE TABLE industry_reports (id INTEGER PRIMARY KEY, industry_name TEXT NOT NULL, status TEXT)"
                    )
                    connection.exec_driver_sql(
                        "CREATE TABLE industry_data_sources (id INTEGER PRIMARY KEY, name TEXT, source_type TEXT, extracted_text TEXT)"
                    )
                    connection.exec_driver_sql("INSERT INTO industry_reports VALUES (1, '旧报告', 'completed')")
                Base.metadata.create_all(engine)
                _migrate_sqlite_columns(engine)
                Base.metadata.create_all(engine)
                _ensure_sqlite_indexes(engine)
                _migrate_sqlite_columns(engine)
                _ensure_sqlite_indexes(engine)
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.exec_driver_sql("SELECT industry_name FROM industry_reports WHERE id=1").scalar_one(),
                        "旧报告",
                    )
                self.assertIn("industry_grounded_report_runs", inspect(engine).get_table_names())
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
