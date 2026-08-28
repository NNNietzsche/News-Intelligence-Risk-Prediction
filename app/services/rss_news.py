"""RSS / Google News 近 24 小时资讯采集（支持外置 YAML 配置）。"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from time import mktime
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx

from app.config import get_settings
from app.services.dedup import content_fingerprint
from app.services.http_client import get_http_client
from app.services.http_retry import with_retries
from app.services.recency import is_within_hours, parse_published_at
from app.services.rss_config import RssQuerySpec, load_rss_config

logger = logging.getLogger(__name__)

_UA = "RiskIntelBot/1.2 (+local; RSS collector)"
# Google News 对机器人 UA 常回 503；用常见浏览器头。
_GOOGLE_NEWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
GOOGLE_LOOKBACK_DAYS = 3
_DAY_SCOPE_RE = re.compile(r"\s*(?:after|before):\S+", re.I)


@dataclass
class RssNewsItem:
    title: str
    url: str
    snippet: str
    published_at: Optional[str] = None
    source_domain: Optional[str] = None
    feed_label: Optional[str] = None
    fingerprint: Optional[str] = None
    # Google News <source> 或标题后缀解析出的真实媒体名
    publisher: Optional[str] = None
    # 主体目录赋予的采集范围；用于阻止跨主体误归属。
    entity_key: Optional[str] = None
    relation: str = "unscoped"
    source_type: str = "media"
    configured_source_url: Optional[str] = None


@dataclass
class FeedHealth:
    label: str
    url: str
    ok: bool
    item_count: int = 0
    error: Optional[str] = None


@dataclass
class CollectResult:
    items: list[RssNewsItem] = field(default_factory=list)
    feed_health: list[FeedHealth] = field(default_factory=list)
    fetch_ok: int = 0
    fetch_errors: int = 0


@dataclass
class _FeedJob:
    """单源拉取任务；order 越小越优先合并。"""

    order: int
    label: str
    url: str
    per_feed_limit: int
    allow_unknown: bool
    entity_key: Optional[str] = None
    relation: str = "unscoped"
    source_type: str = "media"
    configured_source_url: Optional[str] = None


def google_date_range_clause(start: date, end: date) -> str:
    """Google News 日期范围：after 含起始日，before 为结束日次日。"""
    if end < start:
        start, end = end, start
    nxt = end + timedelta(days=1)
    return f"after:{start.isoformat()} before:{nxt.isoformat()}"


def google_day_range_clause(day: date) -> str:
    """Google News 日界：after 含当日，before 为次日（按公历日，与东京日历日对齐使用）。"""
    return google_date_range_clause(day, day)


def with_google_date_range(query: str, start: Optional[date], end: Optional[date]) -> str:
    """为 Google 查询附加起止日期；已含 after:/before: 时不重复。"""
    q = (query or "").strip()
    if not start or not end or not q:
        return q
    low = q.lower()
    if "after:" in low or "before:" in low:
        return q
    return f"{q} {google_date_range_clause(start, end)}"


def with_google_day_scope(query: str, day: Optional[date]) -> str:
    """为 Google 查询附加按日范围；已含 after:/before: 时不重复。"""
    return with_google_date_range(query, day, day)


def strip_google_day_scope(query: str) -> str:
    """去掉 after:/before:，便于按日无结果时放宽重试。"""
    return _DAY_SCOPE_RE.sub("", query or "").strip()


def is_google_news_url(url: str) -> bool:
    return "news.google." in (url or "").lower()


def feed_request_headers(feed_url: str) -> dict[str, str]:
    if is_google_news_url(feed_url):
        return {
            "User-Agent": _GOOGLE_NEWS_UA,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,zh-CN;q=0.7",
        }
    return {
        "User-Agent": _UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }


def google_news_rss_url(
    query: str,
    *,
    hl: str = "zh-CN",
    gl: str = "CN",
    ceid: str = "CN:zh-Hans",
) -> str:
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl={hl}&gl={gl}&ceid={ceid}"
    )


class RssNewsCollector:
    """采集 Google News RSS + 精选源，并按小时窗过滤。"""

    def __init__(
        self,
        timeout: int = 25,
        retry_attempts: int = 3,
        retry_backoff: float = 1.5,
        fetch_workers: Optional[int] = None,
    ) -> None:
        self.timeout = timeout
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff
        settings = get_settings()
        path = getattr(settings, "rss_config_path", None) or "config/rss_feeds.yaml"
        self.config = load_rss_config(path)
        raw_workers = fetch_workers
        if raw_workers is None:
            raw_workers = getattr(settings, "pipeline_rss_fetch_workers", 6)
        self.fetch_workers = max(1, int(raw_workers or 1))

    def collect_for_module(
        self,
        module_code: str,
        *,
        hours: int = 24,
        max_items: int = 40,
        allow_unknown: bool = False,
        entity_key: str | None = None,
        extra_queries: list[RssQuerySpec] | None = None,
        calendar_day: date | None = None,
    ) -> list[RssNewsItem]:
        return self.collect_detailed(
            module_code,
            hours=hours,
            max_items=max_items,
            allow_unknown=allow_unknown,
            entity_key=entity_key,
            extra_queries=extra_queries,
            calendar_day=calendar_day,
        ).items

    def collect_detailed(
        self,
        module_code: str,
        *,
        hours: int = 24,
        max_items: int = 40,
        allow_unknown: bool = False,
        entity_key: str | None = None,
        extra_queries: list[RssQuerySpec] | None = None,
        calendar_day: date | None = None,
        skip_direct_feeds: bool | None = None,
        only_extra_queries: bool = False,
        raise_on_all_fail: bool = True,
    ) -> CollectResult:
        code = module_code.upper()
        seen: set[str] = set()
        result = CollectResult()
        cfg = self.config
        # 历史日补采：直连 live RSS 通常不含旧日内容，跳过以免污染 primary_valid
        if skip_direct_feeds is None:
            skip_direct_feeds = calendar_day is not None

        # 1) Google News 查询（按 priority 降序定义顺序，并发拉取后按序合并）
        if only_extra_queries:
            configured_queries = list(extra_queries or [])
        else:
            configured_queries = list(cfg.queries.get(code, [])) + list(extra_queries or [])
        queries = sorted(
            [
                q
                for q in configured_queries
                if q.enabled and (not entity_key or not q.entity_key or q.entity_key == entity_key)
            ],
            key=lambda x: x.priority,
            reverse=True,
        )
        google_jobs: list[_FeedJob] = []
        for idx, q in enumerate(queries):
            hl, gl, ceid = cfg.resolve_google_locale(
                code, hl=q.google_hl, gl=q.google_gl, ceid=q.google_ceid
            )
            scoped_q = with_google_day_scope(q.query, calendar_day)
            url = google_news_rss_url(scoped_q, hl=hl, gl=gl, ceid=ceid)
            google_jobs.append(
                _FeedJob(
                    order=idx,
                    label=q.label,
                    url=url,
                    per_feed_limit=int(q.max_items or cfg.max_items_per_feed),
                    allow_unknown=True,
                    entity_key=q.entity_key,
                    relation=q.relation,
                    source_type=q.source_type,
                    configured_source_url=q.source_url,
                )
            )
        self._fetch_and_merge_jobs(
            google_jobs,
            result,
            seen,
            module_code=code,
            hours=hours,
            max_items=max_items,
            calendar_day=calendar_day,
        )

        # 2) 直连精选源（保底；即使 Google 全挂也尽量有内容）
        if not only_extra_queries and not skip_direct_feeds and len(result.items) < max_items:
            feeds = sorted(
                [
                    f
                    for f in cfg.feeds
                    if f.enabled
                    and code in f.modules
                    and (not entity_key or not f.entity_key or f.entity_key == entity_key)
                ],
                key=lambda x: x.priority,
                reverse=True,
            )
            direct_jobs: list[_FeedJob] = []
            for idx, feed in enumerate(feeds):
                feed_url = feed.url
                if feed.feed_type == "google":
                    hl, gl, ceid = cfg.resolve_google_locale(
                        code, hl=feed.google_hl, gl=feed.google_gl, ceid=feed.google_ceid
                    )
                    scoped = with_google_day_scope(feed.url, calendar_day)
                    feed_url = google_news_rss_url(scoped, hl=hl, gl=gl, ceid=ceid)
                direct_jobs.append(
                    _FeedJob(
                        order=idx,
                        label=feed.label,
                        url=feed_url,
                        per_feed_limit=int(feed.max_items or cfg.max_items_per_feed),
                        allow_unknown=False,
                        entity_key=feed.entity_key,
                        relation=feed.relation,
                        source_type=feed.source_type,
                        configured_source_url=feed.url,
                    )
                )
            self._fetch_and_merge_jobs(
                direct_jobs,
                result,
                seen,
                module_code=code,
                hours=hours,
                max_items=max_items,
                calendar_day=calendar_day,
            )

        logger.info(
            "RSS 模块 %s 采集 %d 条（%dh 窗，成功源 %d，失败源 %d，workers=%d）",
            code,
            len(result.items),
            hours,
            result.fetch_ok,
            result.fetch_errors,
            self.fetch_workers,
        )
        if raise_on_all_fail and not result.items and result.fetch_errors and not result.fetch_ok:
            raise RuntimeError(f"RSS 全部源请求失败（{result.fetch_errors} 个）")
        return result

    def _fetch_and_merge_jobs(
        self,
        jobs: list[_FeedJob],
        result: CollectResult,
        seen: set[str],
        *,
        module_code: str,
        hours: int,
        max_items: int,
        calendar_day: date | None = None,
    ) -> None:
        if not jobs or len(result.items) >= max_items:
            return

        fetched: dict[int, tuple[list[RssNewsItem], FeedHealth, _FeedJob]] = {}
        workers = min(self.fetch_workers, len(jobs))

        if workers <= 1:
            for job in jobs:
                hits, health = self._fetch_feed_safe(job.url, feed_label=job.label)
                fetched[job.order] = (hits, health, job)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._fetch_feed_safe, job.url, feed_label=job.label): job
                    for job in jobs
                }
                for fut in as_completed(futures):
                    job = futures[fut]
                    try:
                        hits, health = fut.result()
                    except Exception as exc:
                        logger.warning("RSS 并发任务异常 %s: %s", job.label, exc)
                        hits, health = [], FeedHealth(
                            label=job.label, url=job.url, ok=False, error=str(exc)[:300]
                        )
                    fetched[job.order] = (hits, health, job)

        for order in sorted(fetched):
            if len(result.items) >= max_items:
                break
            hits, health, job = fetched[order]
            for hit in hits:
                hit.entity_key = job.entity_key
                hit.relation = job.relation
                hit.source_type = job.source_type
                hit.configured_source_url = job.configured_source_url
            result.feed_health.append(health)
            if health.ok:
                result.fetch_ok += 1
            else:
                result.fetch_errors += 1
            self._merge_hits(
                hits[: job.per_feed_limit],
                result.items,
                seen,
                module_code=module_code,
                hours=hours,
                allow_unknown=job.allow_unknown,
                max_items=max_items,
                calendar_day=calendar_day,
            )

    def _merge_hits(
        self,
        hits: list[RssNewsItem],
        items: list[RssNewsItem],
        seen: set[str],
        *,
        module_code: str,
        hours: int,
        allow_unknown: bool,
        max_items: int,
        calendar_day: date | None = None,
    ) -> None:
        for hit in hits:
            fp = hit.fingerprint or content_fingerprint(
                module_code=module_code,
                title=hit.title,
                url=hit.url,
                published_at=hit.published_at,
            )
            hit.fingerprint = fp
            if fp in seen:
                continue
            if not is_within_hours(hit.published_at, hours, allow_unknown=allow_unknown):
                continue
            if calendar_day is not None:
                dt = parse_published_at(hit.published_at)
                if dt is None:
                    # 按日 Google 查询结果允许缺发布时间，后续入库再校验
                    if not allow_unknown:
                        continue
                else:
                    from app.timeutil import TOKYO

                    local = dt.astimezone(TOKYO).replace(tzinfo=None)
                    if local.date() != calendar_day:
                        continue
            seen.add(fp)
            items.append(hit)
            if len(items) >= max_items:
                return

    def _fetch_feed_safe(self, feed_url: str, *, feed_label: str) -> tuple[list[RssNewsItem], FeedHealth]:
        try:
            hits = self._fetch_feed(feed_url, feed_label=feed_label)
            return hits, FeedHealth(label=feed_label, url=feed_url, ok=True, item_count=len(hits))
        except Exception as exc:
            logger.warning("RSS 拉取失败 %s: %s", feed_url, exc)
            return [], FeedHealth(
                label=feed_label, url=feed_url, ok=False, error=str(exc)[:300]
            )

    def _fetch_feed(self, feed_url: str, *, feed_label: str) -> list[RssNewsItem]:
        try:
            import feedparser
        except ImportError:
            logger.warning("未安装 feedparser，跳过 RSS 采集")
            raise RuntimeError("未安装 feedparser，请执行: pip install feedparser") from None

        def _get() -> bytes:
            client = get_http_client()
            resp = client.get(
                feed_url,
                headers=feed_request_headers(feed_url),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.content

        raw = with_retries(
            _get,
            attempts=self.retry_attempts,
            backoff_seconds=self.retry_backoff,
            label=f"RSS:{feed_label}",
        )

        parsed = feedparser.parse(raw)
        out: list[RssNewsItem] = []
        for entry in getattr(parsed, "entries", []) or []:
            title = str(getattr(entry, "title", "") or "").strip() or "无标题"
            link = str(getattr(entry, "link", "") or "").strip()
            summary = str(getattr(entry, "summary", "") or getattr(entry, "description", "") or "")
            # 保留 RSS 提供的完整正文/description；页面的“内容详情”直接展示该字段，
            # 不在采集阶段截成短摘要。
            summary = re_strip_tags(summary)
            published = _entry_published(entry)
            domain = urlparse(link).netloc if link else None
            publisher = _entry_publisher(entry, title=title, snippet=summary, domain=domain)
            out.append(
                RssNewsItem(
                    title=title,
                    url=link,
                    snippet=summary,
                    published_at=published.isoformat() if published else None,
                    source_domain=domain,
                    feed_label=feed_label,
                    publisher=publisher,
                )
            )
        return out


def re_strip_tags(html: str) -> str:
    text = re.sub(r"(?is)<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _entry_publisher(
    entry,
    *,
    title: str,
    snippet: str,
    domain: Optional[str],
) -> Optional[str]:
    """尽量解析真实媒体名（Google News <source> / 标题后缀 / 域名）。"""
    src = getattr(entry, "source", None)
    if src is not None:
        name = ""
        if isinstance(src, dict):
            name = str(src.get("title") or "").strip()
        else:
            name = str(getattr(src, "title", "") or "").strip()
        if name:
            return name[:128]

    # Google News 常见标题：正文 - 媒体名
    if " - " in (title or ""):
        tail = title.rsplit(" - ", 1)[-1].strip()
        if 1 < len(tail) <= 64 and "http" not in tail.lower():
            return tail

    # 摘要里偶发 "...  MediaName"
    if snippet:
        import re

        m = re.search(r"(?:\u00a0|\s){2,}([^\n|]{2,64})\s*$", snippet)
        if m:
            return m.group(1).strip()[:128]

    if domain and "news.google." not in domain.lower():
        return domain.removeprefix("www.")[:128]
    return None


def _entry_published(entry) -> Optional[datetime]:
    for attr in ("published", "updated", "created"):
        val = getattr(entry, attr, None)
        if val:
            dt = parse_published_at(val)
            if dt:
                return dt
    for attr in ("published_parsed", "updated_parsed"):
        struct = getattr(entry, attr, None)
        if struct:
            try:
                return datetime.fromtimestamp(mktime(struct), tz=timezone.utc)
            except (OverflowError, TypeError, ValueError):
                continue
    return None
