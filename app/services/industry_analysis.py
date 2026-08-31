"""行业/授信分析独立模块。"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from html import escape
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.models import IndustryReport
from app.services.chart_generator import extract_and_build_charts, to_echarts_option
from app.services.data_source_service import (
    INDUSTRY_UPLOAD_ROOT,
    adopt_sources_into_library,
    append_industry_network_search_sources,
    build_industry_authoritative_text,
    clone_industry_sources,
    list_industry_sources,
    replace_report_sources_from_library,
)
from app.services.deepseek_analyzer import (
    GROUNDED_REPORT_PROMPT_VERSION,
    DeepSeekAnalyzer,
    LEGACY_INDUSTRY_PROMPT_VERSION,
)
from app.services.demo_industry_reports import (
    DEMO_FIXED_REPORT_PROMPT_VERSION,
    build_fixed_demo_report,
    demo_report_html,
    demo_source_list_html,
    supports_fixed_demo,
)
from app.services.grounded_readiness import check_grounded_readiness
from app.services.grounded_report import (
    GroundedPromotionError,
    GroundedReportError,
    GroundedReportService,
)
from app.services.mita_search import MitaSearchClient

logger = logging.getLogger(__name__)


class IndustryGenerationError(RuntimeError):
    def __init__(self, code: str, message: str, next_step: str) -> None:
        super().__init__(message)
        self.code = code
        self.next_step = next_step

# 制作与维护：DingJiaye
# 最近修改：2026-08-31 — 统一行业报告的章节结构、证据边界和授信风险传导逻辑。
GENERIC_TEMPLATE = """
【写作定位】
本报告供银行授信审查、贷后监测及风险复核使用。参照 IATA 航空行业与 Deloitte 能源行业年度展望的写法：
以资料中可核验事实为基础，先呈现行业变化，再说明对授信客户经营、现金流和偿债能力的传导，最后给出可执行的关注重点。

【推荐章节】
（三）授信客户行业方面的变动情况及原因
1、行业的发展现状及前景概述
（1）概述：交代资料覆盖期间、行业所处周期、宏观环境、供需与盈利的总体变化；不要脱离资料泛谈。
（2）核心市场与需求：按行业实际选择客运/货运、价格、产能、销量、客户、区域、订单或项目进度等维度；有同比、环比、预测或基准期时应清楚区分。
（3）经营与盈利：围绕收入、成本、毛利/利润、资本开支、自由现金流、杠杆或回报展开；每一个数字必须保留资料中的单位、币种、期间和预测属性。
（4）供应链、政策与技术：说明关键原材料、设备交付、监管、补贴、可持续发展或数字化因素如何影响成本、产能、项目进度或合规负担。
（5）行业前景及风险判断：按“已披露变化 → 影响渠道 → 对借款人可能影响 → 需持续核验事项”的逻辑逐项推导，避免仅罗列风险词。
2、授信关注重点：给出可用于贷前/贷后管理的量化指标、预警信号、资料缺口和核验动作；避免直接给出授信审批结论。

