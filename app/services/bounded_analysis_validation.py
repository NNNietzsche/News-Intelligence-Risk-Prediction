"""Deterministic validation for V2 evidence facts and bounded analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.schemas import StructuredGroundedReportCandidate
from app.services.citation_validation import (
    ValidationResult,
    _validate_sentence_support,
    validate_citations,
)
from app.services.structured_report_compiler import compile_structured_report


CAUTIOUS_MARKERS = (
    "可能", "或将", "表明", "意味着", "存在", "风险", "需要关注",
)
CERTAINTY_MARKERS = ("必然", "一定", "肯定", "确保", "完全", "无风险")
ACTUALIZATION_MARKERS = ("已经", "已完成", "已实现", "已发生", "正式投产", "已经投产")
NAMED_SUBJECT_RE = re.compile(
    r"[A-Za-z0-9\u4e00-\u9fff]{2,40}(?:公司|集团|银行|委员会|机构|项目|工厂|电站|平台|基金)"
)
EVENT_MARKERS = (
    "违约", "事故", "诉讼", "处罚", "破产", "停产", "投产", "收购", "裁员",
    "重组", "退市", "关停", "亏损", "盈利",
)
DERIVED_RESULT_RE = re.compile(
    r"(?:市场份额|增长率|评级|概率|收入|利润|现金流|财务结果).{0,12}"
    r"(?:上升|提升|增长|下降|降低|达到|改善|恶化|上调|下调|为\d)"
)


@dataclass(frozen=True)
class StructuredReportValidation:
    compiled_report: dict[str, Any]
    result: ValidationResult


def _error(
    code: str, location: str, text: str, evidence_codes: list[str], message: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "location": location,
        "sentence": text[:1000],
        "evidence_codes": sorted(set(evidence_codes)),
        "message": message,
    }


def _has_cautious_language(text: str) -> bool:
    return any(marker in text for marker in CAUTIOUS_MARKERS) or bool(
        re.search(r"若.{1,200}则", text)
    )


def _bounded_errors(
    text: str, location: str, codes: list[str], evidence_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    evidence = [evidence_map[code] for code in codes if code in evidence_map]
    if not codes or len(evidence) != len(set(codes)):
        errors.append(_error(
            "BOUNDED_ANALYSIS_EVIDENCE_INVALID", location, text, codes,
            "有限分析必须只引用当前Evidence Packet中的合格verified证据。",
        ))
    if not _has_cautious_language(text):
        errors.append(_error(
            "BOUNDED_ANALYSIS_HEDGE_REQUIRED", location, text, codes,
            "有限分析必须使用可能、或将、若…则、表明、意味着、存在风险或需要关注等审慎措辞。",
        ))
    found_certainty = [marker for marker in CERTAINTY_MARKERS if marker in text]
    if found_certainty:
        errors.append(_error(
            "BOUNDED_ANALYSIS_CERTAINTY_FORBIDDEN", location, text, codes,
            "有限分析包含禁止的确定性措辞：" + "、".join(found_certainty),
        ))

    evidence_text = " ".join(
        str(item.get("normalized_claim") or "") + " " + str(item.get("original_quote") or "")
        for item in evidence
    )
    unsupported_subjects = {
        subject for subject in NAMED_SUBJECT_RE.findall(text) if subject not in evidence_text
    }
    if unsupported_subjects:
        errors.append(_error(
            "BOUNDED_ANALYSIS_NEW_ENTITY", location, text, codes,
            "有限分析增加了证据中不存在的主体或项目：" + "、".join(sorted(unsupported_subjects)),
        ))
    unsupported_events = {
        marker for marker in EVENT_MARKERS if marker in text and marker not in evidence_text
    }
    if unsupported_events:
        errors.append(_error(
            "BOUNDED_ANALYSIS_NEW_EVENT", location, text, codes,
            "有限分析增加了证据中不存在的事件：" + "、".join(sorted(unsupported_events)),
        ))
    if any(item.get("claim_type") == "forecast" for item in evidence) and any(
        marker in text and marker not in evidence_text for marker in ACTUALIZATION_MARKERS
    ):
        errors.append(_error(
            "FORECAST_AS_FACT", location, text, codes,
            "有限分析把计划或预测改写成已经发生的事实。",
        ))
    if DERIVED_RESULT_RE.search(text):
        errors.append(_error(
            "BOUNDED_ANALYSIS_NEW_DERIVED_RESULT", location, text, codes,
            "有限分析不得生成新的市场份额、增长率、评级、概率或财务结果。",
        ))
    if evidence:
        errors.extend(_validate_sentence_support(
            text, location, codes, evidence_map, require_lexical_support=False,
        ))
    return errors


def validate_structured_grounded_report(
    candidate: StructuredGroundedReportCandidate | dict[str, Any],
    packet: dict[str, Any],
) -> StructuredReportValidation:
    model = (
        candidate
        if isinstance(candidate, StructuredGroundedReportCandidate)
        else StructuredGroundedReportCandidate.model_validate(candidate)
    )
    compiled = compile_structured_report(model)
    evidence_map = {item["evidence_code"]: item for item in packet.get("evidence") or []}
    bounded_errors: list[dict[str, Any]] = []
    bounded_count = 0
    fact_count = 0
    for section_index, section in enumerate(model.structured_sections):
        location = f"sections[{section_index}].content"
        for sentence in section.sentences:
            codes = list(dict.fromkeys(sentence.evidence_codes))
            if sentence.sentence_type == "bounded_analysis":
                bounded_count += 1
                bounded_errors.extend(_bounded_errors(
                    sentence.text, location, codes, evidence_map,
                ))
            else:
                fact_count += 1

    citation_result = validate_citations(
        compiled.report, packet, sentence_types=compiled.sentence_types,
    )
    errors = citation_result.errors + bounded_errors
    # Stable de-duplication is useful when the shared validator and the
    # bounded validator identify the same unsupported entity/event.
    unique_errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in errors:
        key = (str(item.get("code")), str(item.get("location")), str(item.get("sentence")))
        if key not in seen:
            seen.add(key)
            unique_errors.append(item)
    coverage = dict(citation_result.coverage)
    coverage.update({
        "evidence_fact_sentence_count": fact_count,
        "bounded_analysis_sentence_count": bounded_count,
    })
    result = ValidationResult(
        valid=not unique_errors,
        errors=unique_errors,
        warnings=citation_result.warnings,
        coverage=coverage,
    )
    return StructuredReportValidation(compiled_report=compiled.report, result=result)
