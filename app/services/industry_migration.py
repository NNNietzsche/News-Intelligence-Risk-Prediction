"""将主库中的深度研报迁移到各行业独立数据库。"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.industry_db import (
    INDUSTRY_TABLE_NAMES,
    industry_db_path,
    init_all_industry_databases,
)
from app.database.models import IndustryReport
from app.industry_sectors import INDUSTRY_SECTORS, guess_sector_key
from app.services.data_source_service import (
    INDUSTRY_UPLOAD_ROOT,
    industry_report_upload_dir,
)

logger = logging.getLogger(__name__)

_COPY_ORDER = INDUSTRY_TABLE_NAMES


def _main_db_path() -> Path | None:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    rel = url.replace("sqlite:///", "", 1)
    if rel == ":memory:":
        return None
    path = Path(rel)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def migrate_main_db_industry_reports(main_db: Session) -> dict[str, int]:
    main_path = _main_db_path()
    if not main_path or not main_path.is_file():
        return {}

    # 启动前序任务可能给调用方会话留下只读事务。这里先提交前序工作，再仅提取
    # 标量快照并关闭本函数的查询事务；之后原生 sqlite3 连接才能安全写主库。
    if main_db.in_transaction():
        main_db.commit()
    try:
        rows = (
            main_db.query(IndustryReport.id, IndustryReport.industry_name)
            .order_by(IndustryReport.id.asc())
            .all()
        )
    except Exception:
        main_db.rollback()
        return {}
    main_db.rollback()
    if not rows:
        return {}

    init_all_industry_databases()
    moved = {key: 0 for key in INDUSTRY_SECTORS}
    for report_id, industry_name in rows:
        sector_key = guess_sector_key(industry_name)
        sector_path = industry_db_path(sector_key)
        _copy_report_with_attach(main_path, sector_path, report_id)
        _delete_report_from_main(main_path, report_id)
        _move_upload_dir(report_id, sector_key)
        moved[sector_key] = moved.get(sector_key, 0) + 1
    logger.info("深度研报已迁移完成: %s", moved)
    return moved


def _column_names(
    conn: sqlite3.Connection,
    table_name: str,
    schema: str = "main",
) -> list[str]:
    if schema == "main":
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    else:
        rows = conn.execute(f"PRAGMA {schema}.table_info({table_name})").fetchall()
    return [row[1] for row in rows]


def _copy_rows_by_columns(
    sector_conn: sqlite3.Connection,
    table_name: str,
    where_clause: str,
    params: tuple,
) -> None:
    dest_cols = _column_names(sector_conn, table_name)
    src_col_set = set(_column_names(sector_conn, table_name, schema="src"))
    cols = [col for col in dest_cols if col in src_col_set]
    if not cols:
        return
    col_sql = ", ".join(cols)
    select_sql = ", ".join(cols)
    sector_conn.execute(
        f"INSERT OR IGNORE INTO {table_name} ({col_sql}) "
        f"SELECT {select_sql} FROM src.{table_name} WHERE {where_clause}",
        params,
    )


def _copy_report_with_attach(
    main_path: Path,
    sector_path: Path,
    report_id: int,
) -> None:
    sector_conn = sqlite3.connect(sector_path, timeout=30)
    try:
        sector_conn.execute("PRAGMA busy_timeout=30000")
        sector_conn.execute("PRAGMA foreign_keys=OFF")
        sector_conn.execute("ATTACH DATABASE ? AS src", (str(main_path),))
        for table_name in _COPY_ORDER:
            if not _table_exists(sector_conn, table_name):
                continue
            if table_name == "industry_reports":
                _copy_rows_by_columns(
                    sector_conn,
                    table_name,
                    "id = ?",
                    (report_id,),
                )
                continue
            columns = _column_names(sector_conn, table_name)
            if "report_id" not in columns:
                continue
            _copy_rows_by_columns(
                sector_conn,
                table_name,
                "report_id = ?",
                (report_id,),
            )
        sector_conn.commit()
    finally:
        try:
            sector_conn.execute("DETACH DATABASE src")
        except sqlite3.Error:
            pass
        sector_conn.execute("PRAGMA foreign_keys=ON")
        sector_conn.close()


def _delete_report_from_main(main_path: Path, report_id: int) -> None:
    main_conn = sqlite3.connect(main_path, timeout=30)
    main_conn.execute("PRAGMA busy_timeout=30000")
    main_conn.execute("PRAGMA foreign_keys=ON")
    try:
        for table_name in reversed(_COPY_ORDER):
            if not _table_exists(main_conn, table_name):
                continue
            if table_name == "industry_reports":
                main_conn.execute(
                    "DELETE FROM industry_reports WHERE id = ?",
                    (report_id,),
                )
                continue
            columns = [row[1] for row in main_conn.execute(
                f"PRAGMA table_info({table_name})"
            ).fetchall()]
            if "report_id" not in columns:
                continue
            main_conn.execute(
                f"DELETE FROM {table_name} WHERE report_id = ?",
                (report_id,),
            )
        main_conn.commit()
    finally:
        main_conn.close()


def _move_upload_dir(report_id: int, sector_key: str) -> None:
    legacy_dir = INDUSTRY_UPLOAD_ROOT / str(report_id)
    if not legacy_dir.is_dir():
        return
    target_dir = industry_report_upload_dir(sector_key, report_id)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    shutil.move(str(legacy_dir), str(target_dir))
