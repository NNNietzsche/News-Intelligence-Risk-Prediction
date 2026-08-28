"""发布时间解析与 24 小时时效过滤。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

# 常见相对时间（中文）
_RELATIVE_CN = re.compile(
    r"(?:约)?\s*(\d+)\s*(分钟|小时|天|日|周|星期|月)前"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_published_at(value: Optional[str | datetime]) -> Optional[datetime]:
    """尽力将各类发布时间解析为 timezone-aware UTC datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    # Unix 毫秒 / 秒
    if re.fullmatch(r"\d{10,13}", text):
        ts = int(text)
        if ts > 10_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    # ISO / 常见数字格式
    candidates = [
        text,
        text.replace("Z", "+00:00"),
        text.replace("/", "-"),
    ]
    for cand in candidates:
        try:
            dt = datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y年%m月%d日 %H:%M",
        "%Y年%m月%d日",
        "%m/%d/%Y %H:%M",
        "%d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %z",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue

    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass

    # 相对时间
    m = _RELATIVE_CN.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        now = utc_now()
        if unit == "分钟":
            return now - timedelta(minutes=n)
        if unit == "小时":
            return now - timedelta(hours=n)
        if unit in ("天", "日"):
            return now - timedelta(days=n)
        if unit in ("周", "星期"):
            return now - timedelta(weeks=n)
        if unit == "月":
            return now - timedelta(days=30 * n)

    if "刚刚" in text or "刚才" in text:
        return utc_now()

    return None


def is_within_hours(
    value: Optional[str | datetime],
    hours: int = 24,
    *,
    now: Optional[datetime] = None,
    allow_unknown: bool = False,
) -> bool:
    """判断发布时间是否在最近 hours 小时内。"""
    dt = parse_published_at(value)
    if dt is None:
        return allow_unknown
    ref = now or utc_now()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (ref - dt) <= timedelta(hours=hours) and dt <= ref + timedelta(minutes=5)


def filter_recent_items(
    items: list,
    *,
    hours: int = 24,
    get_published=lambda x: getattr(x, "published_at", None),
    allow_unknown: bool = False,
) -> list:
    now = utc_now()
    return [
        item
        for item in items
        if is_within_hours(get_published(item), hours, now=now, allow_unknown=allow_unknown)
    ]
