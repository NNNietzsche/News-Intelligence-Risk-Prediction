"""统一权威数据源管理（全站共用 + 主体专属）。"""

from __future__ import annotations

import logging
import hashlib
import html
import json
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.database.models import IndustryDataSource, IndustryReport, IndustrySourceChunk, ModuleDataSource
from app.services.data_source_parser import (
    fetch_url_source,
    fetch_url_text,
    parse_text_with_chunks,
    parse_uploaded_file,
    parse_uploaded_file_with_chunks,
    truncate_text,
)
from app.services.source_registry import (
    apply_registry_metadata,
    build_chunk_rows,
    build_registry_metadata,
    infer_mime_type,
)

logger = logging.getLogger(__name__)

UPLOAD_ROOT = Path("data/uploads/modules")
INDUSTRY_UPLOAD_ROOT = Path("data/uploads/industry_reports")
# 统一数据源标记（兼容旧按模块上传的记录：列表与读取时一并纳入）
GLOBAL_SOURCE_CODE = "ALL"


class IndustryReportNotEditableError(ValueError):
    """目标报告已经冻结，不能再修改其数据源。"""


def ensure_upload_dirs() -> None:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    INDUSTRY_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


def industry_report_upload_dir(sector_key: str, report_id: int) -> Path:
    return INDUSTRY_UPLOAD_ROOT / sector_key / str(report_id)


def list_all_sources(db: Session, *, entity_id: int | None = None) -> list[ModuleDataSource]:
    """列出启用的数据源。

    - entity_id 为 None：仅全站共用源（entity_id IS NULL），供风险日报等使用
    - entity_id 有值：仅该主体专属源
    """
    q = db.query(ModuleDataSource).filter(ModuleDataSource.is_active.is_(True))
    if entity_id is None:
        q = q.filter(ModuleDataSource.entity_id.is_(None))
    else:
        q = q.filter(ModuleDataSource.entity_id == entity_id)
    return q.order_by(ModuleDataSource.priority.desc(), ModuleDataSource.created_at.desc()).all()


def get_source_by_id(db: Session, source_id: int) -> Optional[ModuleDataSource]:
    return db.query(ModuleDataSource).filter(ModuleDataSource.id == source_id).first()


def list_module_sources(db: Session, module_code: str | None = None) -> list[ModuleDataSource]:
    """兼容旧接口：返回全站共用数据源。"""
    return list_all_sources(db, entity_id=None)


def _compose_authority_text(sources: list[ModuleDataSource], max_chars: int) -> str:
    blocks: list[str] = []
    used = 0
    for src in sources:
        text = (src.extracted_text or "").strip()
        if not text:
            continue
        scope = f"主体#{src.entity_id}" if src.entity_id else "全站"
        header = f"【权威数据源({scope}): {src.name}】"
        chunk = f"{header}\n{text}"
        if used + len(chunk) > max_chars:
            remaining = max_chars - used
            if remaining <= 200:
                break
            chunk = chunk[:remaining] + "\n...[截断]"
        blocks.append(chunk)
        used += len(chunk)
    return "\n\n".join(blocks)


def get_shared_authoritative_text(db: Session, max_chars: int = 80_000) -> str:
    return _compose_authority_text(list_all_sources(db, entity_id=None), max_chars)


def get_entity_authoritative_text(
    db: Session,
    entity_id: int,
    max_chars: int = 80_000,
) -> str:
    """主体采集优先使用该主体专属数据源，并合并全站共用源。"""
    entity_sources = list_all_sources(db, entity_id=entity_id)
    shared = list_all_sources(db, entity_id=None)
    # 专属在前
    return _compose_authority_text([*entity_sources, *shared], max_chars)


def get_module_authoritative_text(db: Session, module_code: str, max_chars: int = 80_000) -> str:
    """任意模块流水线均读取全站共用数据源。"""
    return get_shared_authoritative_text(db, max_chars=max_chars)


