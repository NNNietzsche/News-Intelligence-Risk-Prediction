"""东京（Asia/Tokyo）时间工具：流水线刷新时间统一按东九区墙钟写入/返回。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TOKYO = ZoneInfo("Asia/Tokyo")


def tokyo_now() -> datetime:
    """当前东京时间（naive，写入 SQLite DateTime）。"""
    return datetime.now(TOKYO).replace(tzinfo=None)


def tokyo_today() -> date:
    """当前东京日历日。"""
    return datetime.now(TOKYO).date()


def tokyo_day_tabs(days: int = 7) -> list[date]:
    """即日起连续 days 个东京日历日（含今天），由近到远。"""
    today = tokyo_today()
    n = max(1, int(days or 7))
    return [today - timedelta(days=i) for i in range(n)]


def tokyo_isoformat(dt: datetime | None, *, timespec: str = "seconds") -> str | None:
    """序列化为带 +09:00 的 ISO 字符串，供前端按东京时间解析。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        aware = dt.replace(tzinfo=TOKYO)
    else:
        aware = dt.astimezone(TOKYO)
    return aware.isoformat(timespec=timespec)


def as_tokyo(dt: datetime | None, *, assume_utc_if_naive: bool = True) -> datetime | None:
    """转换为东京墙钟（naive）。

    行业报告等字段仍用 ``datetime.utcnow`` 写入 naive UTC 时，
    展示前需按 UTC 解释再转到 Asia/Tokyo。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        if assume_utc_if_naive:
            aware = dt.replace(tzinfo=timezone.utc)
        else:
            aware = dt.replace(tzinfo=TOKYO)
    else:
        aware = dt
    return aware.astimezone(TOKYO).replace(tzinfo=None)


def format_tokyo(
    dt: datetime | None,
    fmt: str = "%Y-%m-%d %H:%M",
    *,
    empty: str = "日期未知",
    assume_utc_if_naive: bool = True,
) -> str:
    """格式化为东京时间字符串。"""
    local = as_tokyo(dt, assume_utc_if_naive=assume_utc_if_naive)
    if local is None:
        return empty
    return local.strftime(fmt)
