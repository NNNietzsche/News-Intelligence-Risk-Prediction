"""主体评估「最新消息」直连：原生 RSS/JSON，或由已有 query 生成 Google News RSS。"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.services.entity_catalog import EntityProfile, EntitySourceSpec
from app.services.http_client import get_http_client
from app.services.recency import parse_published_at
from app.services.news_quality import is_malformed_news_candidate
from app.services.rss_news import (
    RssNewsCollector,
    google_news_rss_url,
    with_google_date_range,
)

logger = logging.getLogger(__name__)

TOKYO = ZoneInfo("Asia/Tokyo")
MAX_CHANNELS = 3
MAX_ITEMS_PER_FEED = 12
MAX_ITEMS_TOTAL = 16
CACHE_TTL_SECONDS = 30 * 60
FETCH_TIMEOUT = 15
DEFAULT_LOOKBACK_DAYS = 90
_UA = "RiskIntelBot/1.2 (+local; entity briefing)"

_FINANCIAL_SKIP = re.compile(
    r"有価証券|有价证券|yuho|yuka|fstatement|security.?report|security_reports|"
    r"決算短信|年度报告|annual.?report|ir/library|ir_library",
    re.I,
)
_NATIVE_RSS = re.compile(r"/rss|\brss\?|\.xml(?:$|[?#])|/atom|feeds\.|/feed(?:$|[/?#])", re.I)
_NATIVE_JSON = re.compile(r"\.json(?:$|[?#])|format=json|news\.json", re.I)

_headline_cache: dict[str, tuple[float, list["BriefingHeadline"]]] = {}


@dataclass(frozen=True)
class BriefingChannel:
    label: str
    page_url: str
    query: str | None
    kind: str  # native_rss | native_json | google_news
    source_type: str
    native_feed_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "url": self.page_url,
            "source_type": self.source_type,
            "query": self.query,
            "kind": self.kind,
            "enabled": True,
        }


@dataclass(frozen=True)
class BriefingHeadline:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    feed_label: str = ""


def is_native_feed_url(url: str) -> bool:
    return bool(_NATIVE_RSS.search(url or ""))


def is_json_feed_url(url: str) -> bool:
    return bool(_NATIVE_JSON.search(url or ""))


def google_locale_for_profile(profile: EntityProfile | None) -> tuple[str, str, str]:
    region = (profile.region if profile else "") or ""
    code = (profile.stock_code if profile else "") or ""
    if "日本" in region or code.isdigit():
        return "ja", "JP", "JP:ja"
    if "中国" in region:
        return "zh-CN", "CN", "CN:zh-Hans"
    return "en-US", "US", "US:en"


def channels_from_sources(sources: Iterable[EntitySourceSpec]) -> list[BriefingChannel]:
    channels: list[BriefingChannel] = []
    for src in sources:
        if not src.enabled:
            continue
        channel = _channel_from_source(src)
        if channel is None:
            continue
        channels.append(channel)
        if len(channels) >= MAX_CHANNELS:
            break
    return channels


def resolve_briefing_channels(profile: EntityProfile | None) -> list[BriefingChannel]:
    if profile is None:
        return []
    enabled_briefing = [src for src in profile.briefing_sources if src.enabled]
    derived = _derived_news_sources(profile)
    seen_urls = {src.url for src in enabled_briefing}
    sources = list(enabled_briefing)
    for src in derived:
        if src.url in seen_urls:
            continue
        sources.append(src)
        seen_urls.add(src.url)
    if not sources:
        fallback = _fallback_name_query_source(profile)
        if fallback:
            sources.append(fallback)
    return channels_from_sources(sources)


def _fallback_name_query_source(profile: EntityProfile) -> EntitySourceSpec | None:
    names = [name for name in profile.all_names if str(name).strip()][:4]
    if not names:
        return None
    quoted = " OR ".join(f'"{name}"' if " " in name else name for name in names)
    return EntitySourceSpec(
        label="跨媒体主体检索",
        url="https://news.google.com/",
        source_type="media",
        relation="direct",
        priority=20,
        query=f"({quoted})",
        enabled=True,
    )


def fetch_briefing_headlines(
    channels: list[BriefingChannel],
    *,
    report_date: date,
    profile: EntityProfile | None = None,
    live: bool = True,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[BriefingHeadline]:
    if not channels:
        return []
    locale = google_locale_for_profile(profile)
    collected: list[BriefingHeadline] = []
    if not live:
        return []
    window = max(1, int(lookback_days or DEFAULT_LOOKBACK_DAYS))
    jobs = [
        (
            ch,
            _feed_url(
                ch,
                report_date=report_date,
                locale=locale,
                lookback_days=window,
                date_scope=True,
            ),
        )
        for ch in channels
    ]
    workers = min(3, len(jobs)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _fetch_channel_cached,
                channel,
                feed_url,
                report_date,
                True,
                recent_days=window,
            ): channel
            for channel, feed_url in jobs
        }
        for future in as_completed(futures):
            channel = futures[future]
            try:
                collected.extend(future.result())
            except Exception as exc:
                logger.warning("最新消息直连拉取失败 %s: %s", channel.label, exc)

    # 带日期范围的 Google 查询偶发空结果：去掉 after/before 再拉一次，仍按观察期筛选。
    if not collected:
        retry_jobs = [
            (
                ch,
                _feed_url(
                    ch,
                    report_date=report_date,
                    locale=locale,
                    lookback_days=window,
                    date_scope=False,
                ),
            )
            for ch in channels
            if ch.kind == "google_news"
        ]
        if retry_jobs:
            logger.info("最新消息：近三个月 Google 无结果，去掉日界重试 %s 路", len(retry_jobs))
            with ThreadPoolExecutor(max_workers=min(3, len(retry_jobs)) or 1) as pool:
                futures = {
                    pool.submit(
                        _fetch_channel_cached,
                        channel,
                        feed_url,
                        report_date,
                        True,
                        recent_days=window,
                    ): channel.label
                    for channel, feed_url in retry_jobs
                }
                for future in as_completed(futures):
                    label = futures[future]
                    try:
                        collected.extend(future.result())
                    except Exception as exc:
                        logger.warning("最新消息放宽日界重试失败 %s: %s", label, exc)
    return _dedupe_headlines(collected)[:MAX_ITEMS_TOTAL]


def _derived_news_sources(profile: EntityProfile) -> list[EntitySourceSpec]:
    picked: list[EntitySourceSpec] = []
    for src in sorted(profile.sources, key=lambda item: -int(item.priority or 0)):
        if not src.enabled:
            continue
        if src.source_type not in {"official", "media"}:
            continue
        blob = " ".join(filter(None, (src.label, src.url, src.query)))
        if _FINANCIAL_SKIP.search(blob):
            continue
        if not src.query and not is_native_feed_url(src.url) and not is_json_feed_url(src.url):
            continue
        picked.append(src)
        if len(picked) >= MAX_CHANNELS:
            break
    return picked


def _channel_from_source(src: EntitySourceSpec) -> BriefingChannel | None:
    url = (src.url or "").strip()
    query = (src.query or "").strip() or None
    if is_native_feed_url(url):
        kind = "native_rss"
        native = url
    elif is_json_feed_url(url):
        kind = "native_json"
        native = url
    elif query:
        kind = "google_news"
        native = None
    else:
        return None
    return BriefingChannel(
        label=src.label,
        page_url=url,
        query=query,
        kind=kind,
        source_type=src.source_type,
        native_feed_url=native,
    )


def _feed_url(
    channel: BriefingChannel,
    *,
    report_date: date,
    locale: tuple[str, str, str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    date_scope: bool = True,
) -> str:
    if channel.kind in {"native_rss", "native_json"}:
        return channel.native_feed_url or channel.page_url
    query = channel.query or ""
    if date_scope:
        start = report_date - timedelta(days=max(0, int(lookback_days)))
        query = with_google_date_range(query, start, report_date)
    hl, gl, ceid = locale
    return google_news_rss_url(query, hl=hl, gl=gl, ceid=ceid)


def _fetch_channel_cached(
    channel: BriefingChannel,
    feed_url: str,
    report_date: date,
    live: bool = True,
    *,
    recent_days: int | None = None,
) -> list[BriefingHeadline]:
    scope = f"d{recent_days}" if recent_days is not None else "day"
    cache_key = f"{channel.kind}|{feed_url}|{report_date.isoformat()}|{scope}"
    now = time.time()
    cached = _headline_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    if not live:
        return []
    window = recent_days if recent_days is not None else DEFAULT_LOOKBACK_DAYS
    items = _fetch_channel(channel, feed_url, report_date, recent_days=window)
    _headline_cache[cache_key] = (now + CACHE_TTL_SECONDS, items)
    return items


def _fetch_channel(
    channel: BriefingChannel,
    feed_url: str,
    report_date: date,
    *,
    recent_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[BriefingHeadline]:
    if channel.kind == "native_json":
        raw = _fetch_json_items(feed_url, label=channel.label)
    else:
        raw = _fetch_rss_items(feed_url, label=channel.label)
    out: list[BriefingHeadline] = []
    for item in raw:
        if is_malformed_news_candidate(item.title, item.snippet):
            continue
        if not _within_recent_days(item.published_at, report_date, recent_days):
            continue
        out.append(item)
        if len(out) >= MAX_ITEMS_PER_FEED:
            break
    return out


def _fetch_rss_items(feed_url: str, *, label: str) -> list[BriefingHeadline]:
    collector = RssNewsCollector(timeout=FETCH_TIMEOUT, retry_attempts=2, retry_backoff=0.6)
    hits, health = collector._fetch_feed_safe(feed_url, feed_label=label)
    if not health.ok:
        return []
    return [
        BriefingHeadline(
            title=hit.title,
            url=hit.url or "",
            snippet=hit.snippet or "",
            published_at=hit.published_at,
            feed_label=label,
        )
        for hit in hits
    ]


def _fetch_json_items(feed_url: str, *, label: str) -> list[BriefingHeadline]:
    client = get_http_client()
    resp = client.get(
        feed_url,
        headers={"User-Agent": _UA, "Accept": "application/json, */*"},
        timeout=FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        found = data.get("items") or data.get("articles") or data.get("news") or data.get("entries")
        rows = found if isinstance(found, list) else []
    else:
        rows = []
    out: list[BriefingHeadline] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("headline") or "").strip()
        if not title:
            continue
        url = str(row.get("url") or row.get("link") or "").strip()
        snippet = str(row.get("summary") or row.get("description") or row.get("snippet") or "")
        if is_malformed_news_candidate(title, snippet):
            continue
        published = row.get("published_at") or row.get("published") or row.get("date") or row.get("pubDate")
        published_at = str(published).strip() if published else None
        out.append(
            BriefingHeadline(
                title=title,
                url=url,
                # 供“近三个月公开信息事件”的内容详情直接展示，不截断信源正文。
                snippet=snippet,
                published_at=published_at,
                feed_label=label,
            )
        )
        if len(out) >= MAX_ITEMS_PER_FEED:
            break
    return out


def _within_recent_days(published_at: str | None, report_date: date, days: int) -> bool:
    """无日期时保留；有日期则落在 [report_date-days, report_date]。"""
    if not published_at:
        return True
    dt = parse_published_at(published_at)
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    day = dt.astimezone(TOKYO).date()
    start = report_date - timedelta(days=max(0, int(days)))
    return start <= day <= report_date


def _dedupe_headlines(items: list[BriefingHeadline]) -> list[BriefingHeadline]:
    seen: set[str] = set()
    out: list[BriefingHeadline] = []
    for item in items:
        key = (item.url or item.title).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
