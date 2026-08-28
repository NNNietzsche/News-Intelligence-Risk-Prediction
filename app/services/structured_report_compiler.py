"""Compile V2 structured shadow candidates into the existing report JSON shape."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.schemas import StructuredGroundedReportCandidate


EVIDENCE_CODE_RE = re.compile(r"^E\d+$")
INLINE_CITATION_RE = re.compile(r"\[E\d+\]")
TERMINAL_PUNCTUATION = "。！？!?；;"


@dataclass(frozen=True)
class CompiledStructuredReport:
    report: dict[str, Any]
    sentence_types: dict[tuple[str, int], str]


def _one_sentence(text: str) -> str:
    value = text.strip()
    if INLINE_CITATION_RE.search(value):
        raise ValueError("structured_sentence_must_not_contain_inline_citations")
    core = value[:-1] if value and value[-1] in TERMINAL_PUNCTUATION else value
    if "\n" in core or any(mark in core for mark in TERMINAL_PUNCTUATION):
        raise ValueError("structured_sentence_must_contain_exactly_one_sentence")
    return value


def _compile_sentence(text: str, evidence_codes: list[str]) -> tuple[str, list[str]]:
    value = _one_sentence(text)
    codes: list[str] = []
    for code in evidence_codes:
        normalized = str(code)
        if not EVIDENCE_CODE_RE.fullmatch(normalized):
            raise ValueError("structured_sentence_has_invalid_evidence_code")
        if normalized not in codes:
            codes.append(normalized)
    if not codes:
        raise ValueError("structured_sentence_requires_evidence")
    suffix = "".join(f"[{code}]" for code in codes)
    if value[-1:] in TERMINAL_PUNCTUATION:
        return value[:-1].rstrip() + suffix + value[-1], codes
    return value + suffix + "。", codes


def compile_structured_report(
    candidate: StructuredGroundedReportCandidate | dict[str, Any],
) -> CompiledStructuredReport:
    model = (
        candidate
        if isinstance(candidate, StructuredGroundedReportCandidate)
        else StructuredGroundedReportCandidate.model_validate(candidate)
    )
    sections: list[dict[str, str]] = []
    citations: list[dict[str, str]] = []
    citation_pairs: set[tuple[str, str]] = set()
    sentence_types: dict[tuple[str, int], str] = {}

    for section_index, section in enumerate(model.structured_sections):
        location = f"sections[{section_index}].content"
        lines: list[str] = []
        for sentence_index, sentence in enumerate(section.sentences):
            compiled, codes = _compile_sentence(sentence.text, sentence.evidence_codes)
            lines.append(compiled)
            sentence_types[(location, sentence_index)] = sentence.sentence_type
            for code in codes:
                pair = (code, location)
                if pair not in citation_pairs:
                    citation_pairs.add(pair)
                    citations.append({"evidence_code": code, "location": location})
        sections.append({"heading": section.heading, "content": "\n".join(lines)})

    report = {
        "title": model.title,
        "sections": sections,
        "summary": "",
        "risk_outlook": "",
        "key_metrics": [item.model_dump() for item in model.key_metrics],
        "citations": citations,
        "limitations": list(model.limitations),
        "unresolved_conflicts": list(model.unresolved_conflicts),
        "evidence_coverage": {},
        "generation_metadata": {},
    }
    return CompiledStructuredReport(report=report, sentence_types=sentence_types)
