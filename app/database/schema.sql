-- 企业风险情报日报系统 — SQLite  schema
-- 与 SQLAlchemy 模型对应，可用于手工初始化或审计

CREATE TABLE IF NOT EXISTS domain_whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain VARCHAR(255) NOT NULL UNIQUE,
    module_code VARCHAR(8),
    note TEXT,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS domain_blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain VARCHAR(255) NOT NULL UNIQUE,
    reason TEXT,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS search_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_code VARCHAR(8) NOT NULL,
    query_text TEXT NOT NULL,
    domains_whitelist TEXT,
    domains_blacklist TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    error_message TEXT,
    raw_response TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS report_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date DATE NOT NULL,
    module_code VARCHAR(8) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    started_at DATETIME,
    finished_at DATETIME,
    entry_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    UNIQUE(report_date, module_code)
);

CREATE TABLE IF NOT EXISTS daily_risk_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date DATE NOT NULL,
    module_code VARCHAR(8) NOT NULL,
    country_or_region VARCHAR(128),
    target_entity VARCHAR(256),
    title VARCHAR(512) NOT NULL,
    related_company VARCHAR(256),
    risk_category VARCHAR(128),
    risk_level VARCHAR(16) NOT NULL,
    summary TEXT NOT NULL,
    impact_analysis TEXT,
    source_url VARCHAR(1024),
    source_title VARCHAR(512),
    pillar_or_topic VARCHAR(128),
    structured_json TEXT,
    search_log_id INTEGER REFERENCES search_logs(id),
    published_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_entries_report_date ON daily_risk_entries(report_date);
CREATE INDEX IF NOT EXISTS idx_entries_module ON daily_risk_entries(module_code);
CREATE INDEX IF NOT EXISTS idx_search_logs_module ON search_logs(module_code);
CREATE INDEX IF NOT EXISTS idx_report_runs_date ON report_runs(report_date);

-- 切片 2：三页业务表
CREATE TABLE IF NOT EXISTS news_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date DATE NOT NULL,
    module_code VARCHAR(8) NOT NULL,
    category_tag VARCHAR(128),
    country_or_region VARCHAR(128),
    target_entity VARCHAR(256),
    title VARCHAR(512) NOT NULL,
    related_company VARCHAR(256),
    risk_category VARCHAR(128),
    risk_level VARCHAR(16) NOT NULL,
    summary TEXT NOT NULL,
    impact_analysis TEXT,
    source_url VARCHAR(1024),
    source_title VARCHAR(512),
    structured_json TEXT,
    published_at DATETIME,
    legacy_entry_id INTEGER REFERENCES daily_risk_entries(id),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS target_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(256) NOT NULL UNIQUE,
    display_name VARCHAR(256),
    aliases TEXT,
    industry VARCHAR(128),
    region VARCHAR(128),
    monitor_status VARCHAR(32) NOT NULL DEFAULT 'active',
    credit_level VARCHAR(16) NOT NULL DEFAULT '正常',
    notes TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entity_risks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES target_entities(id),
    report_date DATE NOT NULL,
    title VARCHAR(512) NOT NULL,
    risk_category VARCHAR(128),
    risk_level VARCHAR(16) NOT NULL,
    summary TEXT NOT NULL,
    impact_analysis TEXT,
    source_url VARCHAR(1024),
    source_name VARCHAR(256),
    published_at DATETIME,
    related_company VARCHAR(256),
    provenance VARCHAR(16) NOT NULL DEFAULT 'real',
    relevance VARCHAR(16) NOT NULL DEFAULT 'unknown',
    news_importance VARCHAR(16),
    sentiment_direction VARCHAR(16) NOT NULL DEFAULT 'unknown',
    credit_impact VARCHAR(16) NOT NULL DEFAULT 'none',
    confidence FLOAT,
    review_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    rule_version VARCHAR(32) NOT NULL DEFAULT 'entity-signal-v1',
    structured_json TEXT,
    legacy_entry_id INTEGER REFERENCES daily_risk_entries(id),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS credit_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES target_entities(id),
    previous_level VARCHAR(16) NOT NULL,
    new_level VARCHAR(16) NOT NULL,
    reason TEXT,
    trigger_risk_id INTEGER REFERENCES entity_risks(id),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_alert_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_risk_id INTEGER NOT NULL REFERENCES entity_risks(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES target_entities(id) ON DELETE CASCADE,
    channel VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    error_message TEXT,
    delivered_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(entity_risk_id, channel)
);