【质量标准】
1. 正式、克制、完整的中文银行授信研究文风；正文使用连续段落，标题层级清晰。
2. 事实、来源观点、预测与模型分析必须明确区分；“预计、计划、目标、可能”等不确定性不得写成既成事实。
3. 只使用已提供材料中的数据和主体，不得用常识补充数字、市场份额、政策内容或公司事实。
4. 资料存在冲突、口径差异、期间不一致或正文不足时，说明限制及待核验点，不自行择一认定。
5. 结论应具备行业针对性：解释变化的原因、传导路径、影响对象与时间维度，避免空泛表述如“需持续关注”。
"""


def _source_kind_label(source: Any) -> str:
    origin = str(getattr(source, "source_origin", None) or "")
    source_type = str(getattr(source, "source_type", None) or "")
    if origin == "network_search" or source_type == "network_search":
        return "网络搜索"
    if origin == "customer_url" or source_type == "url":
        return "网址"
    if origin == "customer_file" or source_type == "file":
        return "文件"
    return source_type or "数据源"


def _external_href(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if value.lower().startswith(("http://", "https://")):
        return value
    if value.startswith("//"):
        return "https:" + value
    return "https://" + value


def source_list_html(sources: Optional[list[Any]] = None) -> str:
    """按已保存数据源生成来源列表，不依赖模型是否输出该章节。"""
    parts = ['<section class="report-section report-source-list"><h2>来源列表</h2>']
    items = list(sources or [])
    if not items:
        parts.append('<p class="hint">本次生成未写入可用数据源。</p></section>')
        return "".join(parts)
    parts.append('<ol class="report-source-list-items">')
    for src in items:
        name = escape(str(getattr(src, "name", None) or "未命名来源"))
        kind = escape(_source_kind_label(src))
        href = _external_href(str(getattr(src, "url", None) or ""))
        if href:
            parts.append(
                f'<li><span class="source-kind">{kind}</span> '
                f'<a href="{escape(href)}" target="_blank" rel="noopener noreferrer">{name}</a></li>'
            )
        else:
            parts.append(f'<li><span class="source-kind">{kind}</span> {name}</li>')
    parts.append("</ol></section>")
    return "".join(parts)


def report_json_to_html(report: dict[str, Any], sources: Optional[list[Any]] = None) -> str:
    parts: list[str] = []
    title = escape(str(report.get("title") or "行业分析报告"))
    parts.append(f'<h1 class="report-title">{title}</h1>')
    if report.get("summary"):
        parts.append(f'<section class="report-section"><h2>执行摘要</h2><p>{escape(str(report["summary"]))}</p></section>')
    for sec in report.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        heading = escape(str(sec.get("heading") or ""))
        content = escape(str(sec.get("content") or "")).replace("\n", "<br/>")
        parts.append(f'<section class="report-section"><h2>{heading}</h2><p>{content}</p></section>')
    if report.get("risk_outlook"):
        parts.append(
            f'<section class="report-section"><h2>风险展望</h2><p>{escape(str(report["risk_outlook"]))}</p></section>'
        )
    if sources is not None:
        parts.append(source_list_html(sources))
    return "\n".join(parts)


def build_report_chart_specs(
    report: dict[str, Any], source_text: str = "",
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Build existing chart payloads without requiring source text in grounded mode."""
    combined_text = source_text + "\n" + json.dumps(report, ensure_ascii=False)
    specs, chart_json = extract_and_build_charts(combined_text)
    metrics = report.get("key_metrics") or []
    if metrics and not specs:
        labels = [str(item.get("name", "")) for item in metrics if isinstance(item, dict)]
        values = []
        for item in metrics:
            if not isinstance(item, dict):
                continue
            try:
                values.append(float(str(item.get("value", "0")).replace("%", "")))
            except ValueError:
                values.append(0)
        if labels and values:
            spec = {
                "id": "chart_metrics", "type": "bar", "title": "关键指标",
                "labels": labels, "series": [{"name": "指标值", "data": values}],
            }
            specs = [spec]
            chart_json = json.dumps(
                [{"id": spec["id"], "option": to_echarts_option(spec)}], ensure_ascii=False,
            )
    return specs, chart_json


