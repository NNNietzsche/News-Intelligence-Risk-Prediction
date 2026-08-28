"""深度研报来源登记规则与定位切片持久化。"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.database.models import IndustryDataSource, IndustrySourceChunk
from app.services.data_source_parser import ParsedSource

SOURCE_ORIGINS = {"customer_file", "customer_url", "network_search"}
EVIDENCE_GRADES = {"full_text", "partial_text", "lead_only"}


@dataclass(frozen=True)
class SourceRegistryMetadata:
    raw_content_hash: str
    extracted_text_hash: str
    mime_type: str | None
    file_size: int
    source_origin: str
    source_publisher: str | None
    published_at: str | None
    retrieved_at: datetime
    is_full_text: bool
    is_truncated: bool
    parse_status: str
    parse_warning: str | None
    used_ocr: bool
    page_count: int | None
    slide_count: int | None
    sheet_count: int | None
    evidence_grade: str


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def infer_mime_type(filename: str | None) -> str | None:
    if not filename:
        return None
    guessed, _ = mimetypes.guess_type(Path(filename).name)
    return guessed


def build_registry_metadata(
    *,
    raw_content: bytes,
    parsed: ParsedSource,
    source_origin: str,
    mime_type: str | None = None,
    source_publisher: str | None = None,
    published_at: str | None = None,
    is_full_text: bool = True,
    retrieved_at: datetime | None = None,
) -> SourceRegistryMetadata:
    if source_origin not in SOURCE_ORIGINS:
        raise ValueError(f"未知信源来源类型: {source_origin}")
    if source_origin == "network_search":
        is_full_text = False
        evidence_grade = "lead_only"
    elif not is_full_text or parsed.is_truncated:
        evidence_grade = "partial_text"
    else:
        evidence_grade = "full_text"
    return SourceRegistryMetadata(
        raw_content_hash=sha256_bytes(raw_content),
        extracted_text_hash=sha256_text(parsed.extracted_text),
        mime_type=mime_type,
        file_size=len(raw_content),
        source_origin=source_origin,
        source_publisher=source_publisher or None,
        published_at=published_at or None,
        retrieved_at=retrieved_at or datetime.now(UTC).replace(tzinfo=None),
        is_full_text=is_full_text,
        is_truncated=parsed.is_truncated,
        parse_status="parsed",
        parse_warning="\n".join(parsed.warnings) if parsed.warnings else None,
        used_ocr=parsed.used_ocr,
        page_count=_optional_int(parsed.parse_metadata.get("page_count")),
        slide_count=_optional_int(parsed.parse_metadata.get("slide_count")),
        sheet_count=_optional_int(parsed.parse_metadata.get("sheet_count")),
        evidence_grade=evidence_grade,
    )


def apply_registry_metadata(
    row: IndustryDataSource, metadata: SourceRegistryMetadata
) -> IndustryDataSource:
    for name, value in metadata.__dict__.items():
        setattr(row, name, value)
    return row


def build_chunk_rows(
    *, report_id: int, source_id: int, parsed: ParsedSource
) -> list[IndustrySourceChunk]:
    return [
        IndustrySourceChunk(
            report_id=report_id,
            source_id=source_id,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            locator=chunk.locator,
            page_number=chunk.page_number,
            slide_number=chunk.slide_number,
            sheet_name=chunk.sheet_name,
            cell_range=chunk.cell_range,
            row_range=chunk.row_range,
            paragraph_index=chunk.paragraph_index,
            table_index=chunk.table_index,
            table_row_index=chunk.table_row_index,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            content_hash=chunk.content_hash,
        )
        for chunk in parsed.chunks
    ]


def source_registry_state(row: IndustryDataSource) -> str:
    """旧数据不自动重解析；调用方可据此显示待结构化状态。"""

    if not row.parse_status or not row.extracted_text_hash:
        return "legacy_unstructured"
    return row.parse_status


def _optional_int(value) -> int | None:
    if value is None:
        return None
    return int(value)