CREATE TABLE IF NOT EXISTS alert_channel_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel VARCHAR(32) NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT 0,
    encrypted_config TEXT,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS industry_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_report_id INTEGER REFERENCES industry_reports(id) ON DELETE SET NULL,
    root_report_id INTEGER REFERENCES industry_reports(id) ON DELETE SET NULL,
    version INTEGER NOT NULL DEFAULT 1,
    report_name VARCHAR(256),
    industry_name VARCHAR(256) NOT NULL,
    company_name VARCHAR(256),
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    supplement_search BOOLEAN NOT NULL DEFAULT 1,
    library_saved BOOLEAN NOT NULL DEFAULT 0,
    report_html TEXT,
    report_json TEXT,
    chart_specs TEXT,
    source_manifest_json TEXT,
    generation_config_json TEXT,
    generation_mode VARCHAR(32),
    grounded_run_id INTEGER REFERENCES industry_grounded_report_runs(id) ON DELETE SET NULL,
    prompt_version VARCHAR(64),
    evidence_snapshot_hash VARCHAR(64),
    conflict_snapshot_hash VARCHAR(64),
    citation_validation_status VARCHAR(32),
    promoted_at DATETIME,
    promotion_type VARCHAR(32),
    promotion_note TEXT,
    grounded_generation_metadata TEXT,
    error_message TEXT,
    legacy_report_id INTEGER REFERENCES industry_analysis_reports(id),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS industry_data_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES industry_reports(id) ON DELETE CASCADE,
    copied_from_source_id INTEGER REFERENCES industry_data_sources(id) ON DELETE SET NULL,
    name VARCHAR(256) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    file_path VARCHAR(1024),
    original_filename VARCHAR(512),
    url VARCHAR(1024),
    extracted_text TEXT,
    content_hash VARCHAR(64),
    char_count INTEGER NOT NULL DEFAULT 0,
    raw_content_hash VARCHAR(64),
    extracted_text_hash VARCHAR(64),
    mime_type VARCHAR(128),
    file_size INTEGER,
    source_origin VARCHAR(32),
    source_publisher VARCHAR(256),
    published_at VARCHAR(128),
    retrieved_at DATETIME,
    is_full_text BOOLEAN,
    is_truncated BOOLEAN,
    parse_status VARCHAR(32),
    parse_warning TEXT,
    used_ocr BOOLEAN,
    page_count INTEGER,
    slide_count INTEGER,
    sheet_count INTEGER,
    evidence_grade VARCHAR(32),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS industry_source_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES industry_reports(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES industry_data_sources(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    locator VARCHAR(512) NOT NULL,
    page_number INTEGER,
    slide_number INTEGER,
    sheet_name VARCHAR(256),
    cell_range VARCHAR(128),
    row_range VARCHAR(128),
    paragraph_index INTEGER,
    table_index INTEGER,
    table_row_index INTEGER,
    char_start INTEGER,
    char_end INTEGER,
    content_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS industry_evidence_extraction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES industry_reports(id) ON DELETE CASCADE,
    source_id_scope INTEGER REFERENCES industry_data_sources(id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    extractor_provider VARCHAR(64) NOT NULL,
    extractor_model VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    source_snapshot_hash VARCHAR(64) NOT NULL,
    total_sources INTEGER NOT NULL DEFAULT 0,
    total_chunks INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    verified_count INTEGER NOT NULL DEFAULT 0,
    needs_review_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS industry_evidence_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_code VARCHAR(32) NOT NULL,
    report_id INTEGER NOT NULL REFERENCES industry_reports(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES industry_data_sources(id) ON DELETE CASCADE,
    chunk_id INTEGER NOT NULL REFERENCES industry_source_chunks(id) ON DELETE CASCADE,
    extraction_run_id INTEGER NOT NULL REFERENCES industry_evidence_extraction_runs(id) ON DELETE CASCADE,
    dedupe_key VARCHAR(64) NOT NULL,
    chunk_content_hash VARCHAR(64) NOT NULL,
    locator VARCHAR(512) NOT NULL,
    original_quote TEXT NOT NULL,
    quote_start INTEGER,
    quote_end INTEGER,
    normalized_claim TEXT NOT NULL,
    claim_type VARCHAR(32) NOT NULL,
    subject VARCHAR(512), metric_name VARCHAR(256), raw_value VARCHAR(128),
    normalized_value VARCHAR(128), value_multiplier VARCHAR(64), unit VARCHAR(64),
    currency VARCHAR(32), period VARCHAR(128), as_of_date VARCHAR(64), speaker VARCHAR(512),
    importance_score INTEGER NOT NULL, importance_reason TEXT, risk_tags TEXT NOT NULL DEFAULT '[]',
    extraction_confidence VARCHAR(32), validation_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    verification_scope VARCHAR(32) NOT NULL DEFAULT 'source_match',
    requires_manual_review BOOLEAN NOT NULL DEFAULT 0, rejection_reason TEXT,
    source_origin VARCHAR(32), evidence_grade VARCHAR(32),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(report_id, evidence_code), UNIQUE(extraction_run_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS industry_conflict_detection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES industry_reports(id) ON DELETE CASCADE,
    evidence_snapshot_hash VARCHAR(64) NOT NULL,
    detector_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    eligible_evidence_count INTEGER NOT NULL DEFAULT 0,
    compared_group_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS industry_evidence_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_code VARCHAR(32) NOT NULL,
    report_id INTEGER NOT NULL REFERENCES industry_reports(id) ON DELETE CASCADE,
    detection_run_id INTEGER NOT NULL REFERENCES industry_conflict_detection_runs(id) ON DELETE CASCADE,
    conflict_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    subject_key VARCHAR(512) NOT NULL,
    metric_key VARCHAR(256) NOT NULL,
    period_key VARCHAR(128), currency_key VARCHAR(32),
    dimension_key VARCHAR(32) NOT NULL, base_unit VARCHAR(32),
    description TEXT NOT NULL,
    resolution_status VARCHAR(32) NOT NULL DEFAULT 'open',
    resolution_note TEXT, selected_evidence_code VARCHAR(32),
    requires_manual_review BOOLEAN NOT NULL DEFAULT 1,
    dedupe_key VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(report_id, conflict_code), UNIQUE(detection_run_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS industry_evidence_conflict_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_id INTEGER NOT NULL REFERENCES industry_evidence_conflicts(id) ON DELETE CASCADE,
    evidence_card_id INTEGER NOT NULL REFERENCES industry_evidence_cards(id) ON DELETE CASCADE,
    evidence_code VARCHAR(32) NOT NULL,
    source_id INTEGER NOT NULL,
    comparison_value VARCHAR(128), comparison_unit VARCHAR(32),
    source_origin VARCHAR(32), evidence_grade VARCHAR(32),
    validation_status VARCHAR(32) NOT NULL, member_role VARCHAR(32) NOT NULL DEFAULT 'compared',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(conflict_id, evidence_card_id)
);

CREATE TABLE IF NOT EXISTS industry_grounded_report_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES industry_reports(id) ON DELETE CASCADE,
    evidence_snapshot_hash VARCHAR(64) NOT NULL,
    conflict_snapshot_hash VARCHAR(64) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    model VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    failure_code VARCHAR(64), candidate_report_json TEXT, validation_errors_json TEXT,
    repair_count INTEGER NOT NULL DEFAULT 0,
    citation_count INTEGER NOT NULL DEFAULT 0,
    cited_evidence_count INTEGER NOT NULL DEFAULT 0,
    uncited_sentence_count INTEGER NOT NULL DEFAULT 0,
    started_at DATETIME, completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_news_report_date ON news_articles(report_date);
CREATE INDEX IF NOT EXISTS idx_news_module ON news_articles(module_code);
CREATE INDEX IF NOT EXISTS idx_entity_risks_entity ON entity_risks(entity_id);
CREATE INDEX IF NOT EXISTS ix_entity_risks_published_at ON entity_risks(published_at);
CREATE INDEX IF NOT EXISTS ix_entity_risks_provenance ON entity_risks(provenance);
CREATE INDEX IF NOT EXISTS ix_entity_risks_review_status ON entity_risks(review_status);
CREATE INDEX IF NOT EXISTS idx_credit_updates_entity ON credit_updates(entity_id);
CREATE INDEX IF NOT EXISTS idx_industry_reports_name ON industry_reports(industry_name);
CREATE INDEX IF NOT EXISTS ix_industry_reports_generation_mode ON industry_reports(generation_mode);
CREATE INDEX IF NOT EXISTS ix_industry_reports_grounded_run_id ON industry_reports(grounded_run_id);
CREATE INDEX IF NOT EXISTS ix_industry_data_sources_report_id ON industry_data_sources(report_id);
CREATE INDEX IF NOT EXISTS ix_industry_data_sources_content_hash ON industry_data_sources(content_hash);
CREATE INDEX IF NOT EXISTS ix_industry_data_sources_raw_content_hash ON industry_data_sources(raw_content_hash);
CREATE INDEX IF NOT EXISTS ix_industry_data_sources_extracted_text_hash ON industry_data_sources(extracted_text_hash);
CREATE INDEX IF NOT EXISTS ix_industry_data_sources_source_origin ON industry_data_sources(source_origin);
CREATE INDEX IF NOT EXISTS ix_industry_data_sources_parse_status ON industry_data_sources(parse_status);
CREATE INDEX IF NOT EXISTS ix_industry_data_sources_evidence_grade ON industry_data_sources(evidence_grade);
CREATE INDEX IF NOT EXISTS ix_industry_source_chunks_report_id ON industry_source_chunks(report_id);
CREATE INDEX IF NOT EXISTS ix_industry_source_chunks_source_id ON industry_source_chunks(source_id);
CREATE INDEX IF NOT EXISTS ix_industry_source_chunks_content_hash ON industry_source_chunks(content_hash);
CREATE INDEX IF NOT EXISTS ix_industry_evidence_runs_report_id ON industry_evidence_extraction_runs(report_id);
CREATE INDEX IF NOT EXISTS ix_industry_evidence_runs_snapshot ON industry_evidence_extraction_runs(source_snapshot_hash);
CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_report_id ON industry_evidence_cards(report_id);
CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_source_id ON industry_evidence_cards(source_id);
CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_chunk_id ON industry_evidence_cards(chunk_id);
CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_run_id ON industry_evidence_cards(extraction_run_id);
CREATE INDEX IF NOT EXISTS ix_industry_evidence_cards_status ON industry_evidence_cards(validation_status);
CREATE INDEX IF NOT EXISTS ix_industry_conflict_runs_report_id ON industry_conflict_detection_runs(report_id);
CREATE INDEX IF NOT EXISTS ix_industry_conflict_runs_snapshot ON industry_conflict_detection_runs(evidence_snapshot_hash);
CREATE INDEX IF NOT EXISTS ix_industry_conflicts_report_id ON industry_evidence_conflicts(report_id);
CREATE INDEX IF NOT EXISTS ix_industry_conflicts_run_id ON industry_evidence_conflicts(detection_run_id);
CREATE INDEX IF NOT EXISTS ix_industry_conflicts_type ON industry_evidence_conflicts(conflict_type);
CREATE INDEX IF NOT EXISTS ix_industry_conflicts_severity ON industry_evidence_conflicts(severity);
CREATE INDEX IF NOT EXISTS ix_industry_conflicts_resolution ON industry_evidence_conflicts(resolution_status);
CREATE INDEX IF NOT EXISTS ix_industry_conflict_members_conflict_id ON industry_evidence_conflict_members(conflict_id);
CREATE INDEX IF NOT EXISTS ix_industry_conflict_members_evidence_id ON industry_evidence_conflict_members(evidence_card_id);
CREATE INDEX IF NOT EXISTS ix_industry_conflict_members_source_id ON industry_evidence_conflict_members(source_id);
CREATE INDEX IF NOT EXISTS ix_industry_grounded_runs_report_id ON industry_grounded_report_runs(report_id);
CREATE INDEX IF NOT EXISTS ix_industry_grounded_runs_evidence_snapshot ON industry_grounded_report_runs(evidence_snapshot_hash);
CREATE INDEX IF NOT EXISTS ix_industry_grounded_runs_conflict_snapshot ON industry_grounded_report_runs(conflict_snapshot_hash);
CREATE INDEX IF NOT EXISTS ix_industry_grounded_runs_status ON industry_grounded_report_runs(status);
