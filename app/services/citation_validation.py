"""Deterministic sentence-level citation validation for grounded report candidates."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable


CITATION_RE = re.compile(r"\[(E\d+)\]")
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[+-]?\d[\d,，]*(?:\.\d+)?")
FACT_MARKERS = (
    "收入", "利润", "产能", "产量", "市场", "份额", "增长", "下降", "达到", "发生",
    "政策", "监管", "债务", "融资", "项目", "价格", "成本", "装机", "事故", "诉讼",
    "违约", "处罚", "预计", "计划", "目标", "认为", "表示", "为",
)
LIMITATION_MARKERS = (
    "证据不足", "资料不足", "无法判断", "尚未确认", "待核实", "本报告仅", "方法说明", "局限",
)
DISCLOSURE_MARKERS = ("存在差异", "不同来源", "尚未解决", "无法确定", "待核实", "分别披露", "口径不同")
FORECAST_MARKERS = ("预计", "预期", "预测", "计划", "目标", "力争", "可能")
OPINION_MARKERS = ("认为", "表示", "指出", "称", "观点", "判断")
EVENT_MARKERS = ("违约", "事故", "诉讼", "处罚", "破产", "停产", "收购", "投产", "亏损", "盈利")
ENTITY_RE = re.compile(
    r"(?:[A-Za-z0-9\u4e00-\u9fff]{1,30}(?:公司|集团|株式会社|有限责任公司|股份有限公司)|"
    r"株式会社[A-Za-z0-9\u4e00-\u9fff]{1,30})"
)
CURRENCY_ALIASES = {
    "JPY": ("日元", "JPY", "円"), "CNY": ("人民币", "CNY", "RMB"),
    "USD": ("美元", "USD", "$"), "EUR": ("欧元", "EUR", "€"),
}
UNIT_TOKENS = ("GWh", "MWh", "kWh", "GW", "MW", "kW", "百分点", "%", "％", "倍")
MAGNITUDE_TOKENS = {
    "万": "1e4", "萬": "1e4", "百万": "1e6", "百萬": "1e6",
    "千万": "1e7", "千萬": "1e7", "亿": "1e8", "億": "1e8",
    "十亿": "1e9", "十億": "1e9", "兆": "1e12",
}
NEGATION_MARKERS = ("未", "无", "没有", "尚未", "并未", "从未", "否认")


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    coverage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid, "errors": self.errors,
            "warnings": self.warnings, "coverage": self.coverage,
        }


def _error(code: str, location: str, sentence: str, evidence_codes: Iterable[str], message: str) -> dict:
    return {
        "code": code, "location": location, "sentence": sentence[:1000],
        "evidence_codes": sorted(set(evidence_codes)), "message": message,
    }


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[。！？!?；;])|\n+", text or "") if item.strip()]


def _report_text_fields(report: dict[str, Any]) -> list[tuple[str, str]]:
    fields = [("summary", str(report.get("summary") or "")), ("risk_outlook", str(report.get("risk_outlook") or ""))]
    for index, section in enumerate(report.get("sections") or []):
        if isinstance(section, dict):
            fields.append((f"sections[{index}].content", str(section.get("content") or "")))
    return fields


def _normalize_number(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace(",", "").replace("，", "")


def _supported_numbers(evidence: list[dict[str, Any]]) -> set[str]:
    supported = set()
    for item in evidence:
        for field in ("raw_value", "normalized_value", "period", "as_of_date", "original_quote"):
            for number in NUMBER_RE.findall(str(item.get(field) or "")):
                supported.add(_normalize_number(number))
    return supported


def _currency_tokens(text: str) -> set[str]:
    return {
        canonical for canonical, aliases in CURRENCY_ALIASES.items()
        if any(alias in text or alias.casefold() in text.casefold() for alias in aliases)
    }


def _unit_tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text)
    found = set()
    for token in UNIT_TOKENS:
        if token in normalized:
            found.add("%" if token in {"%", "％"} else token)
    # Avoid treating the MW substring inside MWh as a second unit.
    if "MWh" in found:
        found.discard("MW")
    if "GWh" in found:
        found.discard("GW")
    if "kWh" in found:
        found.discard("kW")
    return found


def _magnitude_tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text)
    return {canonical for token, canonical in MAGNITUDE_TOKENS.items() if token in normalized}


def _event_polarities(text: str, marker: str) -> set[bool]:
    polarities = set()
    for match in re.finditer(re.escape(marker), text):
        prefix = text[max(0, match.start() - 6):match.start()]
        polarities.add(any(negative in prefix for negative in NEGATION_MARKERS))
    return polarities


def _compact(text: str) -> str:
    text = CITATION_RE.sub("", unicodedata.normalize("NFKC", text).casefold())
    return re.sub(r"[\s\W_]+", "", text)


def _lexically_supported(sentence: str, evidence: list[dict[str, Any]]) -> bool:
    compact_sentence = _compact(sentence)
    support = _compact(" ".join(
        str(item.get("normalized_claim") or "") + " " + str(item.get("original_quote") or "")
        for item in evidence
    ))
    if not compact_sentence:
        return True
    if compact_sentence in support or any(
        _compact(str(item.get("normalized_claim") or "")) in compact_sentence
        for item in evidence if len(_compact(str(item.get("normalized_claim") or ""))) >= 6
    ):
        return True
    sentence_chars = re.sub(r"\d+", "", compact_sentence)
    bigrams = {sentence_chars[index:index + 2] for index in range(max(0, len(sentence_chars) - 1))}
    if not bigrams:
        return True
    overlap = sum(1 for token in bigrams if token in support)
    return overlap / len(bigrams) >= 0.35


def _is_factual(sentence: str) -> bool:
    if any(marker in sentence for marker in LIMITATION_MARKERS) and not NUMBER_RE.search(sentence):
        return False
    # Report body prose is factual by default. Only explicit methodology or
    # limitation language is exempt; this avoids a keyword list becoming a
    # loophole for uncited facts such as locations, ownership or ratings.
    return bool(_compact(sentence))


def _validate_sentence_support(
    sentence: str, location: str, evidence_codes: list[str], evidence_map: dict[str, dict[str, Any]],
    *, require_lexical_support: bool = True,
) -> list[dict[str, Any]]:
    errors = []
    evidence = [evidence_map[code] for code in evidence_codes if code in evidence_map]
    sentence_numbers = {_normalize_number(value) for value in NUMBER_RE.findall(sentence)}
    unsupported_numbers = sentence_numbers - _supported_numbers(evidence)
    if unsupported_numbers:
        errors.append(_error(
            "UNSUPPORTED_NUMBER", location, sentence, evidence_codes,
            "句中数字未在本句引用证据中找到：" + "、".join(sorted(unsupported_numbers)),
        ))
    sentence_currencies = _currency_tokens(sentence)
    evidence_currencies = {str(item.get("currency")) for item in evidence if item.get("currency")}
    if sentence_currencies - evidence_currencies:
        errors.append(_error("UNSUPPORTED_CURRENCY", location, sentence, evidence_codes, "句中币种未被引用证据支持。"))
    sentence_units = _unit_tokens(sentence)
    evidence_units = {str(item.get("unit")) for item in evidence if item.get("unit")}
    if "百分点" in sentence_units:
        evidence_units.add("百分点") if any("百分点" in str(item.get("original_quote")) for item in evidence) else None
    if sentence_units - evidence_units:
        errors.append(_error("UNSUPPORTED_UNIT", location, sentence, evidence_codes, "句中单位或维度未被引用证据支持。"))
    sentence_magnitudes = _magnitude_tokens(sentence)
    evidence_magnitudes = _magnitude_tokens(" ".join(
        str(item.get("original_quote") or "") + " " + str(item.get("raw_value") or "")
        for item in evidence
    ))
    if sentence_numbers and evidence_magnitudes and sentence_magnitudes != evidence_magnitudes:
        errors.append(_error("UNSUPPORTED_UNIT", location, sentence, evidence_codes, "句中金额数量级未被引用证据支持。"))
    sentence_years = set(re.findall(r"(?:19|20)\d{2}", sentence))
    evidence_years = set(re.findall(
        r"(?:19|20)\d{2}", " ".join(
            str(item.get("period") or "") + " " + str(item.get("as_of_date") or "")
            + " " + str(item.get("original_quote") or "") for item in evidence
        )
    ))
    if sentence_years - evidence_years:
        errors.append(_error("UNSUPPORTED_PERIOD", location, sentence, evidence_codes, "句中年份或期间未被引用证据支持。"))
    event_markers = {marker for marker in EVENT_MARKERS if marker in sentence}
    evidence_text = " ".join(
        str(item.get("normalized_claim") or "") + str(item.get("original_quote") or "") for item in evidence
    )
    if any(
        marker not in evidence_text
        or not (_event_polarities(sentence, marker) & _event_polarities(evidence_text, marker))
        for marker in event_markers
    ):
        errors.append(_error("UNSUPPORTED_EVENT", location, sentence, evidence_codes, "句中事件状态未被引用证据支持。"))
    entity_mentions = set(ENTITY_RE.findall(CITATION_RE.sub("", sentence)))
    unsupported_entities = {entity for entity in entity_mentions if entity not in evidence_text}
    if unsupported_entities:
        errors.append(_error(
            "UNSUPPORTED_ENTITY", location, sentence, evidence_codes,
            "句中企业或主体名称未被引用证据支持：" + "、".join(sorted(unsupported_entities)),
        ))
    if require_lexical_support and evidence and not _lexically_supported(sentence, evidence):
        errors.append(_error("UNSUPPORTED_CLAIM", location, sentence, evidence_codes, "句中事实性内容与引用证据缺少足够的确定性文本对应。"))
    claim_types = {str(item.get("claim_type")) for item in evidence}
    if claim_types and claim_types <= {"forecast"} and not any(marker in sentence for marker in FORECAST_MARKERS):
        errors.append(_error("FORECAST_AS_FACT", location, sentence, evidence_codes, "预测证据被改写成确定性事实。"))
    opinion_items = [item for item in evidence if item.get("claim_type") == "reported_opinion"]
    if opinion_items:
        speakers = {str(item.get("speaker")) for item in opinion_items if item.get("speaker")}
        if not any(marker in sentence for marker in OPINION_MARKERS) or not any(speaker in sentence for speaker in speakers):
            errors.append(_error("OPINION_ATTRIBUTION_MISSING", location, sentence, evidence_codes, "观点证据缺少speaker或观点属性。"))
    return errors


def validate_citations(
    report: dict[str, Any], packet: dict[str, Any],
    *, sentence_types: dict[tuple[str, int], str] | None = None,
) -> ValidationResult:
    evidence_map = {item["evidence_code"]: item for item in packet.get("evidence") or []}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    inline_pairs: set[tuple[str, str]] = set()
    cited_codes: set[str] = set()
    citation_occurrences = 0
    factual_count = 0
    cited_factual_count = 0
    uncited_count = 0

    declared_pairs = {
        (str(item.get("evidence_code")), str(item.get("location")))
        for item in report.get("citations") or [] if isinstance(item, dict)
    }
    conflict_by_member: dict[str, list[dict[str, Any]]] = {}
    for conflict in packet.get("unresolved_conflicts") or []:
        for code in conflict.get("member_evidence_codes") or []:
            conflict_by_member.setdefault(code, []).append(conflict)
    disclosed_by_member: dict[str, list[dict[str, Any]]] = {}
    for conflict in packet.get("resolved_conflicts") or []:
        if conflict.get("resolution_status") == "resolved_disclosed":
            for code in conflict.get("member_evidence_codes") or []:
                disclosed_by_member.setdefault(code, []).append(conflict)

    for location, text in _report_text_fields(report):
        for sentence_index, sentence in enumerate(_sentences(text)):
            codes = CITATION_RE.findall(sentence)
            citation_occurrences += len(codes)
            cited_codes.update(codes)
            for code in codes:
                inline_pairs.add((code, location))
                if code not in evidence_map:
                    errors.append(_error("INVALID_EVIDENCE_CODE", location, sentence, [code], "引用不存在、失效或不允许用于当前报告。"))
            factual = _is_factual(sentence)
            if factual:
                factual_count += 1
                if not codes:
                    uncited_count += 1
                    errors.append(_error("UNCITED_FACT", location, sentence, [], "事实性句子必须在同一句内引用证据。"))
                else:
                    cited_factual_count += 1
                    errors.extend(_validate_sentence_support(
                        sentence, location, codes, evidence_map,
                        require_lexical_support=(
                            (sentence_types or {}).get((location, sentence_index))
                            != "bounded_analysis"
                        ),
                    ))
            for code in set(codes):
                for conflict in conflict_by_member.get(code, []):
                    member_codes = set(conflict.get("member_evidence_codes") or [])
                    if not any(marker in sentence for marker in DISCLOSURE_MARKERS) or len(member_codes & set(codes)) < 2:
                        errors.append(_error(
                            "UNRESOLVED_CONFLICT_ASSERTED", location, sentence, codes,
                            f"未解决冲突{conflict.get('conflict_code')}不能被表述为唯一确定值。",
                        ))
                for conflict in disclosed_by_member.get(code, []):
                    member_codes = set(conflict.get("member_evidence_codes") or [])
                    if not any(marker in sentence for marker in DISCLOSURE_MARKERS) or len(member_codes & set(codes)) < 2:
                        errors.append(_error(
                            "DISCLOSED_CONFLICT_NOT_DISCLOSED", location, sentence, codes,
                            f"冲突{conflict.get('conflict_code')}要求并列披露来源差异。",
                        ))

    missing_declared = inline_pairs - declared_pairs
    extra_declared = declared_pairs - inline_pairs
    for code, location in sorted(missing_declared):
        errors.append(_error("CITATION_LIST_MISMATCH", location, "", [code], "内联引用未出现在citations列表的相同位置。"))
    for code, location in sorted(extra_declared):
        errors.append(_error("CITATION_LIST_MISMATCH", location, "", [code], "citations列表项没有对应的同位置内联引用。"))

    for index, metric in enumerate(report.get("key_metrics") or []):
        location = f"key_metrics[{index}]"
        if not isinstance(metric, dict) or not metric.get("evidence_code"):
            errors.append(_error("KEY_METRIC_CITATION_MISSING", location, str(metric), [], "key_metrics中的每个数字必须包含evidence_code。"))
            continue
        code = str(metric["evidence_code"])
        if code not in evidence_map:
            errors.append(_error("INVALID_EVIDENCE_CODE", location, str(metric), [code], "key metric引用无效。"))
            continue
        errors.extend(_validate_sentence_support(
            f"{metric.get('name', '')}{metric.get('value', '')}[{code}]", location, [code], evidence_map
        ))

    disclosure_text = " ".join(
        [str(item) for item in report.get("limitations") or []]
        + [str(item) for item in report.get("unresolved_conflicts") or []]
    )
    for conflict in packet.get("unresolved_conflicts") or []:
        if conflict.get("severity") in {"high", "critical"} and conflict.get("conflict_code") not in disclosure_text:
            errors.append(_error(
                "MATERIAL_CONFLICT_NOT_DISCLOSED", "limitations", "", [],
                f"{conflict.get('severity')}级未解决冲突{conflict.get('conflict_code')}必须在限制或冲突列表中披露。",
            ))

    partial_codes = {
        item.get("evidence_code") for item in packet.get("evidence") or []
        if item.get("evidence_grade") == "partial_text"
    }
    used_partial_codes = partial_codes & cited_codes
    if used_partial_codes and not any(marker in disclosure_text for marker in ("部分正文", "截断", "未审阅完整")):
        errors.append(_error(
            "SOURCE_LIMITATION_NOT_DISCLOSED", "limitations", "", used_partial_codes,
            "使用partial_text证据时必须披露资料不完整限制。",
        ))

    if packet.get("missing_information"):
        warnings.append({"code": "MISSING_INFORMATION", "message": "Evidence Packet存在覆盖缺口。"})
    coverage = {
        "sentence_count": sum(len(_sentences(text)) for _, text in _report_text_fields(report)),
        "factual_sentence_count": factual_count,
        "cited_factual_sentence_count": cited_factual_count,
        "uncited_sentence_count": uncited_count,
        "citation_count": citation_occurrences,
        "cited_evidence_count": len(cited_codes),
        "factual_citation_coverage": (
            round(cited_factual_count / factual_count, 4) if factual_count else 1.0
        ),
    }
    return ValidationResult(not errors, errors, warnings, coverage)
