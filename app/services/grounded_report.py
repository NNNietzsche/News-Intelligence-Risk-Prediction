"""Shadow-mode evidence-constrained report generation orchestration."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.database.models import IndustryGroundedReportRun, IndustryReport
from app.schemas import GroundedReportCandidate, StructuredGroundedReportCandidate
from app.services.bounded_analysis_validation import validate_structured_grounded_report
from app.services.citation_validation import ValidationResult, validate_citations
from app.services.deepseek_analyzer import (
    GROUNDED_REPORT_PROMPT_VERSION,
    STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION,
    DeepSeekAnalyzer,
    GroundedReportOutputError,
)
from app.services.evidence_packet import build_evidence_packet
from app.services.gemini_analyzer import GeminiAnalyzer


class GroundedReportError(RuntimeError):
    pass


class GroundedPromotionError(GroundedReportError):
    def __init__(self, code: str, message: str, next_step: str = "grounded generation") -> None:
        super().__init__(message)
        self.code = code
        self.next_step = next_step


def grounded_run_to_dict(
    run: IndustryGroundedReportRun, *, include_candidate: bool = False,
    include_validation: bool = False,
) -> dict[str, Any]:
    excluded = {"candidate_report_json", "validation_errors_json"}
    result = {
        column.name: getattr(run, column.name)
        for column in run.__table__.columns if column.name not in excluded
    }
    def bounded(value: Any) -> Any:
        if isinstance(value, str):
            return value if len(value) <= 20000 else value[:20000] + "…[truncated]"
        if isinstance(value, list):
            return [bounded(item) for item in value[:200]]
        if isinstance(value, dict):
            return {str(key): bounded(item) for key, item in list(value.items())[:200]}
        return value

    if include_candidate:
        candidate = json.loads(run.candidate_report_json) if run.candidate_report_json else None
        result["candidate_report"] = (
            {"invalid_output_retained_for_internal_audit": True}
            if isinstance(candidate, dict) and "raw_output" in candidate
            else bounded(candidate)
        )
    if include_validation:
        result["validation"] = (
            bounded(json.loads(run.validation_errors_json)) if run.validation_errors_json else None
        )
    return result


def _analyzer_provider(analyzer: DeepSeekAnalyzer) -> str:
    return "gemini" if isinstance(analyzer, GeminiAnalyzer) else "deepseek"


class GroundedReportService:
    def __init__(self, db: Session, analyzer: Optional[DeepSeekAnalyzer] = None) -> None:
        self.db = db
        self.analyzer = analyzer or DeepSeekAnalyzer()

    def _report(self, report_id: int) -> IndustryReport:
        report = self.db.get(IndustryReport, report_id)
        if not report:
            raise ValueError("report_not_found")
        return report

    def generate(
        self, report_id: int, *, prompt_version: str = GROUNDED_REPORT_PROMPT_VERSION,
    ) -> IndustryGroundedReportRun:
        report = self._report(report_id)
        if prompt_version not in {
            GROUNDED_REPORT_PROMPT_VERSION, STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION,
        }:
            raise ValueError("unsupported_grounded_prompt_version")
        structured_v2 = prompt_version == STRUCTURED_GROUNDED_REPORT_PROMPT_VERSION
        packet = build_evidence_packet(self.db, report_id)
        prior = self.db.query(IndustryGroundedReportRun).filter(
            IndustryGroundedReportRun.report_id == report_id,
            IndustryGroundedReportRun.evidence_snapshot_hash == packet["evidence_snapshot_hash"],
            IndustryGroundedReportRun.conflict_snapshot_hash == packet["conflict_snapshot_hash"],
            IndustryGroundedReportRun.prompt_version == prompt_version,
            IndustryGroundedReportRun.status == "validated",
        ).order_by(IndustryGroundedReportRun.id.desc()).first()
        if prior:
            return prior

        run = IndustryGroundedReportRun(
            report_id=report_id,
            evidence_snapshot_hash=packet["evidence_snapshot_hash"],
            conflict_snapshot_hash=packet["conflict_snapshot_hash"],
            prompt_version=prompt_version,
            provider=_analyzer_provider(self.analyzer),
            model=getattr(self.analyzer, "model", "unknown"),
            status="running", started_at=datetime.utcnow(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        if not packet["evidence"]:
            run.status = "failed"
            run.failure_code = "insufficient_evidence"
            validation = {
                "valid": False,
                "errors": [{
                    "code": "INSUFFICIENT_EVIDENCE", "location": "evidence_packet",
                    "sentence": "", "evidence_codes": [],
                    "message": "当前报告没有合格verified证据，未调用模型。",
                }],
                "warnings": [], "coverage": packet["coverage"],
            }
            run.validation_errors_json = json.dumps(validation, ensure_ascii=False)
            run.completed_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(run)
            return run

        candidate: Any = None
        validation: Optional[ValidationResult] = None
        first_errors: list[dict[str, Any]] = []
        try:
            try:
                if structured_v2:
                    candidate = self.analyzer.generate_structured_grounded_report(
                        packet, report.industry_name, report.company_name
                    )
                    candidate = StructuredGroundedReportCandidate.model_validate(
                        candidate
                    ).model_dump()
                else:
                    candidate = self.analyzer.generate_grounded_report(
                        packet, report.industry_name, report.company_name
                    )
                    candidate = GroundedReportCandidate.model_validate(candidate).model_dump()
                run.candidate_report_json = json.dumps(candidate, ensure_ascii=False)
                run.status = "validating"
                self.db.commit()
                validation = (
                    validate_structured_grounded_report(candidate, packet).result
                    if structured_v2 else validate_citations(candidate, packet)
                )
                first_errors = validation.errors
            except GroundedReportOutputError as exc:
                candidate = {"raw_output": exc.raw_output[:12000]}
                first_errors = [{
                    "code": "SCHEMA_INVALID", "location": "candidate_report",
                    "sentence": "", "evidence_codes": [], "message": "候选报告未通过严格Schema校验。",
                }]
            except ValidationError:
                first_errors = [{
                    "code": "SCHEMA_INVALID", "location": "candidate_report",
                    "sentence": "", "evidence_codes": [], "message": "候选报告未通过严格Schema校验。",
                }]
            except ValueError as exc:
                first_errors = [{
                    "code": "STRUCTURED_COMPILE_INVALID", "location": "candidate_report",
                    "sentence": "", "evidence_codes": [],
                    "message": f"结构化候选无法安全编译：{str(exc)[:500]}",
                }]

            if validation is None or not validation.valid:
                run.status = "repairing"
                run.repair_count = 1
                run.validation_errors_json = json.dumps(
                    {"valid": False, "errors": first_errors, "warnings": [], "coverage": {}},
                    ensure_ascii=False,
                )
                self.db.commit()
                try:
                    if structured_v2:
                        candidate = self.analyzer.repair_structured_grounded_report(
                            packet, candidate, first_errors,
                            report.industry_name, report.company_name,
                        )
                        candidate = StructuredGroundedReportCandidate.model_validate(
                            candidate
                        ).model_dump()
                    else:
                        candidate = self.analyzer.repair_grounded_report(
                            packet, candidate, first_errors,
                            report.industry_name, report.company_name,
                        )
                        candidate = GroundedReportCandidate.model_validate(candidate).model_dump()
                except GroundedReportOutputError as exc:
                    candidate = {"raw_output": exc.raw_output[:12000]}
                    validation_dict = {
                        "valid": False,
                        "errors": [{
                            "code": "SCHEMA_INVALID_AFTER_REPAIR", "location": "candidate_report",
                            "sentence": "", "evidence_codes": [], "message": "修复输出仍未通过严格Schema校验。",
                        }],
                        "warnings": [], "coverage": {},
                        "repair_history": [{"errors": first_errors}],
                    }
                    return self._fail(run, candidate, validation_dict, "validation_failed")
                except ValidationError:
                    validation_dict = {
                        "valid": False,
                        "errors": [{
                            "code": "SCHEMA_INVALID_AFTER_REPAIR", "location": "candidate_report",
                            "sentence": "", "evidence_codes": [], "message": "修复输出仍未通过严格Schema校验。",
                        }],
                        "warnings": [], "coverage": {},
                        "repair_history": [{"errors": first_errors}],
                    }
                    return self._fail(run, candidate, validation_dict, "validation_failed")
                except ValueError as exc:
                    validation_dict = {
                        "valid": False,
                        "errors": [{
                            "code": "STRUCTURED_COMPILE_INVALID_AFTER_REPAIR",
                            "location": "candidate_report", "sentence": "",
                            "evidence_codes": [],
                            "message": f"修复后的结构化候选仍无法安全编译：{str(exc)[:500]}",
                        }],
                        "warnings": [], "coverage": {},
                        "repair_history": [{"errors": first_errors}],
                    }
                    return self._fail(run, candidate, validation_dict, "validation_failed")
                validation = (
                    validate_structured_grounded_report(candidate, packet).result
                    if structured_v2 else validate_citations(candidate, packet)
                )

            run.candidate_report_json = json.dumps(candidate, ensure_ascii=False)
            validation_dict = validation.to_dict()
            if run.repair_count:
                validation_dict["repair_history"] = [{"errors": first_errors}]
            run.validation_errors_json = json.dumps(validation_dict, ensure_ascii=False)
            run.citation_count = int(validation.coverage.get("citation_count", 0))
            run.cited_evidence_count = int(validation.coverage.get("cited_evidence_count", 0))
            run.uncited_sentence_count = int(validation.coverage.get("uncited_sentence_count", 0))
            run.completed_at = datetime.utcnow()
            if validation.valid:
                run.status = "validated"
                run.failure_code = None
            else:
                run.status = "failed"
                run.failure_code = "validation_failed"
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self.db.rollback()
            failed = self.db.get(IndustryGroundedReportRun, run.id)
            if failed:
                failed.status = "failed"
                failed.failure_code = "generation_failed"
                failed.validation_errors_json = json.dumps({
                    "valid": False,
                    "errors": [{
                        "code": "GENERATION_FAILED", "location": "grounded_run",
                        "sentence": "", "evidence_codes": [],
                        "message": "影子报告生成失败。",
                    }],
                    "warnings": [], "coverage": {},
                }, ensure_ascii=False)
                failed.completed_at = datetime.utcnow()
                self.db.commit()
            raise GroundedReportError("影子报告生成失败，正式报告未受影响") from exc

    def _fail(
        self, run: IndustryGroundedReportRun, candidate: Any,
        validation_dict: dict[str, Any], failure_code: str,
    ) -> IndustryGroundedReportRun:
        run.status = "failed"
        run.failure_code = failure_code
        run.candidate_report_json = json.dumps(candidate, ensure_ascii=False)
        run.validation_errors_json = json.dumps(validation_dict, ensure_ascii=False)
        run.completed_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(run)
        return run

    def list_runs(self, report_id: int) -> list[IndustryGroundedReportRun]:
        self._report(report_id)
        return self.db.query(IndustryGroundedReportRun).filter(
            IndustryGroundedReportRun.report_id == report_id
        ).order_by(IndustryGroundedReportRun.id.desc()).all()

    def get_run(self, report_id: int, run_id: int) -> Optional[IndustryGroundedReportRun]:
        self._report(report_id)
        return self.db.query(IndustryGroundedReportRun).filter(
            IndustryGroundedReportRun.report_id == report_id,
            IndustryGroundedReportRun.id == run_id,
        ).first()

    def promote(
        self, report_id: int, run_id: int, *, promotion_type: str,
        promotion_note: Optional[str] = None,
    ) -> IndustryReport:
        """Atomically promote a still-current validated shadow candidate."""
        report = self._report(report_id)
        run = self.get_run(report_id, run_id)
        if not run:
            raise GroundedPromotionError("RUN_NOT_VALIDATED", "候选运行不存在或不属于当前报告。")
        if (
            report.status == "completed" and report.grounded_run_id == run.id
            and report.citation_validation_status == "validated"
        ):
            return report
        if report.status not in {"draft", "failed", "running", "awaiting_approval"}:
            raise GroundedPromotionError(
                "REPORT_NOT_GENERATABLE", "当前报告状态不允许候选晋升。", "create report revision",
            )
        if promotion_type not in {"manual", "automatic"}:
            raise GroundedPromotionError("PROMOTION_VALIDATION_FAILED", "晋升类型无效。")
        if promotion_note and len(promotion_note) > 4000:
            raise GroundedPromotionError("PROMOTION_VALIDATION_FAILED", "晋升备注过长。")
        if run.status != "validated":
            raise GroundedPromotionError("RUN_NOT_VALIDATED", "只有validated候选才能晋升。")
        if run.prompt_version != GROUNDED_REPORT_PROMPT_VERSION:
            raise GroundedPromotionError("RUN_SNAPSHOT_STALE", "候选使用的Prompt版本已不受支持。")

        from app.services.grounded_readiness import check_grounded_readiness
        readiness = check_grounded_readiness(
            self.db, report_id, require_generatable_status=False,
        )
        if not readiness["ready"]:
            first = readiness["blocking_errors"][0]
            raise GroundedPromotionError(
                "RUN_SNAPSHOT_STALE", first["message"], first["next_step"],
            )
        packet = build_evidence_packet(self.db, report_id)
        if (
            run.evidence_snapshot_hash != packet["evidence_snapshot_hash"]
            or run.conflict_snapshot_hash != packet["conflict_snapshot_hash"]
        ):
            raise GroundedPromotionError(
                "RUN_SNAPSHOT_STALE", "候选证据或冲突快照已经变化，请重新生成。",
            )
        try:
            raw_candidate = json.loads(run.candidate_report_json or "null")
            candidate = GroundedReportCandidate.model_validate(raw_candidate).model_dump()
        except (json.JSONDecodeError, ValidationError) as exc:
            raise GroundedPromotionError(
                "PROMOTION_VALIDATION_FAILED", "候选报告Schema复核失败。",
            ) from exc
        validation = validate_citations(candidate, packet)
        if not validation.valid:
            raise GroundedPromotionError(
                "PROMOTION_VALIDATION_FAILED", "候选报告引用复核失败。",
            )
        if promotion_type == "manual" and not (promotion_note or "").strip():
            raise GroundedPromotionError(
                "PROMOTION_NOTE_REQUIRED", "人工晋升必须填写审批备注。", "enter an approval note",
            )

        safe_metadata = {
            "generation_mode": "grounded",
            "prompt_version": run.prompt_version,
            "grounded_run_id": run.id,
            "provider": run.provider,
            "model": run.model,
            "evidence_snapshot_hash": run.evidence_snapshot_hash,
            "conflict_snapshot_hash": run.conflict_snapshot_hash,
            "citation_validation_status": "validated",
            "promotion_type": promotion_type,
        }
        candidate["evidence_coverage"] = packet["coverage"]
        candidate["generation_metadata"] = safe_metadata
        from app.services.industry_analysis import build_report_chart_specs, report_json_to_html

        now = datetime.utcnow()
        report.report_json = json.dumps(candidate, ensure_ascii=False)
        report.report_html = report_json_to_html(candidate)
        _specs, report.chart_specs = build_report_chart_specs(candidate)
        report.source_manifest_json = json.dumps(
            [
                {
                    "evidence_code": item["evidence_code"],
                    "source_name": item["source_name"],
                    "source_origin": item["source_origin"],
                    "evidence_grade": item["evidence_grade"],
                    "locator": item["locator"],
                }
                for item in packet["evidence"]
            ],
            ensure_ascii=False,
        )
        report.status = "completed"
        report.error_message = None
        report.generation_mode = "grounded"
        report.grounded_run_id = run.id
        report.prompt_version = run.prompt_version
        report.evidence_snapshot_hash = run.evidence_snapshot_hash
        report.conflict_snapshot_hash = run.conflict_snapshot_hash
        report.citation_validation_status = "validated"
        report.promoted_at = now
        report.promotion_type = promotion_type
        report.promotion_note = promotion_note
        report.grounded_generation_metadata = json.dumps(safe_metadata, ensure_ascii=False)
        report.generation_config_json = json.dumps(
            {
                "generation_mode": "grounded",
                "prompt_version": run.prompt_version,
                "approval_required": promotion_type == "manual",
                "legacy_fallback_allowed": False,
            },
            ensure_ascii=False,
        )
        report.updated_at = now
        self.db.commit()
        self.db.refresh(report)
        return report
