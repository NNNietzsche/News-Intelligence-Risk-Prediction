"""主体公开信息预警灯号推断与变化日志。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import (
    CREDIT_LEVEL_ORDER,
    CREDIT_LEVELS,
    RISK_TO_CREDIT,
    get_settings,
)
from app.database.models import CreditUpdate, EntityRisk, TargetEntity
from app.services.entity_catalog import (
    canonical_bilingual_display_name,
    configured_entity_catalog,
)
from app.timeutil import tokyo_today


CREDIT_IMPACT_TO_WARNING = {
    "none": "正常",
    "low": "关注",
    "medium": "预警",
    "high": "高风险",
    "critical": "高风险",
}


def seed_default_entities(db: Session) -> int:
    """写入默认监控主体，已存在则跳过。返回新增数量。"""
    created = 0
    profiles = configured_entity_catalog().profiles
    items = [profile.as_seed() for profile in profiles]
    if not items:
        # 配置文件异常时至少保留旧版两个核心主体，不因启动而清空清单。
        items = [
            {
                "name": "Godiva",
                "display_name": "歌帝梵 Godiva",
                "aliases": "歌帝梵,Godiva Chocolatier",
                "industry": "消费品 / 巧克力",
                "region": "全球",
            },
            {
                "name": "普洛斯",
                "display_name": "普洛斯 GLP",
                "aliases": "GLP,Global Logistic Properties,普洛斯",
                "industry": "物流地产",
                "region": "亚太 / 全球",
            },
        ]
    # 先归并历史键并 flush，避免后续按当前目录名插入时与旧名改名撞 UNIQUE。
    _dedupe_glp_entity(db)
    _dedupe_legacy_entity_keys(db)
    db.flush()
    for item in items:
        exists = db.query(TargetEntity).filter(TargetEntity.name == item["name"]).first()
        if exists:
            # 目录是默认主体元数据的唯一来源；不覆盖运行状态和当前预警灯号。
            exists.display_name = item.get("display_name") or exists.display_name
            exists.aliases = item.get("aliases") or exists.aliases
            exists.industry = item.get("industry") or exists.industry
            exists.region = item.get("region") or exists.region
            continue
        db.add(
            TargetEntity(
                name=item["name"],
                display_name=item.get("display_name"),
                aliases=item.get("aliases"),
                industry=item.get("industry"),
                region=item.get("region"),
                monitor_status="active",
                credit_level="正常",
            )
        )
        created += 1
    _sync_extra_display_names(db)
    db.commit()
    # 修改记录：2026-08-25 | DingJiaye
    # 仅写入配置中附有发布日期和来源链接的已核验基准事件；重复启动不会重复插入。
    _seed_configured_entity_events(db, profiles)
    return created


def _seed_configured_entity_events(db: Session, profiles: tuple[object, ...]) -> int:
    """把目录内的可追溯历史事件写入主体事件表，供首次打开页面时直接展示。"""
    inserted = 0
    for profile in profiles:
        events = getattr(profile, "verified_events", ()) or ()
        if not events:
            continue
        entity = db.query(TargetEntity).filter(TargetEntity.name == profile.key).first()
        if entity is None:
            continue
        for event in events:
            exists = (
                db.query(EntityRisk.id)
                .filter(
                    EntityRisk.entity_id == entity.id,
                    EntityRisk.title == event.title,
                    EntityRisk.source_url == event.source_url,
                )
                .first()
            )
            if exists:
                continue
            try:
                published_at = datetime.fromisoformat(event.published_at.replace("Z", "+00:00"))
            except ValueError:
                try:
                    published_at = datetime.combine(date.fromisoformat(event.published_at), datetime.min.time())
                except ValueError:
                    continue
            db.add(
                EntityRisk(
                    entity_id=entity.id,
                    report_date=published_at.date(),
                    title=event.title,
                    risk_category=event.risk_category,
                    risk_level=event.risk_level if event.risk_level in {"低", "中", "高", "极高"} else "中",
                    summary=event.summary,
                    impact_analysis=event.impact_analysis,
                    source_url=event.source_url,
                    source_name=event.source_name,
                    published_at=published_at,
                    related_company=event.related_company,
                    provenance="real",
                    relevance="contextual",
                    news_importance="中",
                    sentiment_direction="neutral",
                    credit_impact="low",
                    confidence=0.9,
                    review_status="verified",
                    rule_version="configured-event-v1",
                )
            )
            inserted += 1
    if inserted:
        db.commit()
    return inserted


def _sync_extra_display_names(db: Session) -> None:
    """把目录外主体的显示名改成中文加英文。"""
    rows = db.query(TargetEntity).filter(TargetEntity.monitor_status == "active").all()
    for entity in rows:
        canonical = canonical_bilingual_display_name(entity.name, entity.display_name)
        if canonical and canonical != (entity.display_name or ""):
            entity.display_name = canonical


def _dedupe_glp_entity(db: Session) -> None:
    """将独立 GLP 主体的风险/预警记录归并到「普洛斯」，并停用 GLP。"""
    glp = db.query(TargetEntity).filter(TargetEntity.name == "GLP").first()
    if not glp:
        return
    prologis = db.query(TargetEntity).filter(TargetEntity.name == "普洛斯").first()
    if not prologis:
        glp.name = "普洛斯"
        glp.display_name = "普洛斯 GLP"
        glp.aliases = "GLP,Global Logistic Properties,普洛斯"
        glp.industry = glp.industry or "物流地产"
        glp.region = glp.region or "亚太 / 全球"
        return

    if glp.id == prologis.id:
        return

    db.query(EntityRisk).filter(EntityRisk.entity_id == glp.id).update(
        {EntityRisk.entity_id: prologis.id}, synchronize_session=False
    )
    db.query(CreditUpdate).filter(CreditUpdate.entity_id == glp.id).update(
        {CreditUpdate.entity_id: prologis.id}, synchronize_session=False
    )
    glp.monitor_status = "inactive"


_LEGACY_ENTITY_KEYS = {
    "三菱UFJフィナンシャル・グループ": "三菱UFJ",
    "三井住友フィナンシャルグループ": "三井住友FG",
    "みずほフィナンシャルグループ": "みずほ",
    "野村ホールディングス": "野村",
    "ＳＭＢＣ日興証券": "SMBC日興",
}


def _dedupe_legacy_entity_keys(db: Session) -> None:
    """停用已改名的历史主体，并把记录归并到当前目录键。"""
    for old_name, new_name in _LEGACY_ENTITY_KEYS.items():
        old = db.query(TargetEntity).filter(TargetEntity.name == old_name).first()
        if not old:
            continue
        current = db.query(TargetEntity).filter(TargetEntity.name == new_name).first()
        if not current:
            old.name = new_name
            continue
        if old.id == current.id:
            continue
        db.query(EntityRisk).filter(EntityRisk.entity_id == old.id).update(
            {EntityRisk.entity_id: current.id}, synchronize_session=False
        )
        db.query(CreditUpdate).filter(CreditUpdate.entity_id == old.id).update(
            {CreditUpdate.entity_id: current.id}, synchronize_session=False
        )
        old.monitor_status = "inactive"


def resolve_entity(
    db: Session,
    *,
    name_hint: Optional[str] = None,
    related_company: Optional[str] = None,
    create_if_missing: bool = True,
) -> Optional[TargetEntity]:
    """按名称/别名匹配主体；必要时新建。"""
    candidates = [c for c in (name_hint, related_company) if c and str(c).strip()]
    if not candidates:
        return None

    entities = db.query(TargetEntity).all()
    for hint in candidates:
        hint_l = hint.strip().lower()
        for ent in entities:
            names = [ent.name, ent.display_name or ""]
            if ent.aliases:
                names.extend(a.strip() for a in ent.aliases.split(",") if a.strip())
            for n in names:
                if n and (hint_l == n.lower() or hint_l in n.lower() or n.lower() in hint_l):
                    return ent

    if not create_if_missing:
        return None

    primary = candidates[0].strip()
    # 归一常见复合名
    if "godiva" in primary.lower() or "歌帝梵" in primary:
        primary = "Godiva"
    elif "glp" in primary.lower() or "普洛斯" in primary:
        primary = "普洛斯"

    existing = db.query(TargetEntity).filter(TargetEntity.name == primary).first()
    if existing:
        return existing

    ent = TargetEntity(
        name=primary,
        display_name=primary,
        monitor_status="active",
        credit_level="正常",
    )
    db.add(ent)
    db.flush()
    return ent


def list_active_entity_ids(db: Session) -> list[int]:
    """当前仍在监控的主体，按 id 稳定排序。"""
    rows = (
        db.query(TargetEntity.id)
        .filter(TargetEntity.monitor_status == "active")
        .order_by(TargetEntity.id.asc())
        .all()
    )
    return [int(row[0]) for row in rows]


def credit_from_risk_level(risk_level: str) -> str:
    level = RISK_TO_CREDIT.get(risk_level, "关注")
    return level if level in CREDIT_LEVELS else "关注"


def suggested_credit_for_entity(db: Session, entity_id: int) -> str:
    """按观察期内有效负面信用信号推导舆情预警灯号。

    演示、降级、已驳回、上下文资讯和低置信度条目不会触发灯号。
    该结果是公开信息预警，不是客户内部评级或授信审批结论。
    """
    lookback_days = max(
        1, int(getattr(get_settings(), "entity_warning_lookback_days", 90) or 90)
    )
    cutoff = tokyo_today() - timedelta(days=lookback_days)
    risks = (
        db.query(EntityRisk)
        .filter(EntityRisk.entity_id == entity_id, EntityRisk.report_date >= cutoff)
        .all()
    )
    if not risks:
        return "正常"
    best = "正常"
    best_rank = CREDIT_LEVEL_ORDER[best]
    for r in risks:
        if (r.provenance or "real") not in {"real", "manual"}:
            continue
        if (r.review_status or "pending") == "rejected":
            continue
        if (r.relevance or "unknown") != "direct":
            continue
        if (r.sentiment_direction or "unknown") != "negative":
            continue
        # 自动整理的真实来源必须给出达到门槛的置信度；缺失不能默认为可信。
        if r.provenance == "real" and (
            r.confidence is None or float(r.confidence) < 0.55
        ):
            continue
        if r.provenance == "manual" and (
            r.confidence is not None and float(r.confidence) < 0.55
        ):
            continue
        cand = CREDIT_IMPACT_TO_WARNING.get((r.credit_impact or "none").lower(), "正常")
        rank = CREDIT_LEVEL_ORDER.get(cand, 0)
        if rank > best_rank:
            best = cand
            best_rank = rank
    return best


def apply_credit_update(
    db: Session,
    entity: TargetEntity,
    *,
    new_level: Optional[str] = None,
    reason: str = "",
    trigger_risk_id: Optional[int] = None,
    force: bool = False,
) -> Optional[CreditUpdate]:
    """
    更新主体公开信息预警灯号并写变化日志。
    默认仅在等级上升时更新；force=True 时按建议等级同步（含下调）。
    """
    target = new_level or suggested_credit_for_entity(db, entity.id)
    if target not in CREDIT_LEVELS:
        target = "关注"

    prev = entity.credit_level or "正常"
    prev_rank = CREDIT_LEVEL_ORDER.get(prev, 1)
    new_rank = CREDIT_LEVEL_ORDER.get(target, 1)

    if not force and new_rank <= prev_rank and target == prev:
        return None
    if not force and new_rank < prev_rank:
        # 默认不自动下调
        return None
    if target == prev:
        return None

    entity.credit_level = target
    log = CreditUpdate(
        entity_id=entity.id,
        previous_level=prev,
        new_level=target,
        reason=reason or f"依据公开信息信号调整灯号：{prev} → {target}",
        trigger_risk_id=trigger_risk_id,
    )
    db.add(log)
    db.flush()
    return log


def refresh_entity_credit(
    db: Session,
    entity: TargetEntity,
    *,
    trigger_risk: Optional[EntityRisk] = None,
    reason: str = "",
    force: bool = False,
) -> Optional[CreditUpdate]:
    suggested = suggested_credit_for_entity(db, entity.id)
    return apply_credit_update(
        db,
        entity,
        new_level=suggested,
        reason=reason
        or (
            f"风险事件「{trigger_risk.title}」触发人工复核"
            if trigger_risk
            else "公开信息事件集合变更后复核预警灯号"
        ),
        trigger_risk_id=trigger_risk.id if trigger_risk else None,
        force=force,
    )


def rebuild_entity_warning_levels(db: Session) -> int:
    """按新规则重算所有主体灯号；用于升级旧库并清除 Mock 导致的误预警。"""
    changed = 0
    for entity in db.query(TargetEntity).all():
        if refresh_entity_credit(
            db,
            entity,
            reason="主体舆情预警规则升级：仅采用观察期内有效负面信用信号",
            force=True,
        ):
            changed += 1
    if changed:
        db.commit()
    return changed
