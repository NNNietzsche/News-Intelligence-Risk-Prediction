"""高风险与关联企业公开信息事件的外部预警通知。

支持 SMTP 邮件和企业微信机器人。页面配置使用
SECRET_KEY 派生密钥加密保存；API 从不回传 Webhook 或 SMTP 密码。
Modified by DingJiaye: 2026-08-27.
"""

from __future__ import annotations

import base64
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
import hashlib
import json
import logging
import smtplib
from threading import Thread
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.models import AlertChannelConfig, EntityRisk, RiskAlertDelivery, TargetEntity
from app.services.http_client import get_http_client

logger = logging.getLogger(__name__)
_CHANNELS = ("email", "wecom")


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest())
    return Fernet(key)


def _decode(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = _fernet().decrypt(value.encode("utf-8"))
        decoded = json.loads(payload.decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}
    except (InvalidToken, ValueError, json.JSONDecodeError):
        logger.warning("预警渠道配置无法解密；请在设置中重新保存。")
        return {}


def _encode(value: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("utf-8")


def _env_policy() -> dict[str, Any]:
    # Modified by DingJiaye: 2026-08-27 — 默认仅推送高 / 极高风险新闻。
    return {"high_risk_news": True, "entity_ids": []}


def _env_channel(channel: str) -> dict[str, Any]:
    s = get_settings()
    if channel == "email":
        return {"alert_email_to": s.alert_email_to, "smtp_host": s.smtp_host, "smtp_port": s.smtp_port, "smtp_username": s.smtp_username, "smtp_password": s.smtp_password, "smtp_from": s.smtp_from, "smtp_use_starttls": s.smtp_use_starttls}
    if channel == "wecom":
        return {"webhook_url": s.wecom_webhook_url}
    return {}


def _channel_ready(channel: str, value: dict[str, Any]) -> bool:
    if channel == "email":
        return bool(str(value.get("alert_email_to") or "").strip() and str(value.get("smtp_host") or "").strip())
    return bool(str(value.get("webhook_url") or "").strip())


def _saved_row(db: Session, channel: str) -> AlertChannelConfig | None:
    return db.query(AlertChannelConfig).filter(AlertChannelConfig.channel == channel).first()


def _policy(db: Session | None = None) -> dict[str, Any]:
    if db is not None:
        row = _saved_row(db, "policy")
        if row:
            saved = _decode(row.encrypted_config)
            return {
                "high_risk_news": bool(saved.get("high_risk_news", True)),
                "entity_ids": [int(item) for item in saved.get("entity_ids", []) if str(item).isdigit()],
            }
    return _env_policy()


def _channel_config(db: Session, channel: str) -> tuple[bool, dict[str, Any]]:
    row = _saved_row(db, channel)
    if row:
        return bool(row.enabled), _decode(row.encrypted_config)
    value = _env_channel(channel)
    return _channel_ready(channel, value), value


def _active_channels(db: Session) -> tuple[str, ...]:
    return tuple(channel for channel in _CHANNELS if _channel_config(db, channel)[0] and _channel_ready(channel, _channel_config(db, channel)[1]))


def _eligible(risk: EntityRisk, policy: dict[str, Any]) -> bool:
    if (risk.provenance or "real") not in {"real", "manual"}:
        return False
    return bool(
        (policy.get("high_risk_news") and (risk.risk_level or "") in {"高", "极高"})
        or risk.entity_id in set(policy.get("entity_ids") or [])
    )


def notification_status(db: Session | None = None) -> dict[str, object]:
    """返回脱敏后的通知配置状态，供页面与健康检查使用。"""
    policy = _policy(db)
    channels = _active_channels(db) if db is not None else tuple(
        channel for channel in _CHANNELS if _channel_ready(channel, _env_channel(channel))
    )
    return {
        "high_risk_news": policy["high_risk_news"],
        "entity_ids": policy["entity_ids"],
        "channels": list(channels),
        "configured": bool(channels),
    }


def alert_settings_view(db: Session) -> dict[str, Any]:
    """面向设置页面的脱敏配置；绝不回传密码与 Webhook。"""
    items: dict[str, Any] = {}
    for channel in _CHANNELS:
        enabled, config = _channel_config(db, channel)
        if channel == "email":
            items[channel] = {"enabled": enabled, "configured": _channel_ready(channel, config), "config": {key: config.get(key, "") for key in ("alert_email_to", "smtp_host", "smtp_port", "smtp_username", "smtp_from", "smtp_use_starttls")}, "secret_configured": bool(config.get("smtp_password"))}
        else:
            items[channel] = {"enabled": enabled, "configured": _channel_ready(channel, config), "config": {}, "secret_configured": bool(config.get("webhook_url"))}
    entities = (
        db.query(TargetEntity)
        .filter(TargetEntity.monitor_status == "active")
        .order_by(TargetEntity.display_name.asc(), TargetEntity.name.asc())
        .all()
    )
    return {
        "policy": _policy(db),
        "entities": [
            {"id": entity.id, "name": _display_name(entity), "industry": entity.industry or ""}
            for entity in entities
        ],
        "channels": items,
    }


def save_alert_policy(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    requested_ids = {int(item) for item in (payload.get("entity_ids") or []) if isinstance(item, int) or str(item).isdigit()}
    valid_ids = {
        entity_id
        for (entity_id,) in db.query(TargetEntity.id)
        .filter(TargetEntity.id.in_(requested_ids), TargetEntity.monitor_status == "active")
        .all()
    } if requested_ids else set()
    row = _saved_row(db, "policy") or AlertChannelConfig(channel="policy")
    row.enabled = bool(payload.get("high_risk_news", True) or valid_ids)
    row.encrypted_config = _encode({"high_risk_news": bool(payload.get("high_risk_news", True)), "entity_ids": sorted(valid_ids)})
    db.add(row); db.commit()
    return alert_settings_view(db)


def save_alert_channel(db: Session, channel: str, *, enabled: bool, config: dict[str, Any]) -> dict[str, Any]:
    if channel not in _CHANNELS:
        raise ValueError("未知通知渠道")
    row = _saved_row(db, channel)
    existing = _decode(row.encrypted_config) if row else _env_channel(channel)
    merged = dict(existing)
    if channel == "email":
        for key in ("alert_email_to", "smtp_host", "smtp_port", "smtp_username", "smtp_from", "smtp_use_starttls"):
            if key in config:
                merged[key] = config[key]
        # 空密码意味着保留原值，避免设置页回显或意外清空。
        if str(config.get("smtp_password") or "").strip():
            merged["smtp_password"] = str(config["smtp_password"]).strip()
    else:
        if str(config.get("webhook_url") or "").strip():
            merged["webhook_url"] = str(config["webhook_url"]).strip()
    row = row or AlertChannelConfig(channel=channel)
    row.enabled = bool(enabled)
    row.encrypted_config = _encode(merged)
    db.add(row); db.commit()
    return alert_settings_view(db)


def _display_name(entity: TargetEntity) -> str:
    return str(entity.display_name or entity.name or f"主体#{entity.id}")


def _message(entity: TargetEntity, risk: EntityRisk) -> tuple[str, str, str]:
    name = _display_name(entity)
    subject = f"【风险情报预警·{risk.risk_level}】{name}：{risk.title}"
    published = risk.published_at.strftime("%Y-%m-%d %H:%M") if risk.published_at else "待核验"
    body = "\n".join([f"监测主体：{name}", f"风险等级：{risk.risk_level}", f"风险类型：{risk.risk_category or '待分类'}", f"事件标题：{risk.title}", f"源发布时间：{published}", f"来源：{risk.source_name or '未标注'}", "", "内容摘要：", risk.summary or "未取得正文摘要。", "", "风险提示：", risk.impact_analysis or "请核验事件范围、本行敞口及后续披露。", "", f"原始来源：{risk.source_url or '未提供'}", "", "提示：本通知仅用于公开信息风险复核，不构成内部评级、授信审批或资产风险分类结论。"])
    markdown = "\n".join([f"## 风险情报预警 · {risk.risk_level}", f"> **监测主体：** {_display_name(entity)}  ", f"> **风险类型：** {risk.risk_category or '待分类'}  ", f"> **事件：** {risk.title}", "", f"**内容摘要**  \n{risk.summary or '未取得正文摘要。'}", "", f"**风险提示**  \n{risk.impact_analysis or '请核验事件范围、本行敞口及后续披露。'}", "", f"[查看原始来源]({risk.source_url})" if risk.source_url else "原始来源待补充", "", "*仅用于公开信息风险复核，不构成内部评级或授信审批结论。*"])
    return subject, body, markdown


def _send_email(subject: str, body: str, config: dict[str, Any]) -> None:
    recipients = [item.strip() for item in str(config.get("alert_email_to") or "").split(",") if item.strip()]
    sender = str(config.get("smtp_from") or config.get("smtp_username") or "").strip()
    if not recipients or not sender:
        raise ValueError("邮件收件人或发件人未配置")
    message = MIMEText(body, "plain", "utf-8"); message["Subject"] = Header(subject, "utf-8"); message["From"] = sender; message["To"] = ", ".join(recipients)
    with smtplib.SMTP(str(config.get("smtp_host") or ""), int(config.get("smtp_port") or 587), timeout=15) as client:
        if bool(config.get("smtp_use_starttls", True)): client.starttls()
        if str(config.get("smtp_username") or "").strip(): client.login(str(config["smtp_username"]), str(config.get("smtp_password") or ""))
        client.sendmail(sender, recipients, message.as_string())


def _send_wecom(markdown: str, config: dict[str, Any]) -> None:
    response = get_http_client().post(str(config.get("webhook_url") or ""), json={"msgtype": "markdown", "markdown": {"content": markdown}}, timeout=15)
    response.raise_for_status(); payload = response.json()
    if payload.get("errcode", 0) != 0: raise RuntimeError(payload.get("errmsg") or "企业微信机器人拒绝消息")


def _deliver_one(db: Session, *, entity: TargetEntity, risk: EntityRisk, channel: str) -> str:
    existing = db.query(RiskAlertDelivery).filter(RiskAlertDelivery.entity_risk_id == risk.id, RiskAlertDelivery.channel == channel).first()
    if existing and existing.status == "sent": return "skipped"
    if existing is None:
        existing = RiskAlertDelivery(entity_risk_id=risk.id, entity_id=entity.id, channel=channel); db.add(existing)
        try: db.flush()
        except IntegrityError: db.rollback(); return "skipped"
    subject, body, markdown = _message(entity, risk)
    try:
        _, config = _channel_config(db, channel)
        if channel == "email": _send_email(subject, body, config)
        elif channel == "wecom": _send_wecom(markdown, config)
        existing.status = "sent"; existing.error_message = None; existing.delivered_at = datetime.utcnow(); db.commit()
        return "sent"
    except Exception as exc:
        db.rollback(); failed = db.query(RiskAlertDelivery).filter(RiskAlertDelivery.entity_risk_id == risk.id, RiskAlertDelivery.channel == channel).first()
        if failed: failed.status = "failed"; failed.error_message = str(exc)[:1000]; db.commit()
        logger.warning("风险预警通知失败 [%s, risk=%s]: %s", channel, risk.id, exc); return "failed"


def dispatch_entity_risk_alerts(db: Session, risks: Iterable[EntityRisk]) -> dict[str, int]:
    result = {"sent": 0, "failed": 0, "skipped": 0}; policy = _policy(db); channels = _active_channels(db)
    if not (policy.get("high_risk_news") or policy.get("entity_ids")) or not channels: return result
    for risk in risks:
        if not _eligible(risk, policy): continue
        entity = db.get(TargetEntity, risk.entity_id)
        if entity:
            for channel in channels:
                outcome = _deliver_one(db, entity=entity, risk=risk, channel=channel); result[outcome] = result.get(outcome, 0) + 1
    return result


def dispatch_entity_risk_alerts_async(risk_ids: Iterable[int]) -> None:
    ids = sorted({int(item) for item in risk_ids if item})
    if not ids: return
    def _worker() -> None:
        from app.database.session import SessionLocal
        db = SessionLocal()
        try: dispatch_entity_risk_alerts(db, db.query(EntityRisk).filter(EntityRisk.id.in_(ids)).all())
        except Exception: logger.exception("异步风险预警通知任务异常")
        finally: db.close()
    Thread(target=_worker, name="risk-alert-notifier", daemon=True).start()


# Modified by DingJiaye: 2026-08-28 — 用于人工发布“今日风险情报概览”与单条新闻。
def send_external_message(
    db: Session, *, subject: str, body: str, markdown: str
) -> dict[str, int]:
    """立即向已启用渠道发送一条人工触发的信息，不受自动预警范围限制。"""
    result = {"sent": 0, "failed": 0}
    for channel in _active_channels(db):
        try:
            _, config = _channel_config(db, channel)
            if channel == "email":
                _send_email(subject, body, config)
            elif channel == "wecom":
                _send_wecom(markdown, config)
            result["sent"] += 1
        except Exception as exc:
            result["failed"] += 1
            logger.warning("人工发送外部通知失败 [%s]: %s", channel, exc)
    return result
