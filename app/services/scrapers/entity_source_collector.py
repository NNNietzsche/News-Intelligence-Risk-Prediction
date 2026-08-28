"""主体配置中的官网/监管/交易所列表页采集器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

from app.services.entity_catalog import EntityProfile
from app.services.http_client import get_http_client
from app.services.recency import parse_published_at
from app.services.news_quality import is_malformed_news_candidate

_UA = "RiskIntelBot/1.4 (+local; entity source collector)"
_DATE = re.compile(r"(?:20\d{2}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]20\d{2})")
_CONTENT_PATH = re.compile(r"news|press|media|blog|recall|alert|announcement|release|disclosure|ir", re.I)
_SKIP = re.compile(r"^(home|menu|search|contact|privacy|terms|about|investor relations|ニュース|一覧)$", re.I)


@dataclass(frozen=True)
class EntitySourceHit:
    title: str
    url: str
    snippet: str
    published_at: str | None
    source_domain: str
    feed_label: str
    source_type: str


class EntitySourceCollector:
    """抓取已配置的直接信源；行业背景与跨媒体检索不会进入这里。"""

    def collect(self, profile: EntityProfile | None, *, hours: int, max_items: int) -> list[EntitySourceHit]:
        if profile is None:
            return []
        cutoff = datetime.utcnow() - timedelta(hours=max(1, hours))
        hits: list[EntitySourceHit] = []
        seen: set[str] = set()
        sources = [s for s in profile.sources if s.enabled and s.relation == "direct" and s.source_type in {"official", "regulatory", "exchange"}]
        for source in sorted(sources, key=lambda s: -int(s.priority or 0))[:5]:
            for hit in self._fetch(source.url, source.label, source.source_type)[:16]:
                if hit.url in seen:
                    continue
                published = parse_published_at(hit.published_at) if hit.published_at else None
                if published and published.replace(tzinfo=None) < cutoff:
                    continue
                seen.add(hit.url)
                hits.append(hit)
                if len(hits) >= max_items:
                    return hits
        return hits

    def _fetch(self, page_url: str, label: str, source_type: str) -> list[EntitySourceHit]:
        resp = get_http_client().get(page_url, headers={"User-Agent": _UA, "Accept": "text/html,*/*"}, timeout=20)
        resp.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        origin = urlparse(page_url).netloc
        out: list[EntitySourceHit] = []
        for anchor in soup.find_all("a", href=True):
            title = " ".join(anchor.get_text(" ", strip=True).split())
            href = str(anchor.get("href") or "").strip()
            url = urljoin(page_url, href)
            if not title or len(title) < 10 or len(title) > 260 or _SKIP.match(title):
                continue
            if urlparse(url).netloc != origin or not _CONTENT_PATH.search(url):
                continue
            parent = anchor.find_parent(["article", "li", "tr", "div"]) or anchor
            context = " ".join(parent.get_text(" ", strip=True).split())[:500]
            if is_malformed_news_candidate(title, context):
                continue
            match = _DATE.search(context)
            out.append(EntitySourceHit(title, url, context, match.group(0) if match else None, origin, label, source_type))
        return out
