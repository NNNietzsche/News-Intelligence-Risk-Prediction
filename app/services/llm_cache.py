"""LLM 结构化结果缓存（按材料 hash，减少重跑失败与费用）。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.database.models import LlmResponseCache

logger = logging.getLogger(__name__)


def material_hash(text: str, *, module_code: str, source: str = "") -> str:
    payload = f"{module_code.upper()}|{source}|{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_cached_items(
    db: Session,
    *,
    material_key: str,
    max_age_hours: int = 168,
) -> Optional[list[dict[str, Any]]]:
    row = (
        db.query(LlmResponseCache)
        .filter(LlmResponseCache.material_hash == material_key)
        .first()
    )
    if not row:
        return None
    if row.created_at and datetime.utcnow() - row.created_at > timedelta(hours=max_age_hours):
        return None
    try:
        data = json.loads(row.response_json or "[]")
        if isinstance(data, list):
            logger.info("LLM 缓存命中: %s…", material_key[:12])
            return data
    except json.JSONDecodeError:
        return None
    return None


def set_cached_items(
    db: Session,
    *,
    material_key: str,
    module_code: str,
    source: str,
    items: list[dict[str, Any]],
) -> None:
    payload = json.dumps(items, ensure_ascii=False)
    row = (
        db.query(LlmResponseCache)
        .filter(LlmResponseCache.material_hash == material_key)
        .first()
    )
    if row:
        row.response_json = payload
        row.module_code = module_code.upper()
        row.source = source
        row.created_at = datetime.utcnow()
    else:
        db.add(
            LlmResponseCache(
                material_hash=material_key,
                module_code=module_code.upper(),
                source=source,
                response_json=payload,
            )
        )
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("写入 LLM 缓存失败: %s", exc)
