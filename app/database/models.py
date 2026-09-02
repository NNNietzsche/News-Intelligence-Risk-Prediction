"""SQLAlchemy ORM 模型。"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DomainWhitelist(Base):
    __tablename__ = "domain_whitelist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    module_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DomainBlacklist(Base):
    __tablename__ = "domain_blacklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ModuleDataSource(Base):
    """权威数据源（文件/模板/网址）。

    - entity_id 为空：全站共用（风险日报等）
    - entity_id 有值：主体评估页当前主体的专属参考素材
    """

    __tablename__ = "module_data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    module_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    entity_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("target_entities.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # file, url, template
    file_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    original_filename: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    extracted_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class IndustryDataSource(Base):
    """单份深度研报独占的数据源快照。"""

    __tablename__ = "industry_data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    copied_from_source_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("industry_data_sources.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    original_filename: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    extracted_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A report is generated only from explicitly selected source snapshots.
    # Defaulting to True keeps historical reports reproducible after migration.
    is_selected: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Source registry metadata. Existing rows intentionally remain nullable and are
    # treated as legacy/unstructured until explicitly reprocessed.
    raw_content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    extracted_text_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_origin: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    source_publisher: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    published_at: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    retrieved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_full_text: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_truncated: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    parse_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    parse_warning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    used_ocr: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slide_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sheet_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    evidence_grade: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    report: Mapped["IndustryReport"] = relationship(back_populates="sources")
    chunks: Mapped[list["IndustrySourceChunk"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )


class IndustrySourceChunk(Base):
    """带原文逻辑位置的研报信源文本切片。"""

    __tablename__ = "industry_source_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "chunk_index", name="uq_industry_source_chunk_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("industry_data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[str] = mapped_column(String(512), nullable=False)
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slide_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    cell_range: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    row_range: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    paragraph_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    table_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    table_row_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    source: Mapped["IndustryDataSource"] = relationship(back_populates="chunks")


class IndustryEvidenceExtractionRun(Base):
    """Auditable evidence extraction attempt for one report source snapshot."""

    __tablename__ = "industry_evidence_extraction_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id_scope: Mapped[Optional[int]] = mapped_column(
        ForeignKey("industry_data_sources.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    extractor_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    total_sources: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    needs_review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    cards: Mapped[list["IndustryEvidenceCard"]] = relationship(
        back_populates="extraction_run", cascade="all, delete-orphan", passive_deletes=True
    )


class IndustryEvidenceCard(Base):
    """One atomic, source-bound claim with deterministic verification results."""

    __tablename__ = "industry_evidence_cards"
    __table_args__ = (
        UniqueConstraint("report_id", "evidence_code", name="uq_industry_evidence_code"),
        UniqueConstraint("extraction_run_id", "dedupe_key", name="uq_industry_evidence_run_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_code: Mapped[str] = mapped_column(String(32), nullable=False)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("industry_data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("industry_source_chunks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("industry_evidence_extraction_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    locator: Mapped[str] = mapped_column(String(512), nullable=False)
    original_quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quote_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    normalized_claim: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subject: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    metric_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    raw_value: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    normalized_value: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    value_multiplier: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    period: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    as_of_date: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    speaker: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    importance_score: Mapped[int] = mapped_column(Integer, nullable=False)
    importance_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    risk_tags: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    extraction_confidence: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    validation_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    verification_scope: Mapped[str] = mapped_column(String(32), default="source_match", nullable=False)
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_origin: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    evidence_grade: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    extraction_run: Mapped["IndustryEvidenceExtractionRun"] = relationship(back_populates="cards")


class IndustryConflictDetectionRun(Base):
    """Deterministic conflict detection over an immutable evidence snapshot."""

    __tablename__ = "industry_conflict_detection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    eligible_evidence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    compared_group_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    conflicts: Mapped[list["IndustryEvidenceConflict"]] = relationship(
        back_populates="detection_run", cascade="all, delete-orphan", passive_deletes=True
    )


class IndustryEvidenceConflict(Base):
    __tablename__ = "industry_evidence_conflicts"
    __table_args__ = (
        UniqueConstraint("report_id", "conflict_code", name="uq_industry_conflict_code"),
        UniqueConstraint("detection_run_id", "dedupe_key", name="uq_industry_conflict_run_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conflict_code: Mapped[str] = mapped_column(String(32), nullable=False)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detection_run_id: Mapped[int] = mapped_column(
        ForeignKey("industry_conflict_detection_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conflict_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    subject_key: Mapped[str] = mapped_column(String(512), nullable=False)
    metric_key: Mapped[str] = mapped_column(String(256), nullable=False)
    period_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    currency_key: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    dimension_key: Mapped[str] = mapped_column(String(32), nullable=False)
    base_unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(32), default="open", nullable=False, index=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    selected_evidence_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    detection_run: Mapped["IndustryConflictDetectionRun"] = relationship(back_populates="conflicts")
    members: Mapped[list["IndustryEvidenceConflictMember"]] = relationship(
        back_populates="conflict", cascade="all, delete-orphan", passive_deletes=True
    )


class IndustryEvidenceConflictMember(Base):
    __tablename__ = "industry_evidence_conflict_members"
    __table_args__ = (
        UniqueConstraint("conflict_id", "evidence_card_id", name="uq_industry_conflict_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conflict_id: Mapped[int] = mapped_column(
        ForeignKey("industry_evidence_conflicts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_card_id: Mapped[int] = mapped_column(
        ForeignKey("industry_evidence_cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_code: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    comparison_value: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    comparison_unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_origin: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    evidence_grade: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    member_role: Mapped[str] = mapped_column(String(32), default="compared", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    conflict: Mapped["IndustryEvidenceConflict"] = relationship(back_populates="members")
    evidence_card: Mapped["IndustryEvidenceCard"] = relationship()


class IndustryGroundedReportRun(Base):
    """Shadow-mode report candidate generated only from a gated evidence packet."""

    __tablename__ = "industry_grounded_report_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conflict_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    failure_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    candidate_report_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    validation_errors_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    repair_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    citation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cited_evidence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uncited_sentence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class IndustryAnalysisReport(Base):
    """行业/授信分析报告（与常规日报解耦）。"""

    __tablename__ = "industry_analysis_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    industry_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    report_html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    report_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chart_specs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class SearchLog(Base):
    __tablename__ = "search_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    module_code: Mapped[str] = mapped_column(String(8), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    domains_whitelist: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    domains_blacklist: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    entries: Mapped[list["DailyRiskEntry"]] = relationship(back_populates="search_log")


class ReportRun(Base):
    __tablename__ = "report_runs"
    __table_args__ = (
        UniqueConstraint(
            "report_date",
            "module_code",
            "window_hours",
            name="uq_report_run_date_module_window",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    module_code: Mapped[str] = mapped_column(String(8), nullable=False)
    # 采集时效窗（24 / 168）；与 news_articles.window_hours 对齐
    window_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    entry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # collect | analyze | publish | done
    phase: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    funnel_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    kept_previous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PipelineJob(Base):
    """异步流水线任务（前端轮询）。"""

    __tablename__ = "pipeline_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    module_codes: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list
    window_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    results_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    funnel_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 任务启动时冻结的权威数据源等快照；运行中上传/删除不影响本次采集
    snapshot_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PipelineArtifact(Base):
    """collect 阶段中间落盘，analyze 可独立重试。"""

    __tablename__ = "pipeline_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    module_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)  # collect | analyze
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class LlmResponseCache(Base):
    """DeepSeek 结构化结果缓存。"""

    __tablename__ = "llm_response_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    module_code: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ContentFingerprint(Base):
    """已发布内容指纹，跨次运行去重。"""

    __tablename__ = "content_fingerprints"
    __table_args__ = (
        UniqueConstraint("module_code", "fingerprint", name="uq_content_fp_module"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    module_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    report_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DailyRiskEntry(Base):
    __tablename__ = "daily_risk_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    module_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    country_or_region: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_entity: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    related_company: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    risk_category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    impact_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    source_title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    pillar_or_topic: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    structured_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    search_log_id: Mapped[Optional[int]] = mapped_column(ForeignKey("search_logs.id"), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    # 采集时效窗：24=近24小时，168=近7×24小时
    window_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    search_log: Mapped[Optional[SearchLog]] = relationship(back_populates="entries")


# ---------------------------------------------------------------------------
# 切片 2：三页业务表（与旧表共存，同一数据库）
# ---------------------------------------------------------------------------


class NewsArticle(Base):
    """风险日报资讯（中东日报 / 日本方面内容 / 每日宏观与市场情报）。"""

    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    module_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # 采集时效窗：24=近24小时，168=近7×24小时；与日报副分界面隔离
    window_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False, index=True)
    category_tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    country_or_region: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_entity: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    related_company: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    risk_category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    impact_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    source_title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    structured_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    legacy_entry_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("daily_risk_entries.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TargetEntity(Base):
    """主体评估 — 重点监控主体。"""

    __tablename__ = "target_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    aliases: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    region: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    monitor_status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    credit_level: Mapped[str] = mapped_column(String(16), default="正常", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    risks: Mapped[list["EntityRisk"]] = relationship(back_populates="entity")
    credit_updates: Mapped[list["CreditUpdate"]] = relationship(back_populates="entity")


class EntityRisk(Base):
    """主体风险事件（司法/经营/舆情等）。"""

    __tablename__ = "entity_risks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("target_entities.id"), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    risk_category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    impact_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    source_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    related_company: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    provenance: Mapped[str] = mapped_column(String(16), default="real", nullable=False, index=True)
    relevance: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    news_importance: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    sentiment_direction: Mapped[str] = mapped_column(
        String(16), default="unknown", nullable=False
    )
    credit_impact: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )
    rule_version: Mapped[str] = mapped_column(
        String(32), default="entity-signal-v1", nullable=False
    )
    structured_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    legacy_entry_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("daily_risk_entries.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    entity: Mapped["TargetEntity"] = relationship(back_populates="risks")


# Modified by DingJiaye: 2026-09-01 — 将高风险事件纳入可追踪的处置闭环，
# 保留负责人、期限、核验结论及处置留痕，避免风险只停留在页面提示。
class RiskReviewTask(Base):
    """主体风险事件的核验与处置任务。"""

    __tablename__ = "risk_review_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("target_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_risk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("entity_risks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="风险", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="待核验", nullable=False, index=True)
    assignee: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    verification_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    disposition: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


# Modified by DingJiaye: 2026-09-01 — 可维护的客户关联图谱数据，支持把风险沿股东、
# 融资银行、供应商、关联方及国家地区关系链进行人工追踪。
class EntityRelationship(Base):
    """监测主体与外部实体之间的可核验关系。"""

    __tablename__ = "entity_relationships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("target_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_name: Mapped[str] = mapped_column(String(256), nullable=False)
    related_type: Mapped[str] = mapped_column(String(64), nullable=False, default="关联方")
    relationship_type: Mapped[str] = mapped_column(String(128), nullable=False, default="关联关系")
    ownership_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    country_or_region: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    risk_signal: Mapped[str] = mapped_column(String(16), default="中性", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


# Modified by DingJiaye: 2026-09-01 — 支持用户导入/维护替代原先配置文件中的静态财务数字。
class EntityFinancialRecord(Base):
    """主体财务指标的手工或 Excel 导入记录。"""

    __tablename__ = "entity_financial_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("target_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    statement_type: Mapped[str] = mapped_column(String(32), default="context", nullable=False, index=True)
    period: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    metric_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    metric_label: Mapped[str] = mapped_column(String(256), nullable=False)
    value: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class CreditUpdate(Base):
    """公开信息预警灯号变化日志：正常 | 关注 | 预警 | 高风险。"""

    __tablename__ = "credit_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("target_entities.id"), nullable=False, index=True)
    previous_level: Mapped[str] = mapped_column(String(16), nullable=False)
    new_level: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger_risk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("entity_risks.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    entity: Mapped["TargetEntity"] = relationship(back_populates="credit_updates")


class RiskAlertDelivery(Base):
    """高等级事件的外部通知投递审计记录。"""

    __tablename__ = "risk_alert_deliveries"
    __table_args__ = (
        UniqueConstraint("entity_risk_id", "channel", name="uq_risk_alert_delivery_event_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_risk_id: Mapped[int] = mapped_column(
        ForeignKey("entity_risks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("target_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AlertChannelConfig(Base):
    """外部预警渠道配置；敏感配置以加密 JSON 保存。"""

    __tablename__ = "alert_channel_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    encrypted_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

class IndustryReport(Base):
    """深度研报 — 行业/企业长篇结构化报告。"""

    __tablename__ = "industry_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_report_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    root_report_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("industry_reports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    report_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    industry_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    supplement_search: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Explicit opt-in for reusing this report's sources in the same industry.
    library_saved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Hidden per-industry holder so sources can be uploaded before any report exists.
    is_source_library: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    report_html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    report_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chart_specs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_manifest_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generation_config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generation_mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    grounded_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("industry_grounded_report_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prompt_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence_snapshot_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    conflict_snapshot_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    citation_validation_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    promoted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    promotion_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    promotion_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    grounded_generation_metadata: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    legacy_report_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("industry_analysis_reports.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    sources: Mapped[list["IndustryDataSource"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", passive_deletes=True
    )
    evidence_runs: Mapped[list["IndustryEvidenceExtractionRun"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )
    evidence_cards: Mapped[list["IndustryEvidenceCard"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )
    conflict_runs: Mapped[list["IndustryConflictDetectionRun"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )
    evidence_conflicts: Mapped[list["IndustryEvidenceConflict"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )
    grounded_report_runs: Mapped[list["IndustryGroundedReportRun"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True,
        foreign_keys="IndustryGroundedReportRun.report_id",
    )
