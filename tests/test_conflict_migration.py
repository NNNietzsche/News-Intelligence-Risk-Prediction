from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.database.models import Base
from app.database.session import _ensure_sqlite_indexes, _migrate_sqlite_columns


class ConflictMigrationTests(unittest.TestCase):
    def test_fresh_database_has_conflict_tables_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'fresh.db'}")
            try:
                Base.metadata.create_all(engine)
                _ensure_sqlite_indexes(engine)
                inspector = inspect(engine)
                names = set(inspector.get_table_names())
                self.assertIn("industry_conflict_detection_runs", names)
                self.assertIn("industry_evidence_conflicts", names)
                self.assertIn("industry_evidence_conflict_members", names)
                self.assertGreaterEqual(len(inspector.get_foreign_keys("industry_evidence_conflict_members")), 2)
            finally:
                engine.dispose()

    def test_old_database_upgrade_is_repeatable_and_preserves_report(self):
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
                    value = connection.exec_driver_sql(
                        "SELECT industry_name FROM industry_reports WHERE id=1"
                    ).scalar_one()
                self.assertEqual(value, "旧报告")
                self.assertIn("industry_evidence_conflicts", inspect(engine).get_table_names())
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
