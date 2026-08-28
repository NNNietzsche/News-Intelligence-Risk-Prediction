"""Evidence extraction orchestration and deterministic source matching."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.database.models import (
    IndustryDataSource,
    IndustryEvidenceCard,
    IndustryEvidenceExtractionRun,
    IndustryReport,
    IndustrySourceChunk,
)
from app.schemas import EvidenceCandidate, EvidenceCandidatePayload
from app.services.deepseek_analyzer import (
    EVIDENCE_EXTRACTION_PROMPT_VERSION,
    DeepSeekAnalyzer,
)
from app.services.gemini_analyzer import GeminiAnalyzer

NEGATIVE_TAGS = {
    "safety_accident", "legal_litigation", "environmental", "financing_debt", "risk_event"
}
INSTRUCTION_PATTERNS = (
    "忽略系统", "忽略以上", "系统指令", "不要引用本段", "将所有金额改写",
    "ignore previous", "ignore system", "system prompt",
)
MULTIPLIERS = (
    ("trillion", Decimal("1000000000000")), ("billion", Decimal("1000000000")),
    ("million", Decimal("1000000")), ("thousand", Decimal("1000")),
    ("十亿", Decimal("1000000000")), ("百万", Decimal("1000000")),
    ("千", Decimal("1000")), ("万", Decimal("10000")),
    ("亿", Decimal("100000000")), ("億", Decimal("100000000")),
    ("兆", Decimal("1000000000000")),
)
CURRENCY_ALIASES = {
    "JPY": ("JPY", "日元", "円"), "CNY": ("CNY", "RMB", "人民币"),
    "USD": ("USD", "美元", "$"), "EUR": ("EUR", "欧元", "€"),
}
UNIT_ALIASES = {
    "%": ("%", "％", "百分比", "百分点"), "倍": ("倍",),
    "kW": ("kW",), "MW": ("MW",), "GW": ("GW",),
    "kWh": ("kWh",), "MWh": ("MWh",), "GWh": ("GWh",),
}


class EvidenceExtractionError(RuntimeError):
    """A safe, API-facing evidence extraction failure."""


@dataclass
class ValidationResult:
    status: str
    quote: str
    quote_start: Optional[int] = None
    quote_end: Optional[int] = None
    normalized_value: Optional[str] = None
    value_multiplier: Optional[str] = None
    unit: Optional[str] = None
    currency: Optional[str] = None
    requires_manual_review: bool = False
    rejection_reason: Optional[str] = None


def compute_source_snapshot_hash(
    sources: Iterable[IndustryDataSource], chunks: Iterable[IndustrySourceChunk]
) -> str:
    """Stable hash over source identity/text hash and ordered chunk identity/hash."""
    source_rows = sorted(sources, key=lambda item: item.id)
    chunk_rows = sorted(chunks, key=lambda item: (item.source_id, item.chunk_index, item.id))
    payload = {
        "sources": [
            {
                "id": row.id,
                "text_hash": row.extracted_text_hash
                or hashlib.sha256((row.extracted_text or "").encode("utf-8")).hexdigest(),
                "grade": row.evidence_grade,
            }
            for row in source_rows
        ],
        "chunks": [
            {"id": row.id, "source_id": row.source_id, "index": row.chunk_index, "hash": row.content_hash}
            for row in chunk_rows
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalized_with_positions(value: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    positions: list[int] = []
    previous_space = False
    for index, char in enumerate(value):
        for normalized in unicodedata.normalize("NFKC", char):
            if normalized.isspace():
                if previous_space:
                    continue
                normalized = " "
                previous_space = True
            else:
                previous_space = False
            chars.append(normalized)
            positions.append(index)
    return "".join(chars), positions


def locate_quote(text: str, quote: str) -> tuple[list[tuple[int, int]], bool]:
    """Return real chunk offsets; normalized matches never save model-rewritten text."""
    exact = [(m.start(), m.end()) for m in re.finditer(re.escape(quote), text)]
    if exact:
        return exact, True
    normalized_text, mapping = _normalized_with_positions(text)
    normalized_quote, _ = _normalized_with_positions(quote)
    if not normalized_quote:
        return [], False
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        found = normalized_text.find(normalized_quote, start)
        if found < 0:
            break
        matches.append((mapping[found], mapping[found + len(normalized_quote) - 1] + 1))
        start = found + 1
    return matches, False


def _canonical_requested(value: Optional[str], aliases: dict[str, tuple[str, ...]]) -> Optional[str]:
    if value is None:
        return None
    folded = unicodedata.normalize("NFKC", value).strip()
    for canonical, choices in aliases.items():
        if folded.casefold() == canonical.casefold() or any(folded.casefold() == x.casefold() for x in choices):
            return canonical
    return "__unsupported__"


def _supported_in_quote(quote: str, aliases: dict[str, tuple[str, ...]]) -> set[str]:
    if aliases is UNIT_ALIASES:
        found = set(re.findall(r"(?<![A-Za-z])(?:kWh|MWh|GWh|kW|MW|GW)(?![A-Za-z])", quote))
        if "%" in unicodedata.normalize("NFKC", quote) or "百分比" in quote or "百分点" in quote:
            found.add("%")
        if "倍" in quote:
            found.add("倍")
        return found
    found = set()
    for canonical, choices in aliases.items():
        if any(choice in quote or choice.casefold() in quote.casefold() for choice in choices):
            found.add(canonical)
    return found


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def validate_candidate(
    candidate: EvidenceCandidate,
    chunk: IndustrySourceChunk,
    source: IndustryDataSource,
) -> ValidationResult:
    matches, _ = locate_quote(chunk.text, candidate.original_quote)
    if not matches:
        return ValidationResult("rejected", candidate.original_quote, rejection_reason="original_quote_not_found")
    if len(matches) != 1:
        return ValidationResult(
            "needs_review", candidate.original_quote, requires_manual_review=True,
            rejection_reason="ambiguous_quote_location",
        )
    start, end = matches[0]
    actual_quote = chunk.text[start:end]
    result = ValidationResult("verified", actual_quote, start, end)

    if chunk.content_hash != hashlib.sha256(chunk.text.encode("utf-8")).hexdigest():
        result.status, result.rejection_reason = "rejected", "chunk_hash_mismatch"
        return result
    lowered = actual_quote.casefold()
    if any(pattern.casefold() in lowered for pattern in INSTRUCTION_PATTERNS):
        result.status, result.rejection_reason = "rejected", "source_instruction_not_evidence"
        return result
    quote_numbers = set(re.findall(r"[+-]?\d[\d,，]*(?:\.\d+)?", unicodedata.normalize("NFKC", actual_quote)))
    claim_numbers = set(re.findall(r"[+-]?\d[\d,，]*(?:\.\d+)?", unicodedata.normalize("NFKC", candidate.normalized_claim)))
    if not claim_numbers.issubset(quote_numbers):
        result.status, result.rejection_reason = "rejected", "normalized_claim_fabricates_number"
        return result
    if candidate.subject and candidate.subject not in actual_quote:
        result.status = "needs_review"
        result.requires_manual_review = True
        result.rejection_reason = "subject_not_directly_supported"
    if candidate.claim_type == "inference":
        result.status, result.rejection_reason = "rejected", "inference_is_not_source_evidence"
        return result

    if candidate.claim_type in {"reported_opinion", "forecast"}:
        if not candidate.speaker or candidate.speaker not in actual_quote:
            result.status = "needs_review"
            result.requires_manual_review = True
            result.rejection_reason = "speaker_not_directly_supported"
    forecast_cues = ("预计", "预期", "计划", "目标", "力争", "可能", "forecast", "expects", "plans")
    if candidate.claim_type == "fact" and any(cue.casefold() in lowered for cue in forecast_cues):
        result.status = "needs_review"
        result.requires_manual_review = True
        result.rejection_reason = "forecast_language_labeled_as_fact"
    opinion_cues = ("认为", "表示", "声称", "指出", "据称", "believes", "said", "stated")
    if candidate.claim_type == "fact" and any(cue.casefold() in lowered for cue in opinion_cues):
        result.status = "needs_review"
        result.requires_manual_review = True
        result.rejection_reason = "opinion_language_labeled_as_fact"

    for field_name, field_value in (("period", candidate.period), ("as_of_date", candidate.as_of_date)):
        if field_value and unicodedata.normalize("NFKC", field_value) not in unicodedata.normalize("NFKC", actual_quote):
            result.status, result.rejection_reason = "rejected", f"{field_name}_not_supported"
            return result

    if candidate.raw_value is not None:
        raw = unicodedata.normalize("NFKC", candidate.raw_value).strip()
        raw_match = re.search(re.escape(raw), unicodedata.normalize("NFKC", actual_quote))
        if not raw_match:
            result.status, result.rejection_reason = "rejected", "raw_value_not_found"
            return result
        numeric = raw.replace(",", "").replace("，", "").rstrip("%％")
        try:
            decimal_value = Decimal(numeric)
        except InvalidOperation:
            result.status, result.rejection_reason = "rejected", "raw_value_not_numeric"
            return result
        context = unicodedata.normalize("NFKC", actual_quote)[raw_match.end(): raw_match.end() + 16]
        multiplier = Decimal("1")
        for label, factor in MULTIPLIERS:
            if context.lstrip().casefold().startswith(label.casefold()):
                multiplier = factor
                break
        result.value_multiplier = _decimal_text(multiplier)

        requested_unit = _canonical_requested(candidate.unit, UNIT_ALIASES)
        found_units = _supported_in_quote(actual_quote, UNIT_ALIASES)
        if requested_unit == "__unsupported__" or (requested_unit and requested_unit not in found_units):
            result.status, result.rejection_reason = "rejected", "unit_not_supported"
            return result
        if len(found_units) > 1 and requested_unit is None:
            result.status, result.requires_manual_review = "needs_review", True
            result.rejection_reason = "ambiguous_unit"
        result.unit = requested_unit or (next(iter(found_units)) if len(found_units) == 1 else None)

        requested_currency = _canonical_requested(candidate.currency, CURRENCY_ALIASES)
        found_currencies = _supported_in_quote(actual_quote, CURRENCY_ALIASES)
        ambiguous_symbol = "¥" in actual_quote or ("元" in actual_quote and not found_currencies)
        if requested_currency == "__unsupported__" or (
            requested_currency and requested_currency not in found_currencies
        ):
            result.status, result.rejection_reason = "rejected", "currency_not_supported"
            return result
        if ambiguous_symbol and not found_currencies:
            result.status, result.requires_manual_review = "needs_review", True
            result.rejection_reason = "ambiguous_currency"
        result.currency = requested_currency or (
            next(iter(found_currencies)) if len(found_currencies) == 1 else None
        )
        if result.unit == "%":
            multiplier *= Decimal("0.01")
            result.value_multiplier = _decimal_text(multiplier)
        result.normalized_value = _decimal_text(decimal_value * multiplier)

    if source.evidence_grade == "lead_only" or source.source_origin == "network_search":
        if result.status != "rejected":
            result.status, result.requires_manual_review = "lead_only", True
            result.rejection_reason = "lead_only_source"
    elif source.used_ocr and candidate.raw_value is not None and result.status != "rejected":
        result.status, result.requires_manual_review = "needs_review", True
        result.rejection_reason = "ocr_numeric_requires_review"
    return result


def evidence_card_to_dict(card: IndustryEvidenceCard) -> dict:
    return {
        column.name: (json.loads(card.risk_tags) if column.name == "risk_tags" else getattr(card, column.name))
        for column in card.__table__.columns
    }


def evidence_run_to_dict(run: IndustryEvidenceExtractionRun) -> dict:
    return {column.name: getattr(run, column.name) for column in run.__table__.columns}


class EvidenceCardService:
    def __init__(self, db: Session, analyzer: Optional[DeepSeekAnalyzer] = None) -> None:
        self.db = db
        self.analyzer = analyzer or DeepSeekAnalyzer()

    def _provider_name(self) -> str:
        return "gemini" if isinstance(self.analyzer, GeminiAnalyzer) else "deepseek"

    def _scope(self, report_id: int, source_id: Optional[int]) -> tuple[list[IndustryDataSource], list[IndustrySourceChunk]]:
        if not self.db.query(IndustryReport).filter(IndustryReport.id == report_id).first():
            raise ValueError("report_not_found")
        query = self.db.query(IndustryDataSource).filter(IndustryDataSource.report_id == report_id)
        if source_id is not None:
            query = query.filter(IndustryDataSource.id == source_id)
        sources = query.order_by(IndustryDataSource.id).all()
        if source_id is not None and not sources:
            raise ValueError("source_not_found_for_report")
        ids = [source.id for source in sources]
        chunks = [] if not ids else (
            self.db.query(IndustrySourceChunk)
            .filter(IndustrySourceChunk.report_id == report_id, IndustrySourceChunk.source_id.in_(ids))
            .order_by(IndustrySourceChunk.source_id, IndustrySourceChunk.chunk_index)
            .all()
        )
        return sources, chunks

    def extract(self, report_id: int, source_id: Optional[int] = None) -> IndustryEvidenceExtractionRun:
        sources, chunks = self._scope(report_id, source_id)
        snapshot = compute_source_snapshot_hash(sources, chunks)
        prior_query = self.db.query(IndustryEvidenceExtractionRun).filter(
            IndustryEvidenceExtractionRun.report_id == report_id,
            IndustryEvidenceExtractionRun.status == "completed",
            IndustryEvidenceExtractionRun.source_snapshot_hash == snapshot,
            IndustryEvidenceExtractionRun.prompt_version == EVIDENCE_EXTRACTION_PROMPT_VERSION,
        )
        prior_query = prior_query.filter(
            IndustryEvidenceExtractionRun.source_id_scope == source_id
            if source_id is not None
            else IndustryEvidenceExtractionRun.source_id_scope.is_(None)
        )
        prior = prior_query.order_by(IndustryEvidenceExtractionRun.id.desc()).first()
        if prior:
            return prior

        run = IndustryEvidenceExtractionRun(
            report_id=report_id, source_id_scope=source_id, status="running",
            extractor_provider=self._provider_name(),
            extractor_model=getattr(self.analyzer, "model", "unknown"),
            prompt_version=EVIDENCE_EXTRACTION_PROMPT_VERSION, source_snapshot_hash=snapshot,
            total_sources=len(sources), total_chunks=len(chunks), started_at=datetime.utcnow(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        source_by_id = {source.id: source for source in sources}
        try:
            next_code = self._next_evidence_number(report_id)
            for chunk in chunks:
                payload = self.analyzer.extract_evidence_candidates(chunk.text)
                if isinstance(payload, dict):
                    payload = EvidenceCandidatePayload.model_validate(payload)
                for candidate in payload.candidates:
                    run.candidate_count += 1
                    if candidate.importance_score < 3 and not (set(candidate.risk_tags) & NEGATIVE_TAGS):
                        continue
                    validation = validate_candidate(candidate, chunk, source_by_id[chunk.source_id])
                    dedupe_raw = "\0".join((str(chunk.id), candidate.original_quote, candidate.normalized_claim, candidate.claim_type))
                    card = IndustryEvidenceCard(
                        evidence_code=f"E{next_code:06d}", report_id=report_id,
                        source_id=chunk.source_id, chunk_id=chunk.id, extraction_run_id=run.id,
                        dedupe_key=hashlib.sha256(dedupe_raw.encode("utf-8")).hexdigest(),
                        chunk_content_hash=chunk.content_hash, locator=chunk.locator,
                        original_quote=validation.quote, quote_start=validation.quote_start,
                        quote_end=validation.quote_end, normalized_claim=candidate.normalized_claim,
                        claim_type=candidate.claim_type, subject=candidate.subject,
                        metric_name=candidate.metric_name, raw_value=candidate.raw_value,
                        normalized_value=validation.normalized_value,
                        value_multiplier=validation.value_multiplier, unit=validation.unit,
                        currency=validation.currency, period=candidate.period,
                        as_of_date=candidate.as_of_date, speaker=candidate.speaker,
                        importance_score=candidate.importance_score,
                        importance_reason=candidate.importance_reason,
                        risk_tags=json.dumps(sorted(set(candidate.risk_tags)), ensure_ascii=False),
                        extraction_confidence=(str(candidate.extraction_confidence) if candidate.extraction_confidence is not None else None),
                        validation_status=validation.status, verification_scope="source_match",
                        requires_manual_review=validation.requires_manual_review,
                        rejection_reason=validation.rejection_reason,
                        source_origin=source_by_id[chunk.source_id].source_origin,
                        evidence_grade=source_by_id[chunk.source_id].evidence_grade,
                    )
                    self.db.add(card)
                    next_code += 1
                    if validation.status == "verified":
                        run.verified_count += 1
                    elif validation.status in {"needs_review", "lead_only"}:
                        run.needs_review_count += 1
                    elif validation.status == "rejected":
                        run.rejected_count += 1
            run.status = "completed"
            run.completed_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self.db.rollback()
            failed = self.db.get(IndustryEvidenceExtractionRun, run.id)
            if failed:
                failed.status = "failed"
                failed.error_message = "证据提取输出未通过校验或处理失败"
                failed.completed_at = datetime.utcnow()
                self.db.commit()
            raise EvidenceExtractionError("证据提取失败，未写入不完整证据卡") from exc

    def _next_evidence_number(self, report_id: int) -> int:
        codes = self.db.query(IndustryEvidenceCard.evidence_code).filter(
            IndustryEvidenceCard.report_id == report_id
        ).all()
        numbers = [int(code[1:]) for (code,) in codes if re.fullmatch(r"E\d+", code or "")]
        return max(numbers, default=0) + 1

    def refresh_stale(self, report_id: int) -> None:
        changed = False
        cards = self.db.query(IndustryEvidenceCard).filter(IndustryEvidenceCard.report_id == report_id).all()
        for card in cards:
            chunk = self.db.get(IndustrySourceChunk, card.chunk_id)
            if chunk and (
                chunk.report_id != report_id or chunk.source_id != card.source_id
                or chunk.content_hash != card.chunk_content_hash
            ) and card.validation_status != "stale":
                card.validation_status = "stale"
                card.requires_manual_review = True
                card.rejection_reason = "source_chunk_changed"
                changed = True
        if changed:
            self.db.commit()

    def list_cards(
        self, report_id: int, source_id: Optional[int] = None,
        validation_status: Optional[str] = None, claim_type: Optional[str] = None,
        risk_tag: Optional[str] = None,
    ) -> list[IndustryEvidenceCard]:
        self._scope(report_id, None)
        self.refresh_stale(report_id)
        query = self.db.query(IndustryEvidenceCard).filter(IndustryEvidenceCard.report_id == report_id)
        if source_id is not None:
            query = query.filter(IndustryEvidenceCard.source_id == source_id)
        if validation_status:
            query = query.filter(IndustryEvidenceCard.validation_status == validation_status)
        if claim_type:
            query = query.filter(IndustryEvidenceCard.claim_type == claim_type)
        rows = query.order_by(IndustryEvidenceCard.id).all()
        if risk_tag:
            rows = [row for row in rows if risk_tag in json.loads(row.risk_tags)]
        return rows
