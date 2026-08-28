"""数据库引擎与会话。"""

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database.models import Base


def _ensure_sqlite_dir(database_url: str) -> None:
    if database_url.startswith("sqlite:///"):
        rel = database_url.replace("sqlite:///", "", 1)
        if rel != ":memory:" and not rel.startswith("file:"):
            path = Path(rel)
            if not path.is_absolute():
                path = Path.cwd() / path
            path.parent.mkdir(parents=True, exist_ok=True)


_settings = get_settings()
_ensure_sqlite_dir(_settings.database_url)

connect_args = {}
if _settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    _settings.database_url,
    connect_args=connect_args,
    echo=os.environ.get("SQL_ECHO", "").lower() in ("1", "true"),
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_connection, connection_record):
    if _settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # 较短超时，避免上传/侧栏接口在采集写库时卡住数十秒
        cursor.execute("PRAGMA busy_timeout=8000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def init_db() -> None:
    _ensure_sqlite_dir(_settings.database_url)
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_columns(engine)
    # New tables may reference columns added above. Re-running create_all is
    # harmless and makes initialization order explicit for old databases.
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_indexes(engine)


def _migrate_sqlite_columns(target_engine=engine) -> None:
    """为已有 SQLite 库补充新增列（create_all 不会 ALTER）。"""
    if target_engine.dialect.name != "sqlite":
        return
    alterations = [
        ("daily_risk_entries", "published_at", "DATETIME"),
        ("news_articles", "published_at", "DATETIME"),
        ("entity_risks", "source_name", "VARCHAR(256)"),
        ("entity_risks", "published_at", "DATETIME"),
        ("entity_risks", "provenance", "VARCHAR(16) NOT NULL DEFAULT 'real'"),
        ("entity_risks", "relevance", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
        ("entity_risks", "news_importance", "VARCHAR(16)"),
        ("entity_risks", "sentiment_direction", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
        ("entity_risks", "credit_impact", "VARCHAR(16) NOT NULL DEFAULT 'none'"),
        ("entity_risks", "confidence", "FLOAT"),
        ("entity_risks", "review_status", "VARCHAR(16) NOT NULL DEFAULT 'pending'"),
        ("entity_risks", "rule_version", "VARCHAR(32) NOT NULL DEFAULT 'entity-signal-v1'"),
        ("report_runs", "phase", "VARCHAR(32)"),
        ("report_runs", "funnel_json", "TEXT"),
        ("report_runs", "job_id", "VARCHAR(64)"),
        ("report_runs", "kept_previous", "BOOLEAN DEFAULT 0"),
        ("pipeline_jobs", "snapshot_json", "TEXT"),
        ("pipeline_jobs", "window_hours", "INTEGER DEFAULT 24"),
        ("module_data_sources", "entity_id", "INTEGER"),
        ("news_articles", "window_hours", "INTEGER DEFAULT 24"),
        ("daily_risk_entries", "window_hours", "INTEGER DEFAULT 24"),
        ("industry_reports", "parent_report_id", "INTEGER"),
        ("industry_reports", "root_report_id", "INTEGER"),
        ("industry_reports", "version", "INTEGER NOT NULL DEFAULT 1"),
        ("industry_reports", "report_name", "VARCHAR(256)"),
        ("industry_reports", "supplement_search", "BOOLEAN NOT NULL DEFAULT 1"),
        ("industry_reports", "library_saved", "BOOLEAN NOT NULL DEFAULT 0"),
        ("industry_reports", "is_source_library", "BOOLEAN NOT NULL DEFAULT 0"),
        ("industry_reports", "source_manifest_json", "TEXT"),
        ("industry_reports", "generation_config_json", "TEXT"),
        ("industry_reports", "generation_mode", "VARCHAR(32)"),
        ("industry_reports", "grounded_run_id", "INTEGER"),
        ("industry_reports", "prompt_version", "VARCHAR(64)"),
        ("industry_reports", "evidence_snapshot_hash", "VARCHAR(64)"),
        ("industry_reports", "conflict_snapshot_hash", "VARCHAR(64)"),
        ("industry_reports", "citation_validation_status", "VARCHAR(32)"),
        ("industry_reports", "promoted_at", "DATETIME"),
        ("industry_reports", "promotion_type", "VARCHAR(32)"),
        ("industry_reports", "promotion_note", "TEXT"),
        ("industry_reports", "grounded_generation_metadata", "TEXT"),
        # Non-destructive upgrade from both the original industry-name pool and
        # the newer report-scoped source table. Old rows deliberately keep a
        # NULL report_id and are not reprocessed during startup.
        ("industry_data_sources", "report_id", "INTEGER"),
        ("industry_data_sources", "copied_from_source_id", "INTEGER"),
        ("industry_data_sources", "is_selected", "BOOLEAN NOT NULL DEFAULT 1"),
        ("industry_data_sources", "content_hash", "VARCHAR(64)"),
        ("industry_data_sources", "char_count", "INTEGER NOT NULL DEFAULT 0"),
        ("industry_data_sources", "raw_content_hash", "VARCHAR(64)"),
        ("industry_data_sources", "extracted_text_hash", "VARCHAR(64)"),
        ("industry_data_sources", "mime_type", "VARCHAR(128)"),
        ("industry_data_sources", "file_size", "INTEGER"),
        ("industry_data_sources", "source_origin", "VARCHAR(32)"),
        ("industry_data_sources", "source_publisher", "VARCHAR(256)"),
        ("industry_data_sources", "published_at", "VARCHAR(128)"),
        ("industry_data_sources", "retrieved_at", "DATETIME"),
        ("industry_data_sources", "is_full_text", "BOOLEAN"),
        ("industry_data_sources", "is_truncated", "BOOLEAN"),
        ("industry_data_sources", "parse_status", "VARCHAR(32)"),
        ("industry_data_sources", "parse_warning", "TEXT"),
        ("industry_data_sources", "used_ocr", "BOOLEAN"),
        ("industry_data_sources", "page_count", "INTEGER"),
        ("industry_data_sources", "slide_count", "INTEGER"),
        ("industry_data_sources", "sheet_count", "INTEGER"),
        ("industry_data_sources", "evidence_grade", "VARCHAR(32)"),
    ]
    with target_engine.begin() as conn:
        for table, column, col_type in alterations:
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                continue
            existing = {r[1] for r in rows}
            if column not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
        for table in ("news_articles", "daily_risk_entries", "pipeline_jobs"):
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if "window_hours" in {r[1] for r in rows}:
                conn.exec_driver_sql(
                    f"UPDATE {table} SET window_hours = 24 WHERE window_hours IS NULL"
                )
        entity_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(entity_risks)").fetchall()
        }
        if {"provenance", "review_status", "credit_impact"}.issubset(entity_columns):
            # 旧库中的 example.com 样本只能是历史演示数据；保留记录但排除预警计算。
            conn.exec_driver_sql(
                "UPDATE entity_risks SET provenance='demo', review_status='rejected', "
                "credit_impact='none' WHERE lower(COALESCE(source_url, '')) LIKE '%example.com/%'"
            )
            conn.exec_driver_sql(
                "UPDATE entity_risks SET provenance='degraded', credit_impact='none' "
                "WHERE lower(COALESCE(structured_json, '')) LIKE '%\"_degraded\": true%' "
                "AND provenance <> 'demo'"
            )
        _migrate_report_runs_window_hours(conn)


def _migrate_report_runs_window_hours(conn) -> None:
    """为 report_runs 增加 window_hours，并重建唯一约束 (date, module, window)。"""
    rows = conn.exec_driver_sql("PRAGMA table_info(report_runs)").fetchall()
    if not rows:
        return
    colnames = {r[1] for r in rows}
    if "window_hours" in colnames:
        conn.exec_driver_sql(
            "UPDATE report_runs SET window_hours = 24 WHERE window_hours IS NULL"
        )
        return

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS report_runs_v2 (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            report_date DATE NOT NULL,
            module_code VARCHAR(8) NOT NULL,
            window_hours INTEGER NOT NULL DEFAULT 24,
            status VARCHAR(32) NOT NULL,
            started_at DATETIME,
            finished_at DATETIME,
            entry_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            phase VARCHAR(32),
            funnel_json TEXT,
            job_id VARCHAR(64),
            kept_previous BOOLEAN NOT NULL DEFAULT 0,
            CONSTRAINT uq_report_run_date_module_window
                UNIQUE (report_date, module_code, window_hours)
        )
        """
    )
    # 清空临时表，避免重复迁移半成品
    conn.exec_driver_sql("DELETE FROM report_runs_v2")
    select_cols = [
        "id",
        "report_date",
        "module_code",
        "status",
        "started_at",
        "finished_at",
        "entry_count",
        "notes",
    ]
    wh_expr = "24"
    insert_cols = (
        "id, report_date, module_code, window_hours, status, started_at, finished_at, "
        "entry_count, notes"
    )
    select_sql = (
        f"id, report_date, module_code, {wh_expr}, status, started_at, finished_at, "
        "entry_count, notes"
    )
    for col in ("phase", "funnel_json", "job_id"):
        if col in colnames:
            insert_cols += f", {col}"
            select_sql += f", {col}"
    if "kept_previous" in colnames:
        insert_cols += ", kept_previous"
        select_sql += ", COALESCE(kept_previous, 0)"
    else:
        insert_cols += ", kept_previous"
        select_sql += ", 0"
    conn.exec_driver_sql(
        f"INSERT INTO report_runs_v2 ({insert_cols}) SELECT {select_sql} FROM report_runs"
    )
    conn.exec_driver_sql("DROP TABLE report_runs")
    conn.exec_driver_sql("ALTER TABLE report_runs_v2 RENAME TO report_runs")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_report_runs_job_id ON report_runs (job_id)"
    )


def _ensure_sqlite_indexes(target_engine=engine) -> None:
    """幂等补齐旧库上create_all不会自动新增的索引。"""

    if target_engine.dialect.name != "sqlite":
        return
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_industry_reports_generation_mode ON industry_reports(generation_mode)",
        "CREATE INDEX IF NOT EXISTS ix_industry_reports_grounded_run_id ON industry_reports(grounded_run_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_data_sources_report_id ON industry_data_sources(report_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_data_sources_content_hash ON industry_data_sources(content_hash)",
        "CREATE INDEX IF NOT EXISTS ix_industry_data_sources_raw_content_hash ON industry_data_sources(raw_content_hash)",
        "CREATE INDEX IF NOT EXISTS ix_industry_data_sources_extracted_text_hash ON industry_data_sources(extracted_text_hash)",
        "CREATE INDEX IF NOT EXISTS ix_industry_data_sources_source_origin ON industry_data_sources(source_origin)",
        "CREATE INDEX IF NOT EXISTS ix_industry_data_sources_parse_status ON industry_data_sources(parse_status)",
        "CREATE INDEX IF NOT EXISTS ix_industry_data_sources_evidence_grade ON industry_data_sources(evidence_grade)",
        "CREATE INDEX IF NOT EXISTS ix_industry_source_chunks_report_id ON industry_source_chunks(report_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_source_chunks_source_id ON industry_source_chunks(source_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_source_chunks_content_hash ON industry_source_chunks(content_hash)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_industry_source_chunk_order ON industry_source_chunks(source_id, chunk_index)",
        "CREATE INDEX IF NOT EXISTS ix_industry_evidence_runs_report_id ON industry_evidence_extraction_runs(report_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_evidence_runs_snapshot ON industry_evidence_extraction_runs(source_snapshot_hash)",
        "CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_report_id ON industry_evidence_cards(report_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_source_id ON industry_evidence_cards(source_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_chunk_id ON industry_evidence_cards(chunk_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_run_id ON industry_evidence_cards(extraction_run_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_status ON industry_evidence_cards(validation_status)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflict_runs_report_id ON industry_conflict_detection_runs(report_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflict_runs_snapshot ON industry_conflict_detection_runs(evidence_snapshot_hash)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflicts_report_id ON industry_evidence_conflicts(report_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflicts_run_id ON industry_evidence_conflicts(detection_run_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflicts_type ON industry_evidence_conflicts(conflict_type)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflicts_severity ON industry_evidence_conflicts(severity)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflicts_resolution ON industry_evidence_conflicts(resolution_status)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflict_members_conflict_id ON industry_evidence_conflict_members(conflict_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflict_members_evidence_id ON industry_evidence_conflict_members(evidence_card_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_conflict_members_source_id ON industry_evidence_conflict_members(source_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_grounded_runs_report_id ON industry_grounded_report_runs(report_id)",
        "CREATE INDEX IF NOT EXISTS ix_industry_grounded_runs_evidence_snapshot ON industry_grounded_report_runs(evidence_snapshot_hash)",
        "CREATE INDEX IF NOT EXISTS ix_industry_grounded_runs_conflict_snapshot ON industry_grounded_report_runs(conflict_snapshot_hash)",
        "CREATE INDEX IF NOT EXISTS ix_industry_grounded_runs_status ON industry_grounded_report_runs(status)",
        "CREATE INDEX IF NOT EXISTS ix_entity_risks_published_at ON entity_risks(published_at)",
        "CREATE INDEX IF NOT EXISTS ix_entity_risks_provenance ON entity_risks(provenance)",
        "CREATE INDEX IF NOT EXISTS ix_entity_risks_review_status ON entity_risks(review_status)",
        "CREATE INDEX IF NOT EXISTS ix_risk_alert_deliveries_risk_id ON risk_alert_deliveries(entity_risk_id)",
        "CREATE INDEX IF NOT EXISTS ix_risk_alert_deliveries_status ON risk_alert_deliveries(status)",
        "CREATE INDEX IF NOT EXISTS ix_alert_channel_configs_channel ON alert_channel_configs(channel)",
    )
    with target_engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for statement in statements:
            if "entity_risks" in statement:
                table_name = "entity_risks"
            elif "risk_alert_deliveries" in statement:
                # Modified by DingJiaye: 2026-08-27 — 通知审计表仅存在于主库，
                # 行业独立库初始化时必须跳过该索引。
                table_name = "risk_alert_deliveries"
            elif "alert_channel_configs" in statement:
                table_name = "alert_channel_configs"
            elif "industry_reports_" in statement:
                table_name = "industry_reports"
            elif "grounded_runs" in statement:
                table_name = "industry_grounded_report_runs"
            elif "conflict_members" in statement:
                table_name = "industry_evidence_conflict_members"
            elif "conflict_runs" in statement:
                table_name = "industry_conflict_detection_runs"
            elif "industry_conflicts" in statement:
                table_name = "industry_evidence_conflicts"
            elif "evidence_runs" in statement:
                table_name = "industry_evidence_extraction_runs"
            elif "evidence_cards" in statement:
                table_name = "industry_evidence_cards"
            elif "source_chunks" in statement or "source_chunk_order" in statement:
                table_name = "industry_source_chunks"
            else:
                table_name = "industry_data_sources"
            if table_name in tables:
                conn.exec_driver_sql(statement)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
