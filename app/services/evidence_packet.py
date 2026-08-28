"""Build a compact, auditable evidence packet for shadow report generation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.database.models import (
    IndustryConflictDetectionRun,
    IndustryDataSource,
    IndustryEvidenceCard,
    IndustryEvidenceConflict,
    IndustryReport,
    IndustrySourceChunk,
)
from app.services.conflict_detection import compute_evidence_snapshot_hash, sources_are_duplicates


REQUIRED_COVERAGE_TAGS = (
    "market_size", "market_growth", "business_model", "profitability",
    "policy_regulation", "competition_market_share", "financing_debt", "risk_event",
)


def _hash_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _risk_tags(card: IndustryEvidenceCard) -> list[str]:
    try:
        value = json.loads(card.risk_tags or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return sorted({str(item) for item in value if item}) if isinstance(value, list) else []


def _conflict_record(conflict: IndustryEvidenceConflict) -> dict[str, Any]:
    return {
        "conflict_code": conflict.conflict_code,
        "conflict_type": conflict.conflict_type,
        "severity": conflict.severity,
        "description": conflict.description,
        "resolution_status": conflict.resolution_status,
        "resolution_note": conflict.resolution_note,
        "selected_evidence_code": conflict.selected_evidence_code,
        "member_evidence_codes": sorted(member.evidence_code for member in conflict.members),
    }


def build_evidence_packet(db: Session, report_id: int) -> dict[str, Any]:
    report = db.get(IndustryReport, report_id)
    if not report:
        raise ValueError("report_not_found")

    cards = db.query(IndustryEvidenceCard).filter(
        IndustryEvidenceCard.report_id == report_id
    ).order_by(IndustryEvidenceCard.id).all()
    eligible: dict[str, tuple[IndustryEvidenceCard, IndustryDataSource, IndustrySourceChunk]] = {}
    excluded_counts: Counter[str] = Counter()
    network_lead_count = 0
    for card in cards:
        source = db.get(IndustryDataSource, card.source_id)
        chunk = db.get(IndustrySourceChunk, card.chunk_id)
        if not source or not source.is_selected:
            excluded_counts["not_selected"] += 1
            continue
        current_grade = getattr(source, "evidence_grade", None) or card.evidence_grade
        current_origin = getattr(source, "source_origin", None) or card.source_origin
        if card.validation_status == "lead_only" or current_grade == "lead_only" or current_origin == "network_search":
            network_lead_count += 1
        if card.validation_status != "verified":
            excluded_counts[card.validation_status or "unknown_status"] += 1
            continue
        if (
            card.requires_manual_review or card.claim_type == "inference"
            or current_grade == "lead_only" or current_origin == "network_search"
        ):
            excluded_counts["manual_or_restricted"] += 1
            continue
        if (
            not source or not chunk or source.report_id != report_id or chunk.report_id != report_id
            or chunk.source_id != card.source_id
        ):
            excluded_counts["report_or_source_mismatch"] += 1
            continue
        current_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        if chunk.content_hash != current_hash or card.chunk_content_hash != chunk.content_hash:
            excluded_counts["invalid_chunk_hash"] += 1
            continue
        eligible[card.evidence_code] = (card, source, chunk)

    latest_conflict_run = db.query(IndustryConflictDetectionRun).filter(
        IndustryConflictDetectionRun.report_id == report_id,
        IndustryConflictDetectionRun.status == "completed",
    ).order_by(IndustryConflictDetectionRun.id.desc()).first()
    source_map = {
        source.id: source for source in db.query(IndustryDataSource).filter(
            IndustryDataSource.report_id == report_id
        ).all()
    }
    chunk_hashes = {
        card.chunk_id: getattr(db.get(IndustrySourceChunk, card.chunk_id), "content_hash", None)
        for card in cards
    }
    current_conflict_snapshot = compute_evidence_snapshot_hash(cards, source_map, chunk_hashes)
    conflict_snapshot_stale = bool(
        latest_conflict_run
        and latest_conflict_run.evidence_snapshot_hash != current_conflict_snapshot
    )
    if conflict_snapshot_stale:
        latest_conflict_run = None
    conflicts = [] if latest_conflict_run is None else db.query(IndustryEvidenceConflict).filter(
        IndustryEvidenceConflict.report_id == report_id,
        IndustryEvidenceConflict.detection_run_id == latest_conflict_run.id,
    ).order_by(IndustryEvidenceConflict.id).all()

    resolved_conflicts: list[dict[str, Any]] = []
    unresolved_conflicts: list[dict[str, Any]] = []
    blocked_codes: set[str] = set()
    disclosed_codes: set[str] = set()
    selected_codes: set[str] = set()
    invalid_selections: list[str] = []
    for conflict in conflicts:
        if conflict.resolution_status == "superseded":
            continue
        record = _conflict_record(conflict)
        member_codes = set(record["member_evidence_codes"])
        if conflict.resolution_status == "resolved_selected":
            selected = conflict.selected_evidence_code
            if selected and selected in member_codes and selected in eligible:
                selected_codes.add(selected)
                blocked_codes.update(member_codes - {selected})
                resolved_conflicts.append(record)
            else:
                record["selection_error"] = "selected_evidence_is_not_current_verified_member"
                invalid_selections.append(conflict.conflict_code)
                blocked_codes.update(member_codes)
                unresolved_conflicts.append(record)
        elif conflict.resolution_status == "resolved_disclosed":
            disclosed_codes.update(member_codes)
            resolved_conflicts.append(record)
        elif conflict.resolution_status == "not_a_conflict":
            resolved_conflicts.append(record)
        elif conflict.resolution_status in {"open", "needs_review"}:
            blocked_codes.update(member_codes)
            unresolved_conflicts.append(record)

    limitations: list[str] = []
    if latest_conflict_run is None:
        limitations.append(
            "当前证据快照对应的跨信源冲突检测已过期。"
            if conflict_snapshot_stale else "尚未完成当前报告的跨信源冲突检测。"
        )
    if invalid_selections:
        limitations.append("存在失效的人工选定证据：" + "、".join(invalid_selections))
    for conflict in unresolved_conflicts:
        if conflict["severity"] in {"high", "critical"}:
            limitations.append(
                f"{conflict['severity']}级未解决冲突{conflict['conflict_code']}：{conflict['description']}"
            )

    evidence: list[dict[str, Any]] = []
    independent_sources: list[IndustryDataSource] = []
    tag_counts: Counter[str] = Counter()
    partial_count = 0
    for code, (card, source, _chunk) in eligible.items():
        if code in blocked_codes and code not in disclosed_codes and code not in selected_codes:
            usage_policy = "conflicted_do_not_select"
        elif code in disclosed_codes:
            usage_policy = "disclose_conflict_only"
        elif code in selected_codes:
            usage_policy = "selected_value"
        else:
            usage_policy = "usable"
        # Unselected members of resolved_selected conflicts are not exposed to the model.
        if code in blocked_codes and usage_policy == "conflicted_do_not_select" and any(
            item.get("resolution_status") == "resolved_selected"
            and code in item.get("member_evidence_codes", [])
            for item in resolved_conflicts
        ):
            continue
        tags = _risk_tags(card)
        tag_counts.update(tags)
        if not any(sources_are_duplicates(source, prior) for prior in independent_sources):
            independent_sources.append(source)
        current_grade = source.evidence_grade or card.evidence_grade
        if current_grade == "partial_text" or source.is_truncated:
            partial_count += 1
            limitations.append(f"证据{code}来自部分正文或截断资料，不代表已审阅完整文件。")
        evidence.append({
            "evidence_code": code,
            "normalized_claim": card.normalized_claim,
            "original_quote": card.original_quote,
            "claim_type": card.claim_type,
            "subject": card.subject,
            "metric_name": card.metric_name,
            "raw_value": card.raw_value,
            "normalized_value": card.normalized_value,
            "unit": card.unit,
            "currency": card.currency,
            "period": card.period,
            "as_of_date": card.as_of_date,
            "speaker": card.speaker,
            "risk_tags": tags,
            "importance_score": card.importance_score,
            "source_origin": source.source_origin or card.source_origin,
            "evidence_grade": current_grade,
            "source_name": source.name,
            "locator": card.locator,
            "chunk_content_hash": card.chunk_content_hash,
            "usage_policy": usage_policy,
        })

    missing_topics = [tag for tag in REQUIRED_COVERAGE_TAGS if tag_counts[tag] == 0]
    missing_information = [f"缺少{tag}主题的可用verified证据。" for tag in missing_topics]
    if not evidence:
        missing_information.append("当前报告没有可用于影子报告生成的verified证据。")
    if network_lead_count and not evidence:
        limitations.append("当前仅有网络线索，没有可用于确定性事实陈述的正式证据。")

    coverage = {
        "verified_evidence_count": len(evidence),
        "independent_source_count": len(independent_sources),
        "partial_text_count": partial_count,
        "unresolved_conflict_count": len(unresolved_conflicts),
        "resolved_conflict_count": len(resolved_conflicts),
        "network_lead_count": network_lead_count,
        "excluded_evidence_count": sum(excluded_counts.values()),
        "excluded_by_reason": dict(sorted(excluded_counts.items())),
        "risk_tag_counts": dict(sorted(tag_counts.items())),
        "missing_topic_count": len(missing_topics),
        "missing_topics": missing_topics,
        "conflict_snapshot_stale": conflict_snapshot_stale,
    }
    report_context = {
        "industry_name": report.industry_name,
        "company_name": report.company_name,
        "report_version": report.version,
    }
    evidence_snapshot_payload = {
        "report_context": report_context,
        "evidence": evidence,
        "missing_information": missing_information,
        "coverage": coverage,
    }
    conflict_snapshot_payload = [
        _conflict_record(conflict) for conflict in conflicts if conflict.resolution_status != "superseded"
    ]
    return {
        "report_id": report_id,
        "report_context": report_context,
        "evidence_snapshot_hash": _hash_json(evidence_snapshot_payload),
        "conflict_snapshot_hash": _hash_json({
            "detection_run": (
                {
                    "id": latest_conflict_run.id,
                    "snapshot": latest_conflict_run.evidence_snapshot_hash,
                    "detector_version": latest_conflict_run.detector_version,
                }
                if latest_conflict_run else None
            ),
            "conflicts": conflict_snapshot_payload,
            "limitations": limitations,
        }),
        "evidence": evidence,
        "resolved_conflicts": resolved_conflicts,
        "unresolved_conflicts": unresolved_conflicts,
        "limitations": sorted(set(limitations)),
        "missing_information": missing_information,
        "coverage": coverage,
    }
