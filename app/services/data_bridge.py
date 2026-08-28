"""切片 2：旧表数据回填至 news_articles / entity_risks / industry_reports。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.database.models import (
    DailyRiskEntry,
    EntityRisk,
    IndustryAnalysisReport,
    IndustryReport,
    NewsArticle,
)
from app.services.entity_credit import (
    rebuild_entity_warning_levels,
    refresh_entity_credit,
    resolve_entity,
    seed_default_entities,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

NEWS_MODULES = {"B", "C", "D"}
ENTITY_MODULE = "A"


def _rename_legacy_credit_levels(db: Session) -> int:
    """兼容旧数据：警示 → 预警。"""
    from app.database.models import CreditUpdate, TargetEntity

    n = 0
    n += (
        db.query(TargetEntity)
        .filter(TargetEntity.credit_level == "警示")
        .update({TargetEntity.credit_level: "预警"}, synchronize_session=False)
    )
    n += (
        db.query(CreditUpdate)
        .filter(CreditUpdate.previous_level == "警示")
        .update({CreditUpdate.previous_level: "预警"}, synchronize_session=False)
    )
    n += (
        db.query(CreditUpdate)
        .filter(CreditUpdate.new_level == "警示")
        .update({CreditUpdate.new_level: "预警"}, synchronize_session=False)
    )
    if n:
        db.commit()
    return n


def migrate_legacy_data(db: Session) -> dict[str, int]:
    """
    幂等回填：按 legacy_*_id 去重。
    返回各表新增条数。
    """
    stats = {
        "entities_seeded": seed_default_entities(db),
        "credit_renamed": _rename_legacy_credit_levels(db),
        "news_articles": 0,
        "entity_risks": 0,
        "industry_reports": 0,
        "entity_demo": 0,
        "warning_levels_rebuilt": 0,
    }

    try:
        from app.services.entity_mock import seed_entity_demo_data

        if bool(getattr(get_settings(), "entity_demo_mode", False)):
            demo = seed_entity_demo_data(db, force=False)
            stats["entity_demo"] = int(demo.get("risks") or 0)
    except Exception as exc:
        logger.warning("主体演示数据写入跳过: %s", exc)

    # --- 资讯：B/C/D ---
    legacy_news_ids = {
        r[0]
        for r in db.query(NewsArticle.legacy_entry_id)
        .filter(NewsArticle.legacy_entry_id.isnot(None))
        .all()
    }
    news_entries = (
        db.query(DailyRiskEntry)
        .filter(DailyRiskEntry.module_code.in_(NEWS_MODULES))
        .all()
    )
    for e in news_entries:
        if e.id in legacy_news_ids:
            continue
        db.add(
            NewsArticle(
                report_date=e.report_date,
                module_code=e.module_code,
                category_tag=e.pillar_or_topic or e.risk_category,
                country_or_region=e.country_or_region,
                target_entity=e.target_entity,
                title=e.title,
                related_company=e.related_company,
                risk_category=e.risk_category,
                risk_level=e.risk_level,
                summary=e.summary,
                impact_analysis=e.impact_analysis,
                source_url=e.source_url,
                source_title=e.source_title,
                structured_json=e.structured_json,
                legacy_entry_id=e.id,
                created_at=e.created_at,
            )
        )
        stats["news_articles"] += 1

    # --- 主体风险：A ---
    legacy_risk_ids = {
        r[0]
        for r in db.query(EntityRisk.legacy_entry_id)
        .filter(EntityRisk.legacy_entry_id.isnot(None))
        .all()
    }
    entity_entries = (
        db.query(DailyRiskEntry).filter(DailyRiskEntry.module_code == ENTITY_MODULE).all()
    )
    for e in entity_entries:
        if e.id in legacy_risk_ids:
            continue
        ent = resolve_entity(
            db,
            name_hint=e.target_entity,
            related_company=e.related_company,
            create_if_missing=True,
        )
        if not ent:
            continue
        risk = EntityRisk(
            entity_id=ent.id,
            report_date=e.report_date,
            title=e.title,
            risk_category=e.risk_category,
            risk_level=e.risk_level,
            summary=e.summary,
            impact_analysis=e.impact_analysis,
            source_url=e.source_url,
            source_name=e.source_title,
            published_at=e.published_at,
            related_company=e.related_company,
            provenance=(
                "demo"
                if "example.com" in (e.source_url or "").lower()
                else "degraded"
                if '"_degraded": true' in (e.structured_json or "").lower()
                else "real"
            ),
            relevance="direct",
            news_importance=e.risk_level,
            sentiment_direction="unknown",
            credit_impact="none",
            review_status=(
                "rejected" if "example.com" in (e.source_url or "").lower() else "pending"
            ),
            rule_version="entity-signal-v1",
            structured_json=e.structured_json,
            legacy_entry_id=e.id,
            created_at=e.created_at,
        )
        db.add(risk)
        db.flush()
        refresh_entity_credit(db, ent, trigger_risk=risk)
        stats["entity_risks"] += 1

    # --- 深度研报 ---
    legacy_report_ids = {
        r[0]
        for r in db.query(IndustryReport.legacy_report_id)
        .filter(IndustryReport.legacy_report_id.isnot(None))
        .all()
    }
    old_reports = db.query(IndustryAnalysisReport).all()
    for r in old_reports:
        if r.id in legacy_report_ids:
            continue
        # 亦跳过已按内容近似存在的（无 legacy 但同名同时间）——以 legacy 为主
        db.add(
            IndustryReport(
                industry_name=r.industry_name,
                company_name=r.company_name,
                status=r.status,
                report_html=r.report_html,
                report_json=r.report_json,
                chart_specs=r.chart_specs,
                error_message=r.error_message,
                legacy_report_id=r.id,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
        )
        stats["industry_reports"] += 1

    if any(stats[k] for k in ("news_articles", "entity_risks", "industry_reports", "entities_seeded")):
        db.commit()
        logger.info("切片2回填完成: %s", stats)
    stats["warning_levels_rebuilt"] = rebuild_entity_warning_levels(db)
    # rebuild 在无需更新时只执行查询。显式结束这个只读事务，避免后续启动
    # 迁移另开 sqlite3 写连接时被当前 SQLAlchemy 会话自身阻塞。
    db.commit()
    return stats
