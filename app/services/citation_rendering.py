"""Report-scoped citation presentation and grounded export gating.

Internal evidence codes remain immutable.  This module only assigns stable display
numbers and exposes a deliberately small, safe view of the underlying evidence.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.database.models import (
    IndustryDataSource,
    IndustryEvidenceCard,
    IndustryEvidenceConflict,
    IndustryEvidenceConflictMember,
    IndustryReport,
    IndustrySourceChunk,
)
from app.services.citation_validation import CITATION_RE, validate_citations
from app.services.evidence_packet import build_evidence_packet


@dataclass(frozen=True)
class CitationPresentationError(ValueError):
    code: str
    message: str
    next_step: str = "refresh the report and try again"

    def __str__(self) -> str:
        return self.message


def _payload(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "null")
    except (TypeError, json.JSONDecodeError) as exc:
        raise CitationPresentationError(
            "REPORT_JSON_INVALID", "报告正文不是有效的结构化JSON。", "regenerate the report"
        ) from exc
    if not isinstance(parsed, dict):
        raise CitationPresentationError(
            "REPORT_JSON_INVALID", "报告正文缺少结构化对象。", "regenerate the report"
        )
    return parsed


def _citation_occurrences(report: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (code, location) in deterministic display order."""
    fields: list[tuple[str, str]] = [("summary", str(report.get("summary") or ""))]
    for index, section in enumerate(report.get("sections") or []):
        if isinstance(section, dict):
            fields.append((f"sections[{index}].content", str(section.get("content") or "")))
    fields.append(("risk_outlook", str(report.get("risk_outlook") or "")))
    found: list[tuple[str, str]] = []
    for location, text in fields:
        found.extend((code, location) for code in CITATION_RE.findall(text))
    for index, metric in enumerate(report.get("key_metrics") or []):
        if isinstance(metric, dict) and metric.get("evidence_code"):
            found.append((str(metric["evidence_code"]), f"key_metrics[{index}]"))
    return found


def citation_number_map(report: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for code, _location in _citation_occurrences(report):
        if code not in result:
            result[code] = len(result) + 1
    return result


def _safe_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _bounded_quote(value: str, soft_limit: int = 900, hard_limit: int = 1400) -> tuple[str, bool]:
    text = (value or "").strip()
    if len(text) <= soft_limit:
        return text, False
    safe_marks = "。！？!?；;\n"
    before = max(text.rfind(mark, int(soft_limit * 0.65), soft_limit + 1) for mark in safe_marks)
    if before >= 0:
        return text[: before + 1].rstrip() + "（摘录已截断）", True
    after_positions = [text.find(mark, soft_limit, hard_limit + 1) for mark in safe_marks]
    after_positions = [position for position in after_positions if position >= 0]
    if after_positions:
        end = min(after_positions) + 1
        return text[:end].rstrip() + "（摘录已截断）", True
    # Avoid splitting a compact number/unit token; whitespace is the last safe fallback.
    whitespace = max(text.rfind(" ", soft_limit, hard_limit + 1), text.rfind("\t", soft_limit, hard_limit + 1))
    if whitespace >= 0:
        return text[:whitespace].rstrip() + "（摘录已截断）", True
    end = hard_limit
    # A compact source can have no punctuation or spaces. Move the boundary
    # before any numeric/currency/year/unit token that overlaps the hard cap.
    token_re = re.compile(
        r"(?:JPY|CNY|RMB|USD|EUR|[￥¥$€])?[+-]?\d[\d,，]*(?:\.\d+)?"
        r"(?:%|％|亿元|万元|百万|亿|万|年|月|日|GWh|MWh|kWh|GW|MW|kW|JPY|CNY|USD|EUR)?"
    )
    window_start = max(0, hard_limit - 96)
    for match in token_re.finditer(text, window_start, min(len(text), hard_limit + 96)):
        if match.start() < hard_limit < match.end():
            end = match.start()
            break
    for marker in ("尚未", "并未", "没有", "从未", "否认"):
        marker_start = text.rfind(marker, max(0, end - len(marker)), min(len(text), end + len(marker)))
        if marker_start >= 0 and marker_start < end < marker_start + len(marker):
            end = marker_start
            break
    return text[:end].rstrip() + "（摘录已截断）", True


def _declared_pairs(report: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(item.get("evidence_code")), str(item.get("location")))
        for item in report.get("citations") or []
        if isinstance(item, dict) and item.get("evidence_code") and item.get("location")
    }


