"""DuckDuckGo 新闻搜索（开源库 ddgs）。只使用官方 duckduckgo 后端。"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

from app.config import NEWS_WINDOW_HOURS_7X24, get_settings
from app.services.mita_search import (
    MitaSearchResponse,
    MitaSearchResultItem,
    finalize_search_items,
)
from app.services.search_fallback import allowed_ddg_backend

logger = logging.getLogger(__name__)

DDG_BACKEND = "duckduckgo"


class DuckDuckGoNewsClient:
    """新闻检索适配器，返回与秘塔相同的 SearchResponse 结构。"""

    def __init__(
        self,
        *,
        region: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self.region = (region or getattr(settings, "search_ddg_region", None) or "jp-jp").strip()
        self.timeout = timeout or settings.request_timeout_seconds
        self.backend = DDG_BACKEND
        configured = allowed_ddg_backend(getattr(settings, "search_ddg_backend", None))
        if configured != DDG_BACKEND:
            logger.warning("已忽略非 duckduckgo 的 DDG 后端配置，固定使用 %s", DDG_BACKEND)

    def search(
        self,
        query: str,
        whitelist_domains: Optional[list[str]] = None,
        blacklist_domains: Optional[list[str]] = None,
        max_results: int = 10,
        *,
        window_hours: int = 24,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> MitaSearchResponse:
        effective = _build_query(query, whitelist_domains)
        limit = max(1, min(int(max_results or 10), 20))
        timelimit = "w" if int(window_hours or 24) >= NEWS_WINDOW_HOURS_7X24 else "d"
        rows = self._news(effective, limit=limit, timelimit=timelimit)
        items = [_to_item(row) for row in rows]
        items = [item for item in items if item.url]
        filtered = finalize_search_items(items, whitelist_domains, blacklist_domains)
        logger.info(
            "DuckDuckGo 新闻补缺: q=%s region=%s timelimit=%s items=%s",
            effective[:160],
            self.region,
            timelimit,
            len(filtered),
        )
        return MitaSearchResponse(
            query=effective,
            items=filtered,
            raw={"provider": "ddg", "backend": self.backend, "timelimit": timelimit, **(extra_params or {})},
            provider="ddg",
        )

    def _news(self, query: str, *, limit: int, timelimit: str) -> list[dict[str, Any]]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise RuntimeError("未安装 ddgs，无法使用 DuckDuckGo 新闻补缺") from exc

        try:
            client = DDGS(timeout=min(30, int(self.timeout or 30)))
            rows = client.news(
                query=query,
                region=self.region,
                timelimit=timelimit,
                max_results=limit,
                backend=self.backend,
            )
        except Exception as exc:
            raise RuntimeError(f"DuckDuckGo 新闻检索失败: {exc}") from exc
        return [row for row in (rows or []) if isinstance(row, dict)]


def _build_query(query: str, whitelist_domains: Optional[list[str]]) -> str:
    wl = [d.strip() for d in (whitelist_domains or []) if d and d.strip()]
    if not wl or len(wl) > 5:
        return query
    site_part = " OR ".join(f"site:{d}" for d in wl)
    return f"{query} ({site_part})"


def _to_item(row: dict[str, Any]) -> MitaSearchResultItem:
    url = str(row.get("url") or row.get("href") or row.get("link") or "").strip()
    title = str(row.get("title") or url or "无标题").strip()
    snippet = str(row.get("body") or row.get("snippet") or row.get("excerpt") or "").strip()
    published = row.get("date") or row.get("published_at") or row.get("published")
    domain = row.get("source_domain") or row.get("domain")
    source = str(row.get("source") or "").strip()
    if not domain and url:
        domain = urlparse(url).netloc
    if not domain and source and "." in source and " " not in source:
        domain = source
    return MitaSearchResultItem(
        title=title or "无标题",
        url=url,
        snippet=snippet,
        published_at=str(published) if published else None,
        source_domain=str(domain).removeprefix("www.") if domain else None,
    )
