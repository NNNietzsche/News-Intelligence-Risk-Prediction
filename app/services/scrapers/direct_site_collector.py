"""无 RSS 直连网站：按 YAML CSS 选择器抓取列表页。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from app.services.direct_site_config import DirectSiteSpec, DirectSitesConfig, load_direct_sites_config
from app.services.http_client import get_http_client
from app.services.http_retry import with_retries
from app.services.recency import parse_published_at

logger = logging.getLogger(__name__)

_UA = "RiskIntelBot/1.3 (+local; direct site HTML collector)"


@dataclass
class DirectSiteHit:
    title: str
    url: str
    snippet: str
    published_at: Optional[str]
    source_domain: str
    feed_label: str


class DirectSiteCollector:
    """抓取配置中的 HTML 列表页，输出与 RSS/TDnet 同形的资讯条目。"""

    def __init__(
        self,
        *,
        config: Optional[DirectSitesConfig] = None,
        config_path: Optional[str] = None,
        retry_attempts: int = 3,
        retry_backoff: float = 1.5,
    ) -> None:
        self.config = config or load_direct_sites_config(config_path)
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff

    def collect_for_module(
        self,
        module_code: str,
        *,
        hours: int = 24,
        max_items: int = 36,
    ) -> list[DirectSiteHit]:
        sites = self.config.sites_for_module(module_code)
        if not sites:
            return []

        cutoff = datetime.utcnow() - timedelta(hours=max(1, hours))
        hits: list[DirectSiteHit] = []
        seen: set[str] = set()

        for site in sites:
            limit = int(site.max_items or self.config.max_items_per_site or 20)
            try:
                page_hits = self._fetch_site(site, limit=limit)
            except Exception as exc:
                logger.warning("直连站点采集失败 [%s]: %s", site.label, exc)
                continue
            for hit in page_hits:
                key = hit.url or f"{hit.feed_label}|{hit.title}|{hit.published_at}"
                if key in seen:
                    continue
                if hit.published_at:
                    pub = parse_published_at(hit.published_at)
                    if pub is not None and pub.replace(tzinfo=None) < cutoff:
                        continue
                seen.add(key)
                hits.append(hit)
                if len(hits) >= max_items:
                    return hits
        return hits

    def _fetch_site(self, site: DirectSiteSpec, *, limit: int) -> list[DirectSiteHit]:
        if site.site_type not in ("html_list", "html", "list"):
            logger.warning("不支持的直连类型 %s [%s]，已跳过", site.site_type, site.label)
            return []

        html = self._get_html(site)
        if not html:
            return []
        return self._parse_html_list(html, site, limit=limit)

    def _get_html(self, site: DirectSiteSpec) -> str:
        headers = {"User-Agent": _UA, **(site.headers or {})}
        timeout = float(self.config.timeout_seconds or 30)

        def _get() -> str:
            client = get_http_client()
            resp = client.get(site.list_url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if site.encoding:
                resp.encoding = site.encoding
            elif not resp.encoding:
                resp.encoding = "utf-8"
            return resp.text

        return with_retries(
            _get,
            attempts=self.retry_attempts,
            backoff_seconds=self.retry_backoff,
            label=f"direct-site:{site.label}",
        )

    def _parse_html_list(
        self, html: str, site: DirectSiteSpec, *, limit: int
    ) -> list[DirectSiteHit]:
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "直连 HTML 采集需要 beautifulsoup4，请 pip install beautifulsoup4"
            ) from exc

        soup = BeautifulSoup(html, "html.parser")
        nodes = soup.select(site.item_selector)
        base = site.base_url or self._origin(site.list_url)
        domain = site.source_domain or urlparse(base or site.list_url).netloc
        hits: list[DirectSiteHit] = []

        for node in nodes:
            title_el = node.select_one(site.title_selector)
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not title:
                continue

            link_el = node.select_one(site.link_selector) or title_el
            href = (link_el.get(site.link_attr) or "").strip() if link_el else ""
            if not href:
                continue
            url = urljoin(base + "/", href) if base else href

            published_at = self._extract_date(node, site)
            snippet = ""
            if site.snippet_selector:
                sn_el = node.select_one(site.snippet_selector)
                if sn_el:
                    snippet = sn_el.get_text(" ", strip=True)
            if not snippet:
                snippet = f"【{site.label}】{title}"
                if published_at:
                    snippet += f"。发布时间：{published_at}"

            hits.append(
                DirectSiteHit(
                    title=title,
                    url=url,
                    snippet=snippet,
                    published_at=published_at,
                    source_domain=domain,
                    feed_label=site.label,
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def _extract_date(self, node, site: DirectSiteSpec) -> Optional[str]:
        if not site.date_selector:
            return None
        el = node.select_one(site.date_selector)
        if not el:
            return None
        raw = ""
        if site.date_attr:
            raw = (el.get(site.date_attr) or "").strip()
        if not raw:
            raw = el.get_text(" ", strip=True)
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw or None

    @staticmethod
    def _origin(url: str) -> str:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return ""
        return f"{p.scheme}://{p.netloc}"