def build_citation_context(
    db: Session,
    report: IndustryReport,
    report_json: str | dict[str, Any] | None = None,
    *,
    enforce_export_gate: bool = False,
) -> dict[str, Any]:
    payload = _payload(report_json if report_json is not None else report.report_json)
    number_map = citation_number_map(payload)
    pairs = _citation_occurrences(payload)
    declared = _declared_pairs(payload)
    codes = list(number_map)

    cards = db.query(IndustryEvidenceCard).filter(
        IndustryEvidenceCard.report_id == report.id,
        IndustryEvidenceCard.evidence_code.in_(codes or [""]),
    ).all()
    card_map = {card.evidence_code: card for card in cards}
    source_ids = {card.source_id for card in cards}
    chunk_ids = {card.chunk_id for card in cards}
    sources = db.query(IndustryDataSource).filter(
        IndustryDataSource.report_id == report.id,
        IndustryDataSource.id.in_(source_ids or {-1}),
    ).all()
    chunks = db.query(IndustrySourceChunk).filter(
        IndustrySourceChunk.report_id == report.id,
        IndustrySourceChunk.id.in_(chunk_ids or {-1}),
    ).all()
    source_map = {source.id: source for source in sources}
    chunk_map = {chunk.id: chunk for chunk in chunks}

    members = db.query(IndustryEvidenceConflictMember).filter(
        IndustryEvidenceConflictMember.evidence_code.in_(codes or [""])
    ).all()
    conflict_ids = {member.conflict_id for member in members}
    conflicts = db.query(IndustryEvidenceConflict).filter(
        IndustryEvidenceConflict.report_id == report.id,
        IndustryEvidenceConflict.id.in_(conflict_ids or {-1}),
    ).all()
    conflict_map = {conflict.id: conflict for conflict in conflicts}
    conflicts_by_code: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    for member in members:
        conflict = conflict_map.get(member.conflict_id)
        if not conflict:
            continue
        conflicts_by_code.setdefault(member.evidence_code, []).append({
            "conflict_code": conflict.conflict_code,
            "severity": conflict.severity,
            "status": conflict.resolution_status,
            "description": conflict.description,
            "resolution_note": conflict.resolution_note,
            "selected_evidence_code": conflict.selected_evidence_code,
        })

    warnings: list[dict[str, str]] = []
    details: list[dict[str, Any]] = []
    valid_codes: set[str] = set()
    for code, number in number_map.items():
        locations = sorted({location for item_code, location in pairs if item_code == code})
        card = card_map.get(code)
        source = source_map.get(card.source_id) if card else None
        chunk = chunk_map.get(card.chunk_id) if card else None
        declared_locations = {location for item_code, location in declared if item_code == code}
        valid = bool(card and source and chunk and set(locations) <= declared_locations)
        if not valid:
            warnings.append({
                "code": "INVALID_CITATION_REFERENCE",
                "message": f"引用{code}不存在、不属于当前报告或未在citations中声明。",
            })
            continue
        valid_codes.add(code)
        quote, quote_truncated = _bounded_quote(card.original_quote)
        limitations: list[str] = []
        if (source.evidence_grade or card.evidence_grade) == "partial_text":
            limitations.append("部分正文")
        if source.is_truncated:
            limitations.append("原始资料解析结果已截断")
        if source.used_ocr:
            limitations.append("使用OCR解析")
        if source.parse_warning:
            limitations.append("解析存在警告")
        details.append({
            "display_number": number,
            "evidence_code": code,
            "locations": locations,
            "source_name": source.name,
            "source_publisher": source.source_publisher,
            "source_origin": source.source_origin,
            "evidence_grade": source.evidence_grade or card.evidence_grade,
            "published_at": source.published_at,
            "retrieved_at": source.retrieved_at.isoformat() if source.retrieved_at else None,
            "url": _safe_url(source.url),
            "locator": card.locator or chunk.locator,
            "page_number": chunk.page_number,
            "slide_number": chunk.slide_number,
            "sheet_name": chunk.sheet_name,
            "cell_range": chunk.cell_range,
            "row_range": chunk.row_range,
            "paragraph_index": chunk.paragraph_index,
            "table_index": chunk.table_index,
            "table_row_index": chunk.table_row_index,
            "original_quote": quote,
            "quote_truncated": quote_truncated,
            "normalized_claim": card.normalized_claim,
            "claim_type": card.claim_type,
            "speaker": card.speaker,
            "limitations": limitations,
            "related_conflicts": sorted(
                conflicts_by_code.get(code, []), key=lambda item: item["conflict_code"]
            ),
        })

    context = {
        "report_id": report.id,
        "generation_mode": report.generation_mode or "legacy",
        "report": payload,
        "number_map": number_map,
        "valid_codes": valid_codes,
        "citations": sorted(details, key=lambda item: item["display_number"]),
        "warnings": warnings,
        "coverage": payload.get("evidence_coverage") or {},
        "limitations": [str(item) for item in payload.get("limitations") or []],
        "unresolved_conflicts": payload.get("unresolved_conflicts") or [],
        "metadata": payload.get("generation_metadata") or {},
    }
    if enforce_export_gate:
        _enforce_export_gate(db, report, context)
    return context


