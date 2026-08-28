from __future__ import annotations

from app.config import Settings
from app.services.conflict_detection import ConflictDetectionService
from app.services.deepseek_analyzer import EVIDENCE_EXTRACTION_PROMPT_VERSION
from app.services.evidence_cards import compute_source_snapshot_hash
from tests.test_conflict_detection import add_card, setup_report


class FormalFakeAnalyzer:
    model = "fake-grounded"

    def __init__(self, generated=None, repaired=None):
        self.generated = generated
        self.repaired = repaired if repaired is not None else generated
        self.generate_calls = 0
        self.repair_calls = 0
        self.legacy_calls = 0

    def generate_grounded_report(self, packet, *_args):
        self.generate_calls += 1
        if any("extracted_text" in item for item in packet.get("evidence", [])):
            raise AssertionError("grounded packet exposed stitched full text")
        return self.generated

    def repair_grounded_report(self, *_args):
        self.repair_calls += 1
        return self.repaired

    def analyze_industry(self, *_args, **_kwargs):
        self.legacy_calls += 1
        return {
            "title": "legacy report", "sections": [], "summary": "legacy summary",
            "risk_outlook": "legacy outlook", "key_metrics": [],
        }


def grounded_settings(*, approval=True):
    return Settings(
        _env_file=None,
        industry_report_generation_mode="grounded",
        grounded_report_require_approval=approval,
        grounded_report_allow_legacy_fallback=False,
    )


def legacy_settings():
    return Settings(_env_file=None, industry_report_generation_mode="legacy")


def make_ready_report(db, *, value="100", quote=None):
    report, extraction_run = setup_report(db)
    source, chunk, card = add_card(
        db, report, extraction_run, "E000001", value, quote=quote,
    )
    source.parse_status = "parsed"
    extraction_run.prompt_version = EVIDENCE_EXTRACTION_PROMPT_VERSION
    extraction_run.source_snapshot_hash = compute_source_snapshot_hash([source], [chunk])
    db.commit()
    ConflictDetectionService(db).detect(report.id)
    return report, extraction_run, source, chunk, card


def grounded_candidate(card, *, cited=True):
    sentence = card.original_quote.rstrip("。")
    if cited:
        sentence += f"[{card.evidence_code}]"
    sentence += "。"
    return {
        "title": "证据约束报告",
        "sections": [],
        "summary": sentence,
        "risk_outlook": "",
        "key_metrics": [],
        "citations": (
            [{"evidence_code": card.evidence_code, "location": "summary"}] if cited else []
        ),
        "limitations": [],
        "unresolved_conflicts": [],
        "evidence_coverage": {},
        "generation_metadata": {},
    }
