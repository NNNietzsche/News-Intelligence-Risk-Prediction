"""侧栏「24H RSS 新闻情报」列表：从近期流水线 RSS 采集日志聚合。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.models import SearchLog
from app.services.recency import is_within_hours, parse_published_at
from app.services.rss_config import load_rss_config

logger = logging.getLogger(__name__)

_GOOGLE_HINT = re.compile(r"news\.google\.|google\.com/rss", re.I)


def _item_id(item: dict[str, Any], log_id: int, index: int) -> str:
    fp = (item.get("fingerprint") or "").strip()
    if fp:
        return fp
    url = (item.get("url") or "").strip()
    if url:
        return f"url:{url}"
    return f"rss-{log_id}-{index}"


def _direct_feed_labels() -> set[str]:
    """YAML 中 type=direct 的频道标签（Reuters / BBC / NHK 等）。"""
    cfg = load_rss_config()
    return {
        f.label.strip()
        for f in cfg.feeds
        if f.enabled and (f.feed_type or "direct").lower() == "direct" and f.label
    }


def _domain_label(domain: Optional[str]) -> Optional[str]:
    if not domain:
        return None
    host = domain.strip().lower()
    if not host or _GOOGLE_HINT.search(host):
        return None
    if host.startswith("www."):
        host = host[4:]
    # 常用站点友好名
    friendly = {
        "reuters.com": "Reuters",
        "feeds.reuters.com": "Reuters",
        "bbc.co.uk": "BBC",
        "www.bbc.co.uk": "BBC",
        "bbci.co.uk": "BBC",
        "nhk.or.jp": "NHK",
        "www3.nhk.or.jp": "NHK",
        "bloomberg.com": "Bloomberg",
        "nikkei.com": "日経",
        "www.nikkei.com": "日経",
        "prtimes.jp": "PR TIMES",
    }
    if host in friendly:
        return friendly[host]
    for suffix, name in (
        (".reuters.com", "Reuters"),
        (".bbc.co.uk", "BBC"),
        (".bbci.co.uk", "BBC"),
        (".nhk.or.jp", "NHK"),
        (".nikkei.com", "日経"),
    ):
        if host.endswith(suffix) or host == suffix.lstrip("."):
            return name
    return host


def _publisher_from_title(title: str) -> Optional[str]:
    if " - " not in title:
        return None
    tail = title.rsplit(" - ", 1)[-1].strip()
    if 1 < len(tail) <= 64 and "http" not in tail.lower():
        return tail
    return None


def _publisher_from_snippet(snippet: str) -> Optional[str]:
    if not snippet:
        return None
    m = re.search(r"(?:\u00a0|\s){2,}([^\n|]{2,64})\s*$", snippet)
    if m:
        return m.group(1).strip()[:128]
    return None


def _source_name(item: dict[str, Any], *, direct_labels: set[str]) -> str:
    """展示真实媒体/站点名，避免用 Google 检索主题（如「原油 LNG」）当来源。"""
    feed = (item.get("feed") or item.get("feed_label") or "").strip()
    publisher = (item.get("publisher") or item.get("source_title") or "").strip()
    domain = (item.get("source_domain") or "").strip()
    title = (item.get("title") or "").strip()
    snippet = (item.get("snippet") or "").strip()
    url = (item.get("url") or "").strip()

    # 1) 直连媒体频道（路透/BBC/NHK…）直接用配置标签
    if feed in direct_labels:
        return feed

    # 2) 采集时写入的 publisher（Google News <source>）
    if publisher and not _GOOGLE_HINT.search(publisher):
        return publisher

    # 3) 非 Google 域名 → 站点名
    label = _domain_label(domain)
    if label:
        return label
    if url:
        label = _domain_label(urlparse(url).netloc)
        if label:
            return label

    # 4) 从标题/摘要回退解析媒体名（兼容旧日志无 publisher 字段）
    from_title = _publisher_from_title(title)
    if from_title:
        return from_title
    from_snip = _publisher_from_snippet(snippet)
    if from_snip:
        return from_snip

    # 5) 仍失败时：若 feed 不像 Google 主题查询标签，可展示；否则泛称
    if feed and feed not in direct_labels:
        # 主题查询标签（原油 LNG / 中东地缘）不应当作媒体名展示
        return "Google News"
    return feed or "RSS"


def list_rss_sources_24h(
    db: Session,
    *,
    hours: Optional[int] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """返回近 N 小时内 RSS 管道抓取到的去重动态列表。

    数据来自各模块近期 `search_logs` 中 `[RSS近…]` 原始条目，
    避免侧栏打开时实时打全量 RSS。
    来源标签优先展示直连媒体名或真实出版方，而非 Google 检索主题。
    """
    settings = get_settings()
    window = int(hours if hours is not None else getattr(settings, "news_window_hours", 24) or 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window)

    # SQLite 中 created_at 多为 naive UTC，比较时统一去 tzinfo
    cutoff_naive = cutoff.replace(tzinfo=None)
    direct_labels = _direct_feed_labels()

    logs = (
        db.query(SearchLog)
        .filter(
            SearchLog.query_text.like("[RSS%"),
            SearchLog.created_at >= cutoff_naive,
        )
        .order_by(SearchLog.created_at.desc())
        .limit(40)
        .all()
    )

    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    for log in logs:
        raw = log.raw_response or ""
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            if not title:
                continue
            pub = parse_published_at(item.get("published_at"))
            # 有发布时间则按时效过滤；无发布时间则保留（采集日志本身已在窗口内）
            if pub is not None and not is_within_hours(pub, hours=window):
                continue
            rid = _item_id(item, log.id, idx)
            if rid in seen:
                continue
            seen.add(rid)
            out.append(
                {
                    "id": rid,
                    "title": title,
                    "source_name": _source_name(item, direct_labels=direct_labels),
                    "published_at": pub,
                    "type": "rss",
                    "is_selected": True,
                    "url": (item.get("url") or None),
                }
            )
            if len(out) >= limit:
                return out

    return out