def _enforce_export_gate(db: Session, report: IndustryReport, context: dict[str, Any]) -> None:
    if report.generation_mode != "grounded" or report.citation_validation_status != "validated":
        raise CitationPresentationError(
            "GROUNDED_EXPORT_NOT_VALIDATED",
            "证据约束报告尚未通过正式引用验证，不能导出。",
            "validate and promote a grounded candidate",
        )
    if context["warnings"] or set(context["number_map"]) != context["valid_codes"]:
        raise CitationPresentationError(
            "GROUNDED_EXPORT_CITATION_INVALID",
            "报告包含失效、越权或无法定位的引用。",
            "regenerate and promote the report",
        )
    packet = build_evidence_packet(db, report.id)
    if (
        packet["evidence_snapshot_hash"] != report.evidence_snapshot_hash
        or packet["conflict_snapshot_hash"] != report.conflict_snapshot_hash
    ):
        raise CitationPresentationError(
            "GROUNDED_EXPORT_SNAPSHOT_STALE",
            "证据或冲突快照已经变化，已阻止导出。",
            "regenerate and promote the report",
        )
    validation = validate_citations(context["report"], packet)
    if not validation.valid:
        raise CitationPresentationError(
            "GROUNDED_EXPORT_VALIDATION_FAILED",
            "报告未通过导出前引用复核。",
            "regenerate and promote the report",
        )


def _replace_citations(text: str, context: dict[str, Any]) -> str:
    number_map = context["number_map"]
    valid_codes = context["valid_codes"]
    output: list[str] = []
    cursor = 0
    for match in CITATION_RE.finditer(text or ""):
        output.append(html.escape((text or "")[cursor:match.start()]))
        code = match.group(1)
        if code in valid_codes:
            number = number_map[code]
            output.append(
                f'<button type="button" class="citation-ref" data-evidence-code="{code}" '
                f'aria-label="查看引用 {number} 的证据详情">[{number}]</button>'
            )
        else:
            output.append(
                f'<span class="citation-invalid" title="引用无效或不可定位">[引用异常:{html.escape(code)}]</span>'
            )
        cursor = match.end()
    output.append(html.escape((text or "")[cursor:]))
    return "".join(output).replace("\n", "<br>")


def render_report_html(context: dict[str, Any]) -> str:
    report = context["report"]
    parts = [f'<article class="grounded-report"><h1>{html.escape(str(report.get("title") or "行业风险报告"))}</h1>']
    if report.get("summary"):
        parts.append(f'<section><h2>执行摘要</h2><p>{_replace_citations(str(report["summary"]), context)}</p></section>')
    for section in report.get("sections") or []:
        if isinstance(section, dict):
            parts.append(
                f'<section><h2>{html.escape(str(section.get("heading") or ""))}</h2>'
                f'<p>{_replace_citations(str(section.get("content") or ""), context)}</p></section>'
            )
    if report.get("risk_outlook"):
        parts.append(f'<section><h2>风险展望</h2><p>{_replace_citations(str(report["risk_outlook"]), context)}</p></section>')
    metrics = report.get("key_metrics") or []
    if metrics:
        parts.append('<section><h2>关键指标</h2><dl class="report-metrics">')
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            code = str(metric.get("evidence_code") or "")
            citation = (
                f'<button type="button" class="citation-ref" data-evidence-code="{html.escape(code)}" '
                f'aria-label="查看引用 {context["number_map"][code]} 的证据详情">[{context["number_map"][code]}]</button>'
                if code in context["valid_codes"] else '<span class="citation-invalid">[引用异常]</span>'
            )
            parts.append(
                f'<dt>{html.escape(str(metric.get("name") or ""))}</dt>'
                f'<dd>{html.escape(str(metric.get("value") or ""))}{citation}</dd>'
            )
        parts.append("</dl></section>")
    if context["warnings"]:
        parts.append('<aside class="citation-warning" role="alert">部分引用无法验证，请勿将其视为正式证据。</aside>')
    parts.append("</article>")
    return "".join(parts)


def citation_detail(context: dict[str, Any], evidence_code: str) -> dict[str, Any]:
    for item in context["citations"]:
        if item["evidence_code"] == evidence_code:
            return item
    raise CitationPresentationError(
        "CITATION_NOT_FOUND", "引用不存在、未被报告使用或不属于当前报告。", "refresh the report"
    )
