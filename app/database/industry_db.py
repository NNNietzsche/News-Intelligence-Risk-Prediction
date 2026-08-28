"""按行业分类列表管理 SQLite 数据库与会话。"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from fastapi import Header, HTTPException, Query
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Base, IndustryReport
from app.database.session import _ensure_sqlite_indexes, _migrate_sqlite_columns
from app.industry_sectors import INDUSTRY_SECTORS, refresh_sectors, require_sector_key

INDUSTRY_DB_DIR = Path("data/industry_dbs")
INDUSTRY_TABLE_NAMES = (
    "industry_reports",
    "industry_data_sources",
    "industry_source_chunks",
    "industry_evidence_extraction_runs",
    "industry_evidence_cards",
    "industry_conflict_detection_runs",
    "industry_evidence_conflicts",
    "industry_evidence_conflict_members",
    "industry_grounded_report_runs",
    "industry_analysis_reports",
)

current_industry_sector: ContextVar[str | None] = ContextVar(
    "current_industry_sector", default=None
)

_engines: dict[str, object] = {}
_sessionmakers: dict[str, sessionmaker] = {}


def industry_tables() -> list[object]:
    return [
        Base.metadata.tables[name]
        for name in INDUSTRY_TABLE_NAMES
        if name in Base.metadata.tables
    ]


def industry_db_path(sector_key: str) -> Path:
    require_sector_key(sector_key)
    return INDUSTRY_DB_DIR / f"{sector_key}.sqlite3"


def _sqlite_pragma(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=8000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_industry_engine(sector_key: str):
    require_sector_key(sector_key)
    if sector_key not in _engines:
        INDUSTRY_DB_DIR.mkdir(parents=True, exist_ok=True)
        db_path = industry_db_path(sector_key)
        url = f"sqlite:///{db_path.as_posix()}"
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=os.environ.get("SQL_ECHO", "").lower() in ("1", "true"),
        )
        event.listens_for(engine, "connect")(_sqlite_pragma)
        _engines[sector_key] = engine
    return _engines[sector_key]


def get_industry_sessionmaker(sector_key: str) -> sessionmaker:
    if sector_key not in _sessionmakers:
        _sessionmakers[sector_key] = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_industry_engine(sector_key),
        )
    return _sessionmakers[sector_key]


def init_industry_database(sector_key: str) -> None:
    engine = get_industry_engine(sector_key)
    tables = industry_tables()
    if tables:
        Base.metadata.create_all(bind=engine, tables=tables)
    _migrate_sqlite_columns(engine)
    Base.metadata.create_all(bind=engine, tables=tables)
    _ensure_sqlite_indexes(engine)


def init_all_industry_databases() -> None:
    refresh_sectors()
    for sector_key in INDUSTRY_SECTORS:
        init_industry_database(sector_key)


def resolve_sector_key(
    x_industry_sector: str | None = Header(
        None,
        alias="X-Industry-Sector",
        description="行业分类（请求头或查询 GET 参数）",
    ),
    sector: str | None = Query(None),
) -> str:
    key = (x_industry_sector or sector or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="未指定行业分类")
    try:
        require_sector_key(key)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="无效行业分类") from exc
    return key


def _open_industry_session(sector_key: str) -> Session:
    init_industry_database(sector_key)
    session = get_industry_sessionmaker(sector_key)()
    current_industry_sector.set(sector_key)
    return session


def _close_industry_session(session: Session) -> None:
    """关闭会话。ContextVar 在 FastAPI 线程池退出时可能跨 Context，忽略 reset 失败。"""
    try:
        current_industry_sector.set(None)
    except Exception:
        pass
    session.close()


def get_industry_db(sector_key: str) -> Generator[Session, None, None]:
    try:
        require_sector_key(sector_key)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="无效行业分类") from exc
    session = _open_industry_session(sector_key)
    try:
        yield session
    finally:
        _close_industry_session(session)


def get_industry_db_with_query(
    x_industry_sector: str | None = Header(
        None,
        alias="X-Industry-Sector",
        description="行业分类（请求头或查询 GET 参数）",
    ),
    sector: str | None = Query(None),
) -> Generator[Session, None, None]:
    key = resolve_sector_key(x_industry_sector=x_industry_sector, sector=sector)
    session = _open_industry_session(key)
    try:
        yield session
    finally:
        _close_industry_session(session)


def open_industry_session(sector_key: str) -> Session:
    """供页面路由同步调用；调用方负责 close。"""
    require_sector_key(sector_key)
    return _open_industry_session(sector_key)


@contextmanager
def industry_session(sector_key: str) -> Generator[Session, None, None]:
    session = _open_industry_session(sector_key)
    try:
        yield session
    finally:
        _close_industry_session(session)


def find_report_sector(report_id: int) -> str | None:
    """在各行业库中查找报告所属分类。"""
    refresh_sectors()
    for sector_key in INDUSTRY_SECTORS:
        init_industry_database(sector_key)
        session = get_industry_sessionmaker(sector_key)()
        try:
            exists = session.get(IndustryReport, report_id)
            if exists:
                return sector_key
        finally:
            session.close()
    return None


def dispose_industry_database(sector_key: str) -> None:
    """关闭并移除行业库连接（删除文件前调用）。"""
    maker = _sessionmakers.pop(sector_key, None)
    engine = _engines.pop(sector_key, None)
    if maker is not None:
        maker.close_all()
    if engine is not None:
        engine.dispose()


def drop_industry_database(sector_key: str) -> None:
    """删除行业 SQLite 文件（含 WAL/SHM）。"""
    dispose_industry_database(sector_key)
    db_path = INDUSTRY_DB_DIR / f"{sector_key}.sqlite3"
    for path in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def list_all_sector_reports(limit: int = 40) -> list[dict]:
    """跨行业汇总报告列表，按创建时间倒序。"""
    refresh_sectors()
    items: list[dict] = []
    for sector_key, sector in INDUSTRY_SECTORS.items():
        init_industry_database(sector_key)
        session = get_industry_sessionmaker(sector_key)()
        try:
            rows = (
                session.query(IndustryReport)
                .filter(IndustryReport.is_source_library.is_(False))
                .order_by(IndustryReport.created_at.desc())
                .limit(limit)
                .all()
            )
            for row in rows:
                items.append(
                    {
                        "id": row.id,
                        "sector_key": sector_key,
                        "sector_label": sector.label,
                        "industry_name": row.industry_name,
                        "company_name": row.company_name,
                        "report_name": row.report_name,
                        "version": row.version,
                        "status": row.status,
                        "created_at": row.created_at,
                    }
                )
        finally:
            session.close()
    items.sort(
        key=lambda item: item["created_at"] or datetime.min,
        reverse=True,
    )
    return items[:limit]