def save_module_file_source(
    db: Session,
    module_code: str | None,
    name: str,
    filename: str,
    file_bytes: bytes,
    priority: int = 0,
    *,
    entity_id: int | None = None,
) -> ModuleDataSource:
    ensure_upload_dirs()
    code = GLOBAL_SOURCE_CODE
    dest_dir = UPLOAD_ROOT / (f"entity_{entity_id}" if entity_id else code)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    dest = dest_dir / safe_name
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
    dest.write_bytes(file_bytes)
    try:
        extracted = truncate_text(parse_uploaded_file(dest))
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    row = ModuleDataSource(
        module_code=code,
        entity_id=entity_id,
        name=name or safe_name,
        source_type="file",
        file_path=str(dest),
        original_filename=safe_name,
        extracted_text=extracted,
        priority=priority,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def save_module_url_source(
    db: Session,
    module_code: str | None,
    name: str,
    url: str,
    priority: int = 0,
    *,
    entity_id: int | None = None,
) -> ModuleDataSource:
    text = truncate_text(fetch_url_text(url))
    row = ModuleDataSource(
        module_code=GLOBAL_SOURCE_CODE,
        entity_id=entity_id,
        name=name or url,
        source_type="url",
        url=url,
        extracted_text=text,
        priority=priority,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_module_source(db: Session, source_id: int) -> bool:
    row = db.query(ModuleDataSource).filter(ModuleDataSource.id == source_id).first()
    if not row:
        return False
    if row.file_path:
        path = Path(row.file_path)
        if path.is_file():
            path.unlink(missing_ok=True)
    db.delete(row)
    db.commit()
    return True


def list_industry_sources(db: Session, report_id: int) -> list[IndustryDataSource]:
    return (
        db.query(IndustryDataSource)
        .filter(IndustryDataSource.report_id == report_id)
        .order_by(IndustryDataSource.created_at.asc(), IndustryDataSource.id.asc())
        .all()
    )


def get_industry_source_by_id(
    db: Session, source_id: int
) -> Optional[IndustryDataSource]:
    return db.query(IndustryDataSource).filter(IndustryDataSource.id == source_id).first()


def list_industry_source_chunks(
    db: Session, report_id: int, source_id: int
) -> list[IndustrySourceChunk]:
    return (
        db.query(IndustrySourceChunk)
        .filter(
            IndustrySourceChunk.report_id == report_id,
            IndustrySourceChunk.source_id == source_id,
        )
        .order_by(IndustrySourceChunk.chunk_index.asc())
        .all()
    )


def build_industry_authoritative_text(
    db: Session, report_id: int, max_chars: int = 100_000
) -> tuple[str, list[dict]]:
    """构建单份报告的输入，并记录每个来源实际进入模型的字符数。"""
    sources = [
        s for s in list_industry_sources(db, report_id)
        if s.is_selected and (s.extracted_text or "").strip()
    ]
    blocks: list[str] = []
    manifest: list[dict] = []
    remaining = max_chars
    for index, src in enumerate(sources):
        text = (src.extracted_text or "").strip()
        if src.source_type == "network_search":
            # Keep the original fields visible in the source viewer for audit,
            # but do not feed untranslated trace text back into report writing.
            text = "\n".join(
                line for line in text.splitlines()
                if not line.startswith(("原始标题：", "原始摘要（追溯）："))
            ).strip()
        header = f"【研报数据源 #{src.id}: {src.name}】\n"
        separator_cost = 2 if blocks else 0
        available = max(0, remaining - separator_cost)
        sources_left = len(sources) - index
        allocation = max(0, available // sources_left)
        marker = "\n...[本数据源因上下文上限截断]"
        raw_body_budget = max(0, allocation - len(header))
        will_truncate = len(text) > raw_body_budget
        body_budget = max(0, raw_body_budget - (len(marker) if will_truncate else 0))
        included = min(len(text), body_budget)
        chunk = header + text[:included]
        truncated = included < len(text)
        if truncated:
            chunk += marker
        blocks.append(chunk)
        remaining = max(0, remaining - separator_cost - len(chunk))
        manifest.append(
            {
                "source_id": src.id,
                "name": src.name,
                "source_type": src.source_type,
                "content_hash": src.content_hash,
                "attached_chars": len(text),
                "included_chars": included,
                "truncated": truncated,
            }
        )
    return "\n\n".join(blocks), manifest


def get_industry_authoritative_text(db: Session, report_id: int, max_chars: int = 100_000) -> str:
    text, _ = build_industry_authoritative_text(db, report_id, max_chars=max_chars)
    return text


def set_industry_source_selection(
    db: Session, report_id: int, source_ids: list[int]
) -> list[IndustryDataSource]:
    """Persist the source set intentionally selected for the next report run."""
    _editable_industry_report(db, report_id, allow_completed=True)
    selected_ids = {int(source_id) for source_id in source_ids}
    rows = list_industry_sources(db, report_id)
    known_ids = {row.id for row in rows}
    unknown_ids = selected_ids - known_ids
    if unknown_ids:
        raise ValueError("存在不属于当前报告的数据源")
    for row in rows:
        row.is_selected = row.id in selected_ids
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def _editable_industry_report(
    db: Session, report_id: int, *, allow_completed: bool = False
) -> IndustryReport:
    report = (
        db.query(IndustryReport)
        .populate_existing()
        .filter(IndustryReport.id == report_id)
        .first()
    )
    if not report:
        raise ValueError("报告不存在")
    if report.status == "running":
        raise IndustryReportNotEditableError("报告正在生成，暂不能修改数据源")
    allowed = {"draft", "failed"}
    if allow_completed:
        allowed.update({"completed", "awaiting_approval"})
    if report.status not in allowed:
        raise IndustryReportNotEditableError("当前报告不可修改数据源")
    if report.status == "failed":
        report.status = "draft"
        report.error_message = None
    return report


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summarize_network_title(title: str, snippet: str, max_chars: int = 72) -> str:
    """将搜索结果标题压缩成适合侧栏展示的一行摘要。"""
    candidate = html.unescape((title or "").strip())
    if not candidate:
        candidate = html.unescape((snippet or "").strip()).split("。", 1)[0]
    candidate = re.sub(r"\s+", " ", candidate).strip(" -_|｜") or "网络搜索结果"
    if len(candidate) > max_chars:
        candidate = candidate[: max_chars - 1].rstrip() + "…"
    return candidate


def append_industry_network_search_sources(
    db: Session,
    report_id: int,
    items: list[object],
    translator: Optional[Callable[[str, str], tuple[str, str]]] = None,
    require_translation: bool = False,
    is_selected: bool = True,
) -> list[IndustryDataSource]:
    """把补充网络搜索结果固化为当前报告的专属数据源。

    该入口只供正在生成的报告调用；按 URL 和内容哈希去重，避免失败重试或
    新版继承后重复写入同一搜索结果。
    """
    report = (
        db.query(IndustryReport)
        .populate_existing()
        .filter(IndustryReport.id == report_id)
        .first()
    )
    if not report:
        raise ValueError("报告不存在")
    if report.status not in {"running", "draft", "failed", "completed", "awaiting_approval"}:
        raise ValueError("当前报告不可添加搜索结果")

    existing = list_industry_sources(db, report_id)
    existing_urls = {(src.url or "").strip() for src in existing if src.url}
    existing_hashes = {src.content_hash for src in existing if src.content_hash}
    created: list[IndustryDataSource] = []
    try:
        for item in items:
            title = str(getattr(item, "title", "") or "")
            url = str(getattr(item, "url", "") or "").strip()
            snippet = str(getattr(item, "snippet", "") or "")
            published_at = str(getattr(item, "published_at", "") or "")
            source_domain = str(getattr(item, "source_domain", "") or "")
            original_title = re.sub(r"\s+", " ", html.unescape(title)).strip()
            original_snippet = re.sub(r"\s+", " ", html.unescape(snippet)).strip()
            used_translation = False
            if translator is not None:
                try:
                    translated_title, translated_snippet = translator(title, snippet)
                    summary_title = _summarize_network_title(translated_title, translated_snippet)
                    display_snippet = str(translated_snippet or "").strip()
                    if require_translation and not re.search(
                        r"[\u4e00-\u9fff]", summary_title + display_snippet
                    ):
                        raise ValueError("network_source_translation_not_chinese")
                    used_translation = True
                except Exception:
                    if require_translation:
                        raise
                    logger.warning("网络来源翻译失败，已保留原文: url=%s", url or title)
                    summary_title = _summarize_network_title(title, snippet)
                    display_snippet = original_snippet
            else:
                summary_title = _summarize_network_title(title, snippet)
                display_snippet = original_snippet
                if require_translation:
                    raise ValueError("network_source_translation_not_chinese")
            text = "\n".join(
                part
                for part in (
                    "【来源：补充网络搜索功能】",
                    f"标题摘要：{summary_title}",
                    f"原始标题：{original_title}",
                    f"来源网站：{source_domain}" if source_domain else "",
                    f"发布时间：{published_at}" if published_at else "",
                    f"搜索摘要：{display_snippet}",
                    f"原始摘要（追溯）：{original_snippet}" if used_translation else "",
                    f"原始链接：{url}" if url else "",
                )
                if part
            )
            content_hash = _content_hash(text)
            if (url and url in existing_urls) or content_hash in existing_hashes:
                continue
            parsed = parse_text_with_chunks(text, format_name="network_search")
            metadata = build_registry_metadata(
                raw_content=text.encode("utf-8"),
                parsed=parsed,
                source_origin="network_search",
                mime_type="text/plain",
                published_at=published_at or None,
                is_full_text=False,
            )
            row = IndustryDataSource(
                report_id=report_id,
                name=summary_title,
                source_type="network_search",
                url=url or None,
                extracted_text=parsed.extracted_text,
                is_selected=is_selected,
                content_hash=content_hash,
                char_count=len(parsed.extracted_text),
            )
            apply_registry_metadata(row, metadata)
            db.add(row)
            db.flush()
            db.add_all(build_chunk_rows(report_id=report_id, source_id=row.id, parsed=parsed))
            created.append(row)
            if url:
                existing_urls.add(url)
            existing_hashes.add(content_hash)
        db.commit()
    except Exception:
        db.rollback()
        raise
    for row in created:
        db.refresh(row)
    return created


def _unique_destination(report_id: int, filename: str) -> Path:
    dest_dir = INDUSTRY_UPLOAD_ROOT / str(report_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    dest = dest_dir / safe_name
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{Path(safe_name).stem}_{counter}{Path(safe_name).suffix}"
        counter += 1
    return dest


def save_industry_file_source(
    db: Session,
    report_id: int,
    name: str,
    filename: str,
    file_bytes: bytes,
) -> IndustryDataSource:
    _editable_industry_report(db, report_id, allow_completed=True)
    ensure_upload_dirs()
    safe_name = Path(filename).name
    dest = _unique_destination(report_id, safe_name)
    dest.write_bytes(file_bytes)
    try:
        parsed = parse_uploaded_file_with_chunks(dest)
        metadata = build_registry_metadata(
            raw_content=file_bytes,
            parsed=parsed,
            source_origin="customer_file",
            mime_type=infer_mime_type(safe_name),
            is_full_text=True,
        )
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise ValueError(f"文件解析失败: {exc}") from exc
    try:
        _editable_industry_report(db, report_id, allow_completed=True)
        row = IndustryDataSource(
            report_id=report_id,
            name=name or safe_name,
            source_type="file",
            file_path=str(dest),
            original_filename=safe_name,
            extracted_text=parsed.extracted_text,
            # Preserve the existing content_hash contract: hash of extracted text.
            content_hash=_content_hash(parsed.extracted_text),
            char_count=len(parsed.extracted_text),
        )
        apply_registry_metadata(row, metadata)
        db.add(row)
        db.flush()
        db.add_all(build_chunk_rows(report_id=report_id, source_id=row.id, parsed=parsed))
        db.commit()
    except Exception:
        db.rollback()
        dest.unlink(missing_ok=True)
        raise
    db.refresh(row)
    return row


def save_industry_url_source(
    db: Session,
    report_id: int,
    name: str,
    url: str,
) -> IndustryDataSource:
    _editable_industry_report(db, report_id, allow_completed=True)
    try:
        fetched = fetch_url_source(url)
        metadata = build_registry_metadata(
            raw_content=fetched.raw_content,
            parsed=fetched.parsed,
            source_origin="customer_url",
            mime_type=fetched.mime_type,
            is_full_text=True,
        )
        _editable_industry_report(db, report_id, allow_completed=True)
        row = IndustryDataSource(
            report_id=report_id,
            name=name or url,
            source_type="url",
            url=url,
            extracted_text=fetched.parsed.extracted_text,
            content_hash=_content_hash(fetched.parsed.extracted_text),
            char_count=len(fetched.parsed.extracted_text),
        )
        apply_registry_metadata(row, metadata)
        db.add(row)
        db.flush()
        db.add_all(
            build_chunk_rows(report_id=report_id, source_id=row.id, parsed=fetched.parsed)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return row


def clone_industry_sources(
    db: Session, source_report_id: int, target_report_id: int, *, require_editable: bool = True
) -> int:
    if require_editable:
        _editable_industry_report(db, target_report_id)
    count = 0
    for src in list_industry_sources(db, source_report_id):
        _clone_industry_source(db, src, target_report_id, is_selected=bool(src.is_selected))
        count += 1
    if require_editable:
        _editable_industry_report(db, target_report_id)
    db.commit()
    return count


def _clone_industry_source(
    db: Session,
    src: IndustryDataSource,
    target_report_id: int,
    *,
    is_selected: bool,
    source_origin: str | None = None,
) -> IndustryDataSource:
    """Copy a source snapshot and its parsed chunks into a new report."""
    copied_path: str | None = None
    if src.file_path and Path(src.file_path).is_file():
        filename = src.original_filename or Path(src.file_path).name
        dest = _unique_destination(target_report_id, filename)
        shutil.copy2(src.file_path, dest)
        copied_path = str(dest)
    clone = IndustryDataSource(
        report_id=target_report_id,
        copied_from_source_id=src.id,
        name=src.name,
        source_type=src.source_type,
        file_path=copied_path,
        original_filename=src.original_filename,
        url=src.url,
        extracted_text=src.extracted_text,
        is_selected=is_selected,
        content_hash=src.content_hash,
        char_count=src.char_count,
        raw_content_hash=src.raw_content_hash,
        extracted_text_hash=src.extracted_text_hash,
        mime_type=src.mime_type,
        file_size=src.file_size,
        source_origin=source_origin or src.source_origin,
        source_publisher=src.source_publisher,
        published_at=src.published_at,
        retrieved_at=src.retrieved_at,
        is_full_text=src.is_full_text,
        is_truncated=src.is_truncated,
        parse_status=src.parse_status,
        parse_warning=src.parse_warning,
        used_ocr=src.used_ocr,
        page_count=src.page_count,
        slide_count=src.slide_count,
        sheet_count=src.sheet_count,
        evidence_grade=src.evidence_grade,
    )
    db.add(clone)
    db.flush()
    for source_chunk in sorted(src.chunks, key=lambda item: item.chunk_index):
        db.add(
            IndustrySourceChunk(
                report_id=target_report_id,
                source_id=clone.id,
                chunk_index=source_chunk.chunk_index,
                text=source_chunk.text,
                locator=source_chunk.locator,
                page_number=source_chunk.page_number,
                slide_number=source_chunk.slide_number,
                sheet_name=source_chunk.sheet_name,
                cell_range=source_chunk.cell_range,
                row_range=source_chunk.row_range,
                paragraph_index=source_chunk.paragraph_index,
                table_index=source_chunk.table_index,
                table_row_index=source_chunk.table_row_index,
                char_start=source_chunk.char_start,
                char_end=source_chunk.char_end,
                content_hash=source_chunk.content_hash,
            )
        )
    return clone


def _industry_source_reuse_key(src: IndustryDataSource) -> tuple[str, str]:
    """Stable key so repeated report versions do not multiply the source library."""
    if (src.url or "").strip():
        return ("url", src.url.strip().lower())
    if (src.raw_content_hash or "").strip():
        return ("raw", src.raw_content_hash.strip())
    if (src.content_hash or "").strip():
        return ("content", src.content_hash.strip())
    return ("name", f"{src.source_type}:{src.name}".strip().lower())


def adopt_sources_into_library(db: Session, library_id: int) -> int:
    """Move unique report-owned sources into the industry library."""
    library_rows = list_industry_sources(db, library_id)
    seen = {_industry_source_reuse_key(src) for src in library_rows}
    moved = 0
    others = (
        db.query(IndustryDataSource)
        .filter(IndustryDataSource.report_id != library_id)
        .order_by(IndustryDataSource.created_at.desc(), IndustryDataSource.id.desc())
        .all()
    )
    for src in others:
        key = _industry_source_reuse_key(src)
        if key in seen:
            continue
        src.report_id = library_id
        for chunk in src.chunks:
            chunk.report_id = library_id
        seen.add(key)
        moved += 1
    if moved:
        db.commit()
    return moved


def replace_report_sources_from_library(
    db: Session, report_id: int, library_id: int
) -> int:
    """Copy the industry library onto a report for one generation run."""
    if report_id == library_id:
        return len(list_industry_sources(db, report_id))
    for src in list_industry_sources(db, report_id):
        if src.file_path:
            library_paths = {
                row.file_path for row in list_industry_sources(db, library_id) if row.file_path
            }
            if src.file_path not in library_paths:
                Path(src.file_path).unlink(missing_ok=True)
        db.delete(src)
    db.commit()
    return clone_industry_sources(db, library_id, report_id, require_editable=False)


def clone_industry_library_sources(
    db: Session, industry_name: str, target_report_id: int
) -> int:
    """Bring unique historical sources from the same industry into a fresh draft.

    The copied sources remain report-scoped snapshots for auditability, but their
    parsed text and uploaded file copies are reusable. They start unchecked so a
    user explicitly controls the evidence set for the new report.
    """
    _editable_industry_report(db, target_report_id)
    normalized_name = " ".join((industry_name or "").split())
    if not normalized_name:
        return 0
    candidates = (
        db.query(IndustryDataSource)
        .join(IndustryReport, IndustryDataSource.report_id == IndustryReport.id)
        .filter(
            IndustryReport.industry_name == normalized_name,
            IndustryReport.library_saved.is_(True),
            IndustryDataSource.report_id != target_report_id,
        )
        .order_by(IndustryDataSource.created_at.desc(), IndustryDataSource.id.desc())
        .all()
    )
    seen: set[tuple[str, str]] = set()
    count = 0
    for src in candidates:
        key = _industry_source_reuse_key(src)
        if key in seen:
            continue
        seen.add(key)
        _clone_industry_source(
            db,
            src,
            target_report_id,
            is_selected=False,
            source_origin="industry_library",
        )
        count += 1
    db.commit()
    return count


def delete_industry_source(db: Session, report_id: int, source_id: int) -> bool:
    row = (
        db.query(IndustryDataSource)
        .filter(IndustryDataSource.id == source_id, IndustryDataSource.report_id == report_id)
        .first()
    )
    if not row:
        return False
    report = db.query(IndustryReport).filter(IndustryReport.id == report_id).first()
    if not report:
        return False
    if report.status == "running":
        raise IndustryReportNotEditableError("报告正在生成，暂不能修改数据源")
    frozen = report.status in {"completed", "awaiting_approval"}
    if report.status not in {"draft", "failed", "completed", "awaiting_approval"}:
        raise IndustryReportNotEditableError("当前报告不可修改数据源")
    if report.status == "failed":
        report.status = "draft"
        report.error_message = None
    if row.file_path:
        Path(row.file_path).unlink(missing_ok=True)
    db.delete(row)
    # Keep the last-run source manifest frozen so the displayed report is unchanged.
    if report.source_manifest_json and not frozen:
        try:
            manifest = json.loads(report.source_manifest_json)
            if isinstance(manifest, list):
                report.source_manifest_json = json.dumps(
                    [item for item in manifest if item.get("source_id") != source_id],
                    ensure_ascii=False,
                )
        except (TypeError, ValueError):
            logger.warning("无法更新已完成报告的数据源清单: report_id=%s", report_id)
    db.commit()
    return True


def _safe_dirname(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip())
    return cleaned[:64] or "default"
