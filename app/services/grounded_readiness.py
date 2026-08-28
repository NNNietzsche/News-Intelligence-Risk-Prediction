"""Read-only preflight checks for evidence-grounded formal report generation."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.database.models import (
    IndustryConflictDetectionRun,
    IndustryDataSource,
    IndustryEvidenceCard,
    IndustryEvidenceExtractionRun,
    IndustryReport,
    IndustrySourceChunk,
)
from app.services.conflict_detection import DETECTOR_VERSION, compute_evidence_snapshot_hash
from app.services.deepseek_analyzer import EVIDENCE_EXTRACTION_PROMPT_VERSION
from app.services.evidence_cards import compute_source_snapshot_hash
from app.services.evidence_packet import build_evidence_packet


def _issue(code: str, message: str, next_step: str) -> dict[str, str]:
    return {"code": code, "message": message, "next_step": next_step}


def check_grounded_readiness(
    db: Session, report_id: int, *, require_generatable_status: bool = True,
) -> dict[str, Any]:
    report = db.get(IndustryReport, report_id)
    if not report:
        raise ValueError("report_not_found")

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if require_generatable_status and report.status not in {"draft", "failed"}:
        errors.append(_issue(
            "REPORT_NOT_GENERATABLE",
            "只有草稿或生成失败的报告可以开始grounded生成；已完成报告请先创建新版。",
            "create report revision",
        ))

    sources = db.query(IndustryDataSource).filter(
        IndustryDataSource.report_id == report_id
    ).order_by(IndustryDataSource.id).all()
    chunks = db.query(IndustrySourceChunk).filter(
        IndustrySourceChunk.report_id == report_id
    ).order_by(IndustrySourceChunk.source_id, IndustrySourceChunk.chunk_index).all()
    chunk_source_ids = {chunk.source_id for chunk in chunks}
    structured_sources = [source for source in sources if source.id in chunk_source_ids]
    if not structured_sources:
        errors.append(_issue(
            "NO_STRUCTURED_SOURCE", "当前报告没有带定位切片的结构化信源。", "upload or parse sources",
        ))
    elif len(structured_sources) != len(sources):
        errors.append(_issue(
            "SOURCE_CHUNK_INVALID", "部分当前信源尚未形成定位切片。", "reparse sources",
        ))

    source_ids = {source.id for source in sources}
    invalid_chunks = [
        chunk for chunk in chunks
        if chunk.source_id not in source_ids
        or hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() != chunk.content_hash
    ]
    if invalid_chunks:
        errors.append(_issue(
            "SOURCE_CHUNK_INVALID", "存在无主、跨报告或文本哈希失效的信源切片。", "reparse sources",
        ))

    source_snapshot = compute_source_snapshot_hash(sources, chunks)
    extraction_run = db.query(IndustryEvidenceExtractionRun).filter(
        IndustryEvidenceExtractionRun.report_id == report_id,
        IndustryEvidenceExtractionRun.source_id_scope.is_(None),
        IndustryEvidenceExtractionRun.status == "completed",
        IndustryEvidenceExtractionRun.prompt_version == EVIDENCE_EXTRACTION_PROMPT_VERSION,
        IndustryEvidenceExtractionRun.source_snapshot_hash == source_snapshot,
    ).order_by(IndustryEvidenceExtractionRun.id.desc()).first()
    if not extraction_run:
        had_completed = db.query(IndustryEvidenceExtractionRun.id).filter(
            IndustryEvidenceExtractionRun.report_id == report_id,
            IndustryEvidenceExtractionRun.status == "completed",
        ).first()
        errors.append(_issue(
            "EVIDENCE_SNAPSHOT_STALE" if had_completed else "EVIDENCE_EXTRACTION_REQUIRED",
            "当前信源快照没有对应的完整证据提取结果。",
            "evidence extraction",
        ))

    cards = db.query(IndustryEvidenceCard).filter(
        IndustryEvidenceCard.report_id == report_id
    ).order_by(IndustryEvidenceCard.id).all()
    source_map = {source.id: source for source in sources}
    chunk_map = {chunk.id: chunk for chunk in chunks}
    chunk_hashes = {card.chunk_id: getattr(chunk_map.get(card.chunk_id), "content_hash", None) for card in cards}
    invalid_card_bindings = [
        card.evidence_code for card in cards
        if not chunk_map.get(card.chunk_id)
        or card.source_id not in source_map
        or chunk_map[card.chunk_id].source_id != card.source_id
    ]
    if invalid_card_bindings:
        errors.append(_issue(
            "SOURCE_CHUNK_INVALID",
            "证据卡与信源切片绑定不完整：" + "、".join(sorted(invalid_card_bindings)),
            "evidence extraction",
        ))
    conflict_evidence_snapshot = compute_evidence_snapshot_hash(cards, source_map, chunk_hashes)
    conflict_run = db.query(IndustryConflictDetectionRun).filter(
        IndustryConflictDetectionRun.report_id == report_id,
        IndustryConflictDetectionRun.status == "completed",
        IndustryConflictDetectionRun.detector_version == DETECTOR_VERSION,
        IndustryConflictDetectionRun.evidence_snapshot_hash == conflict_evidence_snapshot,
    ).order_by(IndustryConflictDetectionRun.id.desc()).first()
    if not conflict_run:
        had_completed = db.query(IndustryConflictDetectionRun.id).filter(
            IndustryConflictDetectionRun.report_id == report_id,
            IndustryConflictDetectionRun.status == "completed",
        ).first()
        errors.append(_issue(
            "CONFLICT_SNAPSHOT_STALE" if had_completed else "CONFLICT_DETECTION_REQUIRED",
            "当前证据快照没有对应的冲突检测结果。",
            "conflict detection",
        ))

    packet = build_evidence_packet(db, report_id)
    eligible_codes = {item["evidence_code"] for item in packet["evidence"]}
    if not eligible_codes:
        errors.append(_issue(
            "NO_VERIFIED_EVIDENCE", "当前报告没有可用于正式生成的verified证据。", "evidence extraction",
        ))

    invalid_critical = []
    for card in cards:
        if card.importance_score < 4:
            continue
        chunk = chunk_map.get(card.chunk_id)
        structurally_current = bool(
            chunk and chunk.source_id == card.source_id
            and chunk.content_hash == card.chunk_content_hash
            and hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() == chunk.content_hash
        )
        if card.validation_status == "stale" or (card.validation_status == "verified" and not structurally_current):
            invalid_critical.append(card.evidence_code)
    if invalid_critical:
        errors.append(_issue(
            "CRITICAL_EVIDENCE_INVALID",
            "存在失效的高重要度证据：" + "、".join(sorted(invalid_critical)),
            "evidence extraction",
        ))

    invalid_selections = [
        item["conflict_code"] for item in packet["unresolved_conflicts"] if item.get("selection_error")
    ]
    if invalid_selections:
        errors.append(_issue(
            "UNRESOLVED_BLOCKING_CONFLICT",
            "人工选定证据已经失效：" + "、".join(invalid_selections),
            "conflict resolution",
        ))

    limitation_text = " ".join(packet["limitations"])
    missing_material_conflicts = [
        item["conflict_code"] for item in packet["unresolved_conflicts"]
        if item.get("severity") in {"high", "critical"} and item["conflict_code"] not in limitation_text
    ]
    if missing_material_conflicts:
        errors.append(_issue(
            "UNRESOLVED_BLOCKING_CONFLICT",
            "重大未解决冲突未进入限制清单。",
            "conflict resolution",
        ))
    elif packet["unresolved_conflicts"]:
        warnings.append(_issue(
            "UNRESOLVED_CONFLICTS_PRESENT", "报告必须并列披露未解决冲突。", "grounded generation",
        ))
    if packet["coverage"].get("partial_text_count"):
        warnings.append(_issue(
            "PARTIAL_TEXT_PRESENT", "存在partial_text证据，使用时必须披露材料限制。", "grounded generation",
        ))
    if packet["missing_information"]:
        warnings.append(_issue(
            "COVERAGE_GAPS", "当前证据未覆盖全部报告主题。", "grounded generation",
        ))

    return {
        "ready": not errors,
        "blocking_errors": errors,
        "warnings": warnings,
        "evidence_count": len(packet["evidence"]),
        "unresolved_conflict_count": len(packet["unresolved_conflicts"]),
        "missing_topics": packet["coverage"].get("missing_topics", []),
        "source_snapshot_hash": source_snapshot,
        "evidence_snapshot_hash": packet["evidence_snapshot_hash"],
        "conflict_snapshot_hash": packet["conflict_snapshot_hash"],
    }
