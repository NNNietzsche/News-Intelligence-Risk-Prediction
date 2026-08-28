"""Deterministic cross-source evidence conflict detection."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.database.models import (
    IndustryConflictDetectionRun,
    IndustryDataSource,
    IndustryEvidenceCard,
    IndustryEvidenceConflict,
    IndustryEvidenceConflictMember,
    IndustryReport,
    IndustrySourceChunk,
)
from app.services.evidence_normalization import NormalizedEvidence, decimal_string, normalize_evidence


DETECTOR_VERSION = "conflict-v1"
RESOLUTION_STATUSES = {
    "open", "needs_review", "resolved_disclosed", "resolved_selected",
    "not_a_conflict", "superseded",
}
CORE_RISK_TAGS = {"financing_debt", "safety_accident", "legal_litigation", "risk_event", "profitability"}


class ConflictDetectionError(RuntimeError):
    pass


def _stable_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), parsed.query, ""))


def source_identity(source: IndustryDataSource) -> str:
    if source.raw_content_hash:
        return f"raw:{source.raw_content_hash}"
    if source.extracted_text_hash:
        return f"text:{source.extracted_text_hash}"
    stable_url = _stable_url(source.url)
    if stable_url:
        return f"url:{stable_url}"
    return f"source:{source.id}"


def sources_are_duplicates(left: IndustryDataSource, right: IndustryDataSource) -> bool:
    if left.raw_content_hash and left.raw_content_hash == right.raw_content_hash:
        return True
    if left.extracted_text_hash and left.extracted_text_hash == right.extracted_text_hash:
        return True
    left_url, right_url = _stable_url(left.url), _stable_url(right.url)
    if not left_url or left_url != right_url:
        return False
    if left.source_origin == "network_search" or right.source_origin == "network_search":
        return True
    return bool(
        left.published_at and right.published_at and left.published_at == right.published_at
        and left.name.strip().casefold() == right.name.strip().casefold()
    )


def sources_are_versions(left: IndustryDataSource, right: IndustryDataSource) -> bool:
    if left.copied_from_source_id == right.id or right.copied_from_source_id == left.id:
        return True
    left_url, right_url = _stable_url(left.url), _stable_url(right.url)
    if not left_url or left_url != right_url or sources_are_duplicates(left, right):
        return False
    if left.published_at and right.published_at:
        return left.published_at != right.published_at
    return bool(left.retrieved_at and right.retrieved_at and left.retrieved_at != right.retrieved_at)


def compute_evidence_snapshot_hash(
    cards: Iterable[IndustryEvidenceCard],
    sources: Optional[dict[int, IndustryDataSource]] = None,
    current_chunk_hashes: Optional[dict[int, Optional[str]]] = None,
) -> str:
    payload = [
        {
            "id": card.id, "status": card.validation_status,
            "chunk_hash": card.chunk_content_hash, "value": card.normalized_value,
            "unit": card.unit, "currency": card.currency, "period": card.period,
            "subject": card.subject, "metric": card.metric_name, "claim_type": card.claim_type,
            "manual": card.requires_manual_review,
            "current_chunk_hash": (current_chunk_hashes or {}).get(card.chunk_id),
            "source_raw_hash": getattr((sources or {}).get(card.source_id), "raw_content_hash", None),
            "source_text_hash": getattr((sources or {}).get(card.source_id), "extracted_text_hash", None),
            "source_url": _stable_url(getattr((sources or {}).get(card.source_id), "url", None)),
            "source_ocr": getattr((sources or {}).get(card.source_id), "used_ocr", None),
            "source_grade": getattr((sources or {}).get(card.source_id), "evidence_grade", None),
            "source_truncated": getattr((sources or {}).get(card.source_id), "is_truncated", None),
            "source_published": getattr((sources or {}).get(card.source_id), "published_at", None),
            "source_retrieved": str(getattr((sources or {}).get(card.source_id), "retrieved_at", None)),
        }
        for card in sorted(cards, key=lambda item: item.id)
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _relative_difference(values: list[Decimal]) -> Decimal:
    high, low = max(values), min(values)
    denominator = max(abs(high), abs(low))
    return Decimal("0") if denominator == 0 else abs(high - low) / denominator


def _severity(items: list[NormalizedEvidence], conflict_type: str) -> str:
    if conflict_type in {
        "duplicate_source", "period_mismatch", "unit_mismatch", "currency_mismatch",
        "possible_rounding_difference", "uncomparable",
    }:
        return "low"
    scores = [item.card.importance_score for item in items]
    tags = set()
    for item in items:
        try:
            tags.update(json.loads(item.card.risk_tags or "[]"))
        except (TypeError, json.JSONDecodeError):
            pass
    if tags & CORE_RISK_TAGS and max(scores, default=0) >= 4:
        return "critical" if max(scores) >= 5 else "high"
    if max(scores, default=0) >= 5:
        return "high"
    if max(scores, default=0) >= 4:
        return "medium"
    return "low"


def conflict_to_dict(conflict: IndustryEvidenceConflict, include_members: bool = False) -> dict:
    result = {column.name: getattr(conflict, column.name) for column in conflict.__table__.columns}
    if include_members:
        result["members"] = [
            {column.name: getattr(member, column.name) for column in member.__table__.columns}
            for member in sorted(conflict.members, key=lambda item: item.id)
        ]
    return result


def conflict_run_to_dict(run: IndustryConflictDetectionRun) -> dict:
    return {column.name: getattr(run, column.name) for column in run.__table__.columns}


class ConflictDetectionService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _report(self, report_id: int) -> IndustryReport:
        report = self.db.get(IndustryReport, report_id)
        if not report:
            raise ValueError("report_not_found")
        return report

    def _normalize_cards(self, report_id: int) -> tuple[list[IndustryEvidenceCard], list[NormalizedEvidence], int]:
        cards = self.db.query(IndustryEvidenceCard).filter(
            IndustryEvidenceCard.report_id == report_id
        ).order_by(IndustryEvidenceCard.id).all()
        normalized: list[NormalizedEvidence] = []
        excluded = 0
        for card in cards:
            if card.validation_status in {"rejected", "stale"} or card.claim_type == "inference":
                excluded += 1
                continue
            source = self.db.get(IndustryDataSource, card.source_id)
            chunk = self.db.get(IndustrySourceChunk, card.chunk_id)
            if not source or not chunk or source.report_id != report_id or chunk.report_id != report_id:
                excluded += 1
                continue
            actual_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            if chunk.content_hash != card.chunk_content_hash or chunk.content_hash != actual_hash:
                excluded += 1
                continue
            item = normalize_evidence(card, source)
            if item is None:
                excluded += 1
                continue
            normalized.append(item)
        return cards, normalized, excluded

    def detect(self, report_id: int) -> IndustryConflictDetectionRun:
        self._report(report_id)
        cards, items, excluded = self._normalize_cards(report_id)
        source_map = {
            source.id: source for source in self.db.query(IndustryDataSource).filter(
                IndustryDataSource.report_id == report_id
            ).all()
        }
        chunk_hashes = {
            card.chunk_id: getattr(self.db.get(IndustrySourceChunk, card.chunk_id), "content_hash", None)
            for card in cards
        }
        snapshot = compute_evidence_snapshot_hash(cards, source_map, chunk_hashes)
        prior = self.db.query(IndustryConflictDetectionRun).filter(
            IndustryConflictDetectionRun.report_id == report_id,
            IndustryConflictDetectionRun.evidence_snapshot_hash == snapshot,
            IndustryConflictDetectionRun.detector_version == DETECTOR_VERSION,
            IndustryConflictDetectionRun.status == "completed",
        ).order_by(IndustryConflictDetectionRun.id.desc()).first()
        if prior:
            return prior
        run = IndustryConflictDetectionRun(
            report_id=report_id, evidence_snapshot_hash=snapshot, detector_version=DETECTOR_VERSION,
            status="running", eligible_evidence_count=len(items), excluded_count=excluded,
            started_at=datetime.utcnow(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        try:
            proposals, group_count = self._build_proposals(items)
            run.compared_group_count = group_count
            next_code = self._next_conflict_number(report_id)
            for proposal in proposals:
                conflict = self._save_conflict(run, next_code, **proposal)
                next_code += 1
                if conflict.resolution_status == "needs_review":
                    run.review_count += 1
                else:
                    run.conflict_count += 1
            previous = self.db.query(IndustryEvidenceConflict).filter(
                IndustryEvidenceConflict.report_id == report_id,
                IndustryEvidenceConflict.detection_run_id != run.id,
                IndustryEvidenceConflict.resolution_status.in_(("open", "needs_review")),
            ).all()
            for conflict in previous:
                conflict.resolution_status = "superseded"
            run.status = "completed"
            run.completed_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self.db.rollback()
            failed = self.db.get(IndustryConflictDetectionRun, run.id)
            if failed:
                failed.status = "failed"
                failed.error_message = "确定性冲突检测失败"
                failed.completed_at = datetime.utcnow()
                self.db.commit()
            raise ConflictDetectionError("冲突检测失败，未写入不完整结果") from exc

    def _build_proposals(self, items: list[NormalizedEvidence]) -> tuple[list[dict], int]:
        proposals: list[dict] = []
        proposal_keys: set[str] = set()
        groups: dict[tuple, list[NormalizedEvidence]] = defaultdict(list)
        for item in items:
            groups[item.strict_group_key].append(item)

        def add(conflict_type: str, members: list[NormalizedEvidence], description: str, review: bool = False):
            distinct = sorted({item.card.id for item in members})
            if len(distinct) < 2:
                return
            key_raw = json.dumps([conflict_type, distinct], separators=(",", ":"))
            key = hashlib.sha256(key_raw.encode()).hexdigest()
            if key in proposal_keys:
                return
            proposal_keys.add(key)
            proposals.append({
                "conflict_type": conflict_type, "items": members, "description": description,
                "resolution_status": "needs_review" if review else "open", "dedupe_key": key,
            })

        compared_groups = 0
        for group_items in groups.values():
            if len(group_items) < 2:
                continue
            compared_groups += 1
            duplicate_groups: list[list[NormalizedEvidence]] = []
            for item in group_items:
                matching = [
                    index for index, group in enumerate(duplicate_groups)
                    if any(sources_are_duplicates(existing.source, item.source) for existing in group)
                ]
                if not matching:
                    duplicate_groups.append([item])
                else:
                    merged = [item]
                    for group_index in reversed(matching):
                        merged.extend(duplicate_groups.pop(group_index))
                    duplicate_groups.append(merged)
            for duplicates in duplicate_groups:
                if len({item.source.id for item in duplicates}) >= 2:
                    add("duplicate_source", duplicates, "多条证据来自相同原始或解析内容，不计为独立信源。", True)

            def independent(subset: list[NormalizedEvidence]) -> list[NormalizedEvidence]:
                selected: list[NormalizedEvidence] = []
                for candidate in sorted(subset, key=lambda item: item.card.id):
                    if not any(sources_are_duplicates(candidate.source, prior.source) for prior in selected):
                        selected.append(candidate)
                return selected

            strict_items = independent([item for item in group_items if not item.restricted])
            if len(strict_items) >= 2:
                values = [item.comparison_value for item in strict_items]
                if len(set(values)) != 1 or any(item.approximate for item in strict_items):
                    versioned = any(
                        sources_are_versions(left.source, right.source)
                        for index, left in enumerate(strict_items) for right in strict_items[index + 1:]
                    )
                    if versioned:
                        conflict_type, review = "source_version_mismatch", True
                    elif any(item.approximate for item in strict_items) or _relative_difference(values) <= Decimal("0.005"):
                        conflict_type, review = "possible_rounding_difference", True
                    else:
                        conflict_type, review = "numeric_mismatch", False
                    add(conflict_type, strict_items, "同一主体、指标、期间和口径的独立完整信源值存在差异。", review)

            restricted_items = [item for item in group_items if item.restricted]
            restricted_comparison = independent(strict_items + restricted_items)
            if restricted_items and len(restricted_comparison) >= 2:
                values = [item.comparison_value for item in restricted_comparison]
                if len(set(values)) != 1:
                    add(
                        "restricted_source_discrepancy", restricted_comparison,
                        "受限证据与其他独立信源的同口径数值存在差异，仅供人工复核。", True,
                    )

        # Conservative pair checks for dimensions that make values non-comparable.
        for index, left in enumerate(items):
            for right in items[index + 1:]:
                if left.source.id == right.source.id or sources_are_duplicates(left.source, right.source):
                    continue
                if left.subject_key != right.subject_key or left.metric_key != right.metric_key:
                    continue
                common_period = left.period_key == right.period_key
                common_category = left.claim_category == right.claim_category
                common_currency = left.currency_key == right.currency_key
                common_dimension = left.dimension_key == right.dimension_key
                if common_period and common_category and common_currency and not common_dimension:
                    add("unit_mismatch", [left, right], "单位维度不兼容，不能直接比较。", True)
                elif common_period and common_category and common_dimension and not common_currency:
                    add("currency_mismatch", [left, right], "币种不同且未进行汇率换算，不能直接比较。", True)
                elif common_period and common_currency and common_dimension and not common_category:
                    categories = {left.claim_category, right.claim_category}
                    if "actual" in categories and categories & {"forecast", "target"}:
                        add("actual_forecast_mismatch", [left, right], "实际值与预测或目标值属于不同事实类别。", True)
                elif common_category and common_currency and common_dimension and not common_period:
                    left_year = re.search(r"\d{4}", left.period_key)
                    right_year = re.search(r"\d{4}", right.period_key)
                    if left_year and right_year and left_year.group() == right_year.group():
                        add("period_mismatch", [left, right], "期间表达不同，无法自动认定为同一口径。", True)
        return proposals, compared_groups

    def _save_conflict(
        self, run: IndustryConflictDetectionRun, number: int, conflict_type: str,
        items: list[NormalizedEvidence], description: str, resolution_status: str,
        dedupe_key: str,
    ) -> IndustryEvidenceConflict:
        first = items[0]
        conflict = IndustryEvidenceConflict(
            conflict_code=f"C{number:06d}", report_id=run.report_id, detection_run_id=run.id,
            conflict_type=conflict_type, severity=_severity(items, conflict_type),
            subject_key=first.subject_key, metric_key=first.metric_key,
            period_key=first.period_key if all(x.period_key == first.period_key for x in items) else None,
            currency_key=first.currency_key if all(x.currency_key == first.currency_key for x in items) else None,
            dimension_key=first.dimension_key if all(x.dimension_key == first.dimension_key for x in items) else "other",
            base_unit=first.base_unit if all(x.base_unit == first.base_unit for x in items) else None,
            description=description, resolution_status=resolution_status,
            requires_manual_review=True, dedupe_key=dedupe_key,
        )
        self.db.add(conflict)
        self.db.flush()
        for item in items:
            self.db.add(IndustryEvidenceConflictMember(
                conflict_id=conflict.id, evidence_card_id=item.card.id,
                evidence_code=item.card.evidence_code, source_id=item.source.id,
                comparison_value=decimal_string(item.comparison_value),
                comparison_unit=item.base_unit, source_origin=item.source.source_origin,
                evidence_grade=item.card.evidence_grade,
                validation_status=item.card.validation_status,
                member_role="restricted" if item.restricted else "compared",
            ))
        return conflict

    def _next_conflict_number(self, report_id: int) -> int:
        codes = self.db.query(IndustryEvidenceConflict.conflict_code).filter(
            IndustryEvidenceConflict.report_id == report_id
        ).all()
        values = [int(code[1:]) for (code,) in codes if re.fullmatch(r"C\d+", code or "")]
        return max(values, default=0) + 1

    def list_conflicts(
        self, report_id: int, conflict_type: Optional[str] = None,
        severity: Optional[str] = None, resolution_status: Optional[str] = None,
        source_id: Optional[int] = None, evidence_code: Optional[str] = None,
    ) -> list[IndustryEvidenceConflict]:
        self._report(report_id)
        query = self.db.query(IndustryEvidenceConflict).filter(
            IndustryEvidenceConflict.report_id == report_id
        )
        if conflict_type:
            query = query.filter(IndustryEvidenceConflict.conflict_type == conflict_type)
        if severity:
            query = query.filter(IndustryEvidenceConflict.severity == severity)
        if resolution_status:
            query = query.filter(IndustryEvidenceConflict.resolution_status == resolution_status)
        if source_id is not None or evidence_code:
            query = query.join(IndustryEvidenceConflictMember)
            if source_id is not None:
                query = query.filter(IndustryEvidenceConflictMember.source_id == source_id)
            if evidence_code:
                query = query.filter(IndustryEvidenceConflictMember.evidence_code == evidence_code)
        return query.distinct().order_by(IndustryEvidenceConflict.id).all()

    def get_conflict(self, report_id: int, conflict_code: str) -> Optional[IndustryEvidenceConflict]:
        self._report(report_id)
        return self.db.query(IndustryEvidenceConflict).filter(
            IndustryEvidenceConflict.report_id == report_id,
            IndustryEvidenceConflict.conflict_code == conflict_code,
        ).first()

    def resolve(
        self, report_id: int, conflict_code: str, resolution_status: str,
        resolution_note: str, selected_evidence_code: Optional[str] = None,
    ) -> IndustryEvidenceConflict:
        conflict = self.get_conflict(report_id, conflict_code)
        if not conflict:
            raise ValueError("conflict_not_found")
        if resolution_status not in RESOLUTION_STATUSES - {"open", "needs_review", "superseded"}:
            raise ValueError("invalid_resolution_status")
        note = resolution_note.strip()
        if not note:
            raise ValueError("resolution_note_required")
        member_codes = {member.evidence_code for member in conflict.members}
        if resolution_status == "resolved_selected":
            if not selected_evidence_code or selected_evidence_code not in member_codes:
                raise ValueError("selected_evidence_must_be_conflict_member")
        elif selected_evidence_code is not None:
            raise ValueError("selected_evidence_only_for_resolved_selected")
        conflict.resolution_status = resolution_status
        conflict.resolution_note = note
        conflict.selected_evidence_code = selected_evidence_code
        conflict.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(conflict)
        return conflict
