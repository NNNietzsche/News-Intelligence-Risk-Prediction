"""主体评估显式演示数据；正常采集不会自动调用。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.database.models import CreditUpdate, EntityRisk, TargetEntity
from app.services.entity_credit import refresh_entity_credit, seed_default_entities
from app.timeutil import tokyo_today


# 这些等级仅用于演示事件外观；演示条目的信用信号固定为 none。
DEMO_RISKS: dict[str, list[dict[str, Any]]] = {
    "Godiva": [
        {
            "title": "欧盟消费者组织关注巧克力品牌可持续可可 sourcing 披露完整性",
            "risk_category": "司法与行政监管",
            "risk_level": "中",
            "summary": "多家消费组织呼吁加强可可供应链尽职调查披露，Godiva 母公司被列入观察名单，尚无正式处罚。",
            "impact_analysis": "短期以合规沟通与披露补强为主，对授信现金流无即时冲击；若后续出台强制尽职调查规则，合规成本可能上升。",
            "source_url": "https://example.com/mock/godiva-eu-disclosure",
            "days_ago": 1,
        },
        {
            "title": "北美门店同店销售波动，部分区域促销力度加大",
            "risk_category": "金融与经营数据",
            "risk_level": "中",
            "summary": "行业跟踪显示北美高端巧克力渠道促销频次上升，Godiva 部分门店同店销售出现个位数波动。",
            "impact_analysis": "经营波动处于可观察区间，需关注毛利率与库存周转；尚未构成实质性财务违约信号。",
            "source_url": "https://example.com/mock/godiva-sales",
            "days_ago": 2,
        },
        {
            "title": "社交媒体出现品牌联名争议话题，传播量短期抬升",
            "risk_category": "公开舆论与社交媒体",
            "risk_level": "低",
            "summary": "某联名营销活动引发网络讨论，负面评论占比可控，品牌官方已发布澄清说明。",
            "impact_analysis": "舆情风险偏事件驱动、衰减较快，对长期品牌资产与授信评级影响有限，建议维持常规监测。",
            "source_url": "https://example.com/mock/godiva-social",
            "days_ago": 0,
        },
        {
            "title": "主要可可原料供应商所在国出现港口延误",
            "risk_category": "供应链与关联方",
            "risk_level": "中",
            "summary": "西非部分港口作业效率下降，可可豆到港周期延长约 1–2 周，可能影响季节性备货节奏。",
            "impact_analysis": "供应链扰动可能推高短期原料成本与安全库存，需评估对毛利与交货履约的压力。",
            "source_url": "https://example.com/mock/godiva-supply",
            "days_ago": 3,
        },
    ],
    "普洛斯": [
        {
            "title": "部分物流园区规划调整涉及地方规划审批沟通",
            "risk_category": "司法与行政监管",
            "risk_level": "中",
            "summary": "公开信息显示个别园区改扩建需补充环境影响与规划合规材料，属于项目推进常规审批环节。",
            "impact_analysis": "项目节奏或阶段性延后，但未见行政处罚或停工令；对整体资产组合信用质量影响可控。",
            "source_url": "https://example.com/mock/glp-permitting",
            "days_ago": 1,
        },
        {
            "title": "物流地产板块租金与出租率保持稳健，个别市场承压",
            "risk_category": "金融与经营数据",
            "risk_level": "低",
            "summary": "行业周报显示核心市场高标仓出租率仍处高位，部分二三线市场租金增速放缓。",
            "impact_analysis": "组合层面经营指标总体稳健；局部市场承压需纳入压力测试，但尚不足以触发授信下调。",
            "source_url": "https://example.com/mock/glp-occupancy",
            "days_ago": 2,
        },
        {
            "title": "媒体报道关注物流地产巨头资本运作与资产证券化进展",
            "risk_category": "公开舆论与社交媒体",
            "risk_level": "低",
            "summary": "财经媒体跟进 GLP 相关资产证券化与再融资讨论，市场情绪中性偏谨慎。",
            "impact_analysis": "舆情以资本市场叙事为主，未指向具体经营或合规丑闻；建议跟踪后续融资落地情况。",
            "source_url": "https://example.com/mock/glp-media",
            "days_ago": 0,
        },
        {
            "title": "重点租户所在电商行业需求波动，续租谈判趋谨慎",
            "risk_category": "供应链与关联方",
            "risk_level": "高",
            "summary": "部分电商/3PL 租户面临需求波动，个别大面积仓续租条款谈判周期拉长，租金让利诉求上升。",
            "impact_analysis": "关联方集中度与续租不确定性上升，可能影响近端 NOI 与现金流预测，建议上调关注并复核租户信用。",
            "source_url": "https://example.com/mock/glp-tenant",
            "days_ago": 1,
        },
    ],
}


def _entity_key(entity: TargetEntity) -> Optional[str]:
    name = (entity.name or "").strip()
    display = (entity.display_name or "").strip()
    if name in DEMO_RISKS:
        return name
    if "Godiva" in name or "Godiva" in display or "歌帝梵" in display:
        return "Godiva"
    if "普洛斯" in name or "GLP" in name.upper() or "GLP" in display.upper() or "普洛斯" in display:
        return "普洛斯"
    return None


def seed_entity_demo_data(db: Session, *, force: bool = False) -> dict[str, int]:
    """为兼容主体显式写入演示事件；force 也不会删除真实数据。"""
    seed_default_entities(db)
    created_risks = 0
    created_credits = 0

    entities = (
        db.query(TargetEntity)
        .filter(TargetEntity.monitor_status == "active")
        .all()
    )
    today = tokyo_today()

    for ent in entities:
        key = _entity_key(ent)
        if not key:
            continue
        demo_ids = [
            row_id
            for (row_id,) in db.query(EntityRisk.id).filter(
                EntityRisk.entity_id == ent.id,
                EntityRisk.provenance == "demo",
            )
        ]
        if demo_ids and not force:
            continue

        if force and demo_ids:
            db.query(CreditUpdate).filter(
                CreditUpdate.trigger_risk_id.in_(demo_ids)
            ).delete(synchronize_session=False)
            db.query(EntityRisk).filter(EntityRisk.id.in_(demo_ids)).delete(
                synchronize_session=False
            )

        for item in DEMO_RISKS[key]:
            rd = today - timedelta(days=int(item.get("days_ago") or 0))
            risk = EntityRisk(
                entity_id=ent.id,
                report_date=rd,
                title=item["title"],
                risk_category=item["risk_category"],
                risk_level=item["risk_level"],
                summary=item["summary"],
                impact_analysis=item["impact_analysis"],
                source_url=item.get("source_url"),
                source_name="演示数据",
                provenance="demo",
                relevance="direct",
                news_importance=item["risk_level"],
                sentiment_direction="unknown",
                credit_impact="none",
                review_status="rejected",
                rule_version="entity-signal-v1",
                related_company=ent.display_name or ent.name,
                structured_json=None,
                created_at=datetime.utcnow() - timedelta(days=int(item.get("days_ago") or 0)),
            )
            db.add(risk)
            created_risks += 1
        db.flush()
        log = refresh_entity_credit(
            db,
            ent,
            reason="演示数据初始化：演示条目不参与舆情预警计算",
            force=True,
        )
        if log:
            created_credits += 1
        elif db.query(CreditUpdate).filter(CreditUpdate.entity_id == ent.id).count() == 0:
            # 等级未变时仍写一条时间轴记录，便于空状态演示
            db.add(
                CreditUpdate(
                    entity_id=ent.id,
                    previous_level="正常",
                    new_level=ent.credit_level or "正常",
                    reason="演示：完成主体公开信息预警基线展示",
                )
            )
            created_credits += 1

    db.commit()
    return {"risks": created_risks, "credit_logs": created_credits}


def refresh_entity_demo_for_collect(
    db: Session,
    *,
    entity_id: int,
    report_date: date,
) -> int:
    """仅在显式演示模式下写入/刷新当日样本，不覆盖真实事件。"""
    ent = db.query(TargetEntity).filter(TargetEntity.id == entity_id).first()
    if not ent:
        return 0
    key = _entity_key(ent)
    if not key:
        return 0

    # 只清除该主体当日旧演示条目，真实/人工/降级事件必须保留。
    old_ids = [
        r[0]
        for r in db.query(EntityRisk.id)
        .filter(
            EntityRisk.entity_id == entity_id,
            EntityRisk.report_date == report_date,
            EntityRisk.provenance == "demo",
        )
        .all()
    ]
    if old_ids:
        db.query(CreditUpdate).filter(CreditUpdate.trigger_risk_id.in_(old_ids)).update(
            {CreditUpdate.trigger_risk_id: None}, synchronize_session=False
        )
        db.query(EntityRisk).filter(EntityRisk.id.in_(old_ids)).delete(synchronize_session=False)

    saved = 0
    for item in DEMO_RISKS[key]:
        # 采集场景：全部记为报告日，突出「近24小时」
        risk = EntityRisk(
            entity_id=entity_id,
            report_date=report_date,
            title=f"[近24小时] {item['title']}",
            risk_category=item["risk_category"],
            risk_level=item["risk_level"],
            summary=item["summary"],
            impact_analysis=item["impact_analysis"],
            source_url=item.get("source_url"),
            source_name="演示数据",
            provenance="demo",
            relevance="direct",
            news_importance=item["risk_level"],
            sentiment_direction="unknown",
            credit_impact="none",
            review_status="rejected",
            rule_version="entity-signal-v1",
            related_company=ent.display_name or ent.name,
        )
        db.add(risk)
        saved += 1
    db.flush()
    refresh_entity_credit(
        db,
        ent,
        reason="演示样本已刷新；演示条目不参与舆情预警计算",
        force=True,
    )
    db.commit()
    return saved