class IndustryAnalysisService:
    def __init__(
        self,
        db: Session,
        deepseek: Optional[DeepSeekAnalyzer] = None,
        mita: Optional[MitaSearchClient] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        # 行业报告统一使用 DeepSeek；测试仍可注入兼容分析器。
        self.deepseek = deepseek or DeepSeekAnalyzer()
        self.mita = mita or MitaSearchClient()
        self.settings = settings or get_settings()

    def create_draft(
        self,
        industry_name: str,
        company_name: Optional[str] = None,
        supplement_search: bool = True,
    ) -> IndustryReport:
        industry_name = industry_name.strip()
        if not industry_name:
            raise ValueError("行业名称不能为空")
        company_name = company_name.strip() if company_name else None
        row = IndustryReport(
            industry_name=industry_name,
            company_name=company_name,
            status="draft",
            supplement_search=supplement_search,
            version=1,
        )
        self.db.add(row)
        self.db.flush()
        row.root_report_id = row.id
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_or_create_source_library(self, industry_name: str) -> IndustryReport:
        """Return the hidden industry source library, creating it if needed."""
        industry_name = " ".join((industry_name or "").split()) or "行业资料库"
        row = (
            self.db.query(IndustryReport)
            .filter(IndustryReport.is_source_library.is_(True))
            .order_by(IndustryReport.id.asc())
            .first()
        )
        if row:
            changed = False
            if not row.library_saved:
                row.library_saved = True
                changed = True
            if row.industry_name != industry_name:
                row.industry_name = industry_name
                changed = True
            if changed:
                row.updated_at = datetime.utcnow()
                self.db.commit()
                self.db.refresh(row)
        else:
            row = IndustryReport(
                industry_name=industry_name,
                report_name="行业资料库",
                status="draft",
                supplement_search=False,
                version=1,
                library_saved=True,
                is_source_library=True,
            )
            self.db.add(row)
            self.db.flush()
            row.root_report_id = row.id
            self.db.commit()
            self.db.refresh(row)
        try:
            adopt_sources_into_library(self.db, row.id)
        except Exception:
            self.db.rollback()
            logger.exception("归并行业资料库失败: industry=%s report_id=%s", industry_name, row.id)
        return row

    def fork_report(self, report_id: int) -> IndustryReport:
        parent = self.get_report(report_id)
        if not parent:
            raise ValueError("报告不存在")
        if parent.status != "completed":
            raise ValueError("只有已完成的报告可以创建新版")
        root_id = parent.root_report_id or parent.id
        latest_version = (
            self.db.query(IndustryReport.version)
            .filter(IndustryReport.root_report_id == root_id)
            .order_by(IndustryReport.version.desc())
            .first()
        )
        version = (latest_version[0] if latest_version else parent.version) + 1
        row = IndustryReport(
            parent_report_id=parent.id,
            root_report_id=root_id,
            version=version,
            report_name=parent.report_name,
            industry_name=parent.industry_name,
            company_name=parent.company_name,
            status="draft",
            supplement_search=parent.supplement_search,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def save_to_industry_library(self, report_id: int) -> IndustryReport:
        """Mark this report's source snapshots reusable for future same-industry drafts."""
        row = self.get_report(report_id)
        if not row:
            raise ValueError("报告不存在")
        if not list_industry_sources(self.db, self.get_or_create_source_library(row.industry_name).id):
            raise ValueError("请先添加至少一条来源，再保存到行业资料库")
        row.library_saved = True
        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row

    def rename_report(self, report_id: int, report_name: str) -> IndustryReport:
        row = self.get_report(report_id)
        if not row:
            raise ValueError("报告不存在")
        normalized = " ".join(report_name.split())
        if not normalized:
            raise ValueError("报告名称不能为空")
        if len(normalized) > 256:
            raise ValueError("报告名称不能超过 256 个字符")
        row.report_name = normalized
        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row

    def revise_completed_report(self, report_id: int, instruction: str) -> IndustryReport:
        """Create a new version from a completed report and apply a user-requested revision."""
        parent = self.get_report(report_id)
        if not parent:
            raise ValueError("报告不存在")
        if parent.status != "completed":
            raise ValueError("只有已完成的报告可以修改")
        try:
            existing_report = json.loads(parent.report_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("当前报告缺少可修订的结构化正文") from exc
        if not isinstance(existing_report, dict) or not existing_report:
            raise ValueError("当前报告缺少可修订的结构化正文")
        normalized_instruction = " ".join(str(instruction or "").split())
        if len(normalized_instruction) < 2:
            raise ValueError("请输入具体的修改要求")

        draft = self.fork_report(report_id)
        library = self.get_or_create_source_library(parent.industry_name)
        replace_report_sources_from_library(self.db, draft.id, library.id)
        try:
            revised = self.deepseek.revise_industry_report(
                existing_report, normalized_instruction, parent.industry_name,
            )
            source_text, _ = build_industry_authoritative_text(self.db, library.id)
            _, chart_json = build_report_chart_specs(revised, source_text)
            draft.report_json = json.dumps(revised, ensure_ascii=False)
            draft.report_html = report_json_to_html(
                revised, sources=list_industry_sources(self.db, library.id),
            )
            draft.chart_specs = chart_json
            draft.status = "completed"
            draft.generation_mode = "revision"
            draft.prompt_version = "deepseek-report-revision-v1"
            draft.generation_config_json = json.dumps(
                {"revision_instruction": normalized_instruction, "parent_report_id": parent.id},
                ensure_ascii=False,
            )
            draft.updated_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(draft)
            return draft
        except Exception:
            draft.status = "failed"
            draft.error_message = "报告修改失败"
            draft.updated_at = datetime.utcnow()
            self.db.commit()
            raise

    def generate_report(self, report_id: int) -> IndustryReport:
        row = self.get_report(report_id)
        if not row:
            raise ValueError("报告不存在")
        if row.is_source_library:
            raise ValueError("请先新建报告。资料库中的来源会在生成时带入。")
        # Source edits on a completed report stay on that library; the next
        # generation copies them into a new version and leaves the current body unchanged.
        if row.status == "completed":
            row = self.fork_report(report_id)
            report_id = row.id
        # Modified by DingJiaye: 2026-08-31 — 不再使用航空、能源/电力固定演示稿；
        # 所有行业均基于当前资料库和用户勾选来源进入常规分析流程。
        library = self.get_or_create_source_library(row.industry_name)
        replace_report_sources_from_library(self.db, report_id, library.id)
        mode = self.settings.industry_report_generation_mode
        logger.info("行业正式报告生成模式: %s report_id=%s", mode, report_id)
        if mode == "grounded":
            return self._generate_grounded_report(report_id)
        return self._generate_legacy_report(report_id)

    def _generate_fixed_demo_report(self, report_id: int) -> IndustryReport:
        row = self.get_report(report_id)
        if not row:
            raise ValueError("报告不存在")
        report = build_fixed_demo_report(row.industry_name)
        now = datetime.utcnow()
        row.report_json = json.dumps(report, ensure_ascii=False)
        # Modified by DingJiaye: 2026-08-26 — 固定演示稿逐段按确认原文渲染，
        # 不经模型摘要、改写或压缩；航空数据表保持为 HTML 表格。
        row.report_html = demo_report_html(report) + demo_source_list_html(report)
        row.chart_specs = None
        row.source_manifest_json = json.dumps(
            report.get("demo_sources") or [], ensure_ascii=False,
        )
        row.generation_config_json = json.dumps(
            {
                "generation_mode": "demo_fixed",
                "fixed_materials": True,
                "note": "航空、能源/电力现场演示固定样稿；未调用外部检索或大模型。",
            },
            ensure_ascii=False,
        )
        row.status = "completed"
        row.generation_mode = "demo_fixed"
        row.grounded_run_id = None
        row.prompt_version = DEMO_FIXED_REPORT_PROMPT_VERSION
        row.evidence_snapshot_hash = None
        row.conflict_snapshot_hash = None
        row.citation_validation_status = "not_applicable"
        row.promoted_at = None
        row.promotion_type = None
        row.promotion_note = None
        row.grounded_generation_metadata = json.dumps(
            {"generation_mode": "demo_fixed", "fixed_materials": True},
            ensure_ascii=False,
        )
        row.error_message = None
        row.updated_at = now
        self.db.commit()
        self.db.refresh(row)
        return row

    def _generate_legacy_report(self, report_id: int) -> IndustryReport:
        changed = (
            self.db.query(IndustryReport)
            .filter(
                IndustryReport.id == report_id,
                IndustryReport.status.in_(["draft", "failed", "awaiting_approval"]),
            )
            .update(
                {
                    IndustryReport.status: "running",
                    IndustryReport.error_message: None,
                    IndustryReport.updated_at: datetime.utcnow(),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        if not changed:
            row = self.get_report(report_id)
            if not row:
                raise ValueError("报告不存在")
            raise ValueError("该报告当前不可生成；已完成报告请先创建新版")
        row = self.get_report(report_id)
        assert row is not None

        try:
            network_query = ""
            network_added = 0
            network_error = ""
            network_provider = ""
            if row.supplement_search:
                network_query = (
                    f"{row.industry_name} {row.company_name or ''} 行业分析 授信 信用风险"
                ).strip()
                try:
                    response = self.mita.search(query=network_query, max_results=8)
                    network_provider = getattr(response, "provider", None) or "mita"
                    translator = getattr(self.deepseek, "translate_network_source_to_chinese", None)
                    network_added = len(
                        append_industry_network_search_sources(
                            self.db,
                            self.get_or_create_source_library(row.industry_name).id,
                            response.items,
                            translator=translator if callable(translator) else None,
                            require_translation=False,
                        )
                    )
                except Exception as exc:
                    network_error = " ".join(str(exc).split())
                    if len(network_error) > 240:
                        network_error = network_error[:240] + "…"
                    logger.warning("行业分析网络补充检索失败: %s", exc)

            library = self.get_or_create_source_library(row.industry_name)
            replace_report_sources_from_library(self.db, row.id, library.id)
            authority_text, manifest = build_industry_authoritative_text(self.db, library.id)
            if not manifest:
                raise IndustryGenerationError(
                    "no_selected_sources",
                    "尚未选择含正文的数据源，无法生成报告",
                    "请在第三步勾选至少一条已成功解析的数据源，或先添加数据源。",
                )
            network_in_manifest = sum(
                1 for item in manifest if item.get("source_type") == "network_search"
            )
            row.source_manifest_json = json.dumps(manifest, ensure_ascii=False)
            row.generation_config_json = json.dumps(
                {
                    "supplement_search": row.supplement_search,
                    "network_search_query": network_query,
                    "network_search_added": network_added,
                    "network_search_sources": network_in_manifest,
                    "network_search_error": network_error or None,
                    "network_search_provider": network_provider or None,
                    "authority_max_chars": 100_000,
                    "source_count": len(manifest),
                    "generation_mode": "legacy",
                    "prompt_version": LEGACY_INDUSTRY_PROMPT_VERSION,
                },
                ensure_ascii=False,
            )
            self.db.commit()

            raw_input = (
                f"=== 当前报告专属数据源 ===\n{authority_text or '（无可用数据源，请使用通用模板）'}\n\n"
                f"=== 分析模板 ===\n{GENERIC_TEMPLATE}\n\n"
            )

            report = self.deepseek.analyze_industry(
                raw_input,
                industry_name=row.industry_name,
                company_name=row.company_name,
                context={
                    "supplement_used": network_in_manifest > 0,
                    "network_search_added": network_added,
                    "report_id": row.id,
                    "source_manifest": manifest,
                },
            )

            specs, chart_json = build_report_chart_specs(report, authority_text)

            report_json = json.dumps(report, ensure_ascii=False)
            report_html = report_json_to_html(report, sources=list_industry_sources(self.db, row.id))
            now = datetime.utcnow()

            row.report_json = report_json
            row.report_html = report_html
            row.chart_specs = chart_json
            row.status = "completed"
            row.generation_mode = "legacy"
            row.grounded_run_id = None
            row.prompt_version = LEGACY_INDUSTRY_PROMPT_VERSION
            row.evidence_snapshot_hash = None
            row.conflict_snapshot_hash = None
            row.citation_validation_status = "not_applicable"
            row.promoted_at = None
            row.promotion_type = None
            row.promotion_note = None
            row.grounded_generation_metadata = json.dumps(
                {
                    "generation_mode": "legacy",
                    "prompt_version": LEGACY_INDUSTRY_PROMPT_VERSION,
                    "citation_validation_status": "not_applicable",
                },
                ensure_ascii=False,
            )
            row.updated_at = now
            self.db.commit()
            self.db.refresh(row)
            return row
        except Exception as exc:
            logger.exception("行业分析失败: report_id=%s", report_id)
            row.status = "failed"
            row.error_message = str(exc)
            row.updated_at = datetime.utcnow()
            self.db.commit()
            raise

    def _generate_grounded_report(self, report_id: int) -> IndustryReport:
        readiness = check_grounded_readiness(self.db, report_id)
        if not readiness["ready"]:
            first = readiness["blocking_errors"][0]
            raise IndustryGenerationError(first["code"], first["message"], first["next_step"])

        changed = (
            self.db.query(IndustryReport)
            .filter(
                IndustryReport.id == report_id,
                IndustryReport.status.in_(["draft", "failed"]),
            )
            .update(
                {
                    IndustryReport.status: "running",
                    IndustryReport.error_message: None,
                    IndustryReport.updated_at: datetime.utcnow(),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        if not changed:
            raise IndustryGenerationError(
                "REPORT_NOT_GENERATABLE",
                "该报告当前不可生成；已完成报告请先创建新版。",
                "create report revision",
            )
        row = self.get_report(report_id)
        assert row is not None
        grounded = GroundedReportService(self.db, analyzer=self.deepseek)
        try:
            run = grounded.generate(report_id)
            if run.status != "validated":
                raise IndustryGenerationError(
                    "GROUNDING_VALIDATION_FAILED",
                    "证据约束候选未通过引用校验，且未回退legacy流程。",
                    "grounded generation",
                )
            audit = {
                "generation_mode": "grounded",
                "prompt_version": run.prompt_version,
                "grounded_run_id": run.id,
                "evidence_snapshot_hash": run.evidence_snapshot_hash,
                "conflict_snapshot_hash": run.conflict_snapshot_hash,
                "citation_validation_status": "validated",
                "approval_required": bool(self.settings.grounded_report_require_approval),
            }
            if self.settings.grounded_report_require_approval:
                row.generation_mode = "grounded"
                row.grounded_run_id = run.id
                row.prompt_version = run.prompt_version
                row.evidence_snapshot_hash = run.evidence_snapshot_hash
                row.conflict_snapshot_hash = run.conflict_snapshot_hash
                row.citation_validation_status = "validated"
                row.grounded_generation_metadata = json.dumps(audit, ensure_ascii=False)
                row.generation_config_json = json.dumps(
                    {
                        "generation_mode": "grounded",
                        "prompt_version": GROUNDED_REPORT_PROMPT_VERSION,
                        "approval_required": True,
                        "legacy_fallback_allowed": False,
                    },
                    ensure_ascii=False,
                )
                row.status = "awaiting_approval"
                row.error_message = None
                row.updated_at = datetime.utcnow()
                self.db.commit()
                self.db.refresh(row)
                return row
            return grounded.promote(
                report_id, run.id, promotion_type="automatic",
                promotion_note="配置允许validated候选自动晋升",
            )
        except IndustryGenerationError as exc:
            row.status = "failed"
            row.error_message = f"{exc.code}: {exc}"
            row.updated_at = datetime.utcnow()
            self.db.commit()
            raise
        except GroundedPromotionError as exc:
            row.status = "failed"
            row.error_message = f"{exc.code}: {exc}"
            row.updated_at = datetime.utcnow()
            self.db.commit()
            raise IndustryGenerationError(exc.code, str(exc), exc.next_step) from exc
        except GroundedReportError as exc:
            row.status = "failed"
            row.error_message = "GROUNDING_VALIDATION_FAILED: grounded generation failed"
            row.updated_at = datetime.utcnow()
            self.db.commit()
            raise IndustryGenerationError(
                "GROUNDING_VALIDATION_FAILED", str(exc), "grounded generation",
            ) from exc
        except Exception as exc:
            row.status = "failed"
            row.error_message = "GROUNDING_VALIDATION_FAILED: grounded generation failed"
            row.updated_at = datetime.utcnow()
            self.db.commit()
            raise IndustryGenerationError(
                "GROUNDING_VALIDATION_FAILED", "证据约束生成失败，未执行legacy回退。",
                "grounded generation",
            ) from exc

    def run_analysis(
        self,
        industry_name: str,
        company_name: Optional[str] = None,
        supplement_search: bool = True,
    ) -> IndustryReport:
        """兼容旧调用：创建无共享数据源的草稿并立即生成。"""
        row = self.create_draft(industry_name, company_name, supplement_search)
        return self.generate_report(row.id)

    def get_report(self, report_id: int) -> Optional[IndustryReport]:
        return self.db.query(IndustryReport).filter(IndustryReport.id == report_id).first()

    def delete_report(self, report_id: int) -> bool:
        """删除一份历史报告及其级联来源、切片和证据记录。"""
        row = self.get_report(report_id)
        if not row:
            return False
        if row.is_source_library:
            raise ValueError("行业资料库不能删除")
        if row.status == "running":
            raise ValueError("报告正在生成，暂不能删除")
        self.db.delete(row)
        self.db.commit()
        shutil.rmtree(INDUSTRY_UPLOAD_ROOT / str(report_id), ignore_errors=True)
        return True

    def list_reports(self, limit: int = 20) -> list[IndustryReport]:
        return (
            self.db.query(IndustryReport)
            .filter(IndustryReport.is_source_library.is_(False))
            .order_by(IndustryReport.created_at.desc())
            .limit(limit)
            .all()
        )
