"""Godiva 供应链 / 品牌关联信源采集（无可用 RSS 的站点）。

覆盖：
- 加纳可可局 COCOBOD 新闻列表
- ICCO WordPress 新闻与月度可可市场报告 PDF

无法稳定准确采集（本模块会跳过并打日志）：
- ICE Cocoa Futures 行情页（反爬 / JS 动态，接口 403）
- 日本歌帝梵店铺一览（Nuxt 前端渲染，SSR 无门店明细）
- 科特迪瓦咖啡可可委员会官网 conseilcafecacao.ci（证书/鉴权问题）
  → 改由 RSS：ICI、cacao.ci
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx

from app.services.http_client import get_http_client
from app.services.http_retry import with_retries
from app.services.recency import parse_published_at

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_COCOBOD_NEWS = "https://cocobod.gh/news/"
_ICCO_POSTS = "https://www.icco.org/wp-json/wp/v2/posts"
_ICCO_STATS = "https://www.icco.org/statistics/"
_EU_MILK = (
    "https://agriculture.ec.europa.eu/data-and-analysis/markets/"
    "overviews/market-observatories/milk_en"
)
_EU_SUGAR = (
    "https://agriculture.ec.europa.eu/data-and-analysis/markets/"
    "overviews/market-observatories/sugar_en"
)
_DEPART_SALE = "https://www.depart.or.jp/store_sale/"

_STRIP_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_MONTH_JP = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")
_PDF_MONTH = re.compile(r"(20\d{2})(0[1-9]|1[0-2])zenkokupp\.pdf", re.I)


@dataclass
class GodivaSourceHit:
    title: str
    url: str
    snippet: str
    published_at: Optional[str]
    source_domain: str
    feed_label: str


class GodivaSourceCollector:
    """模块 A：Godiva 相关官方/准官方无 RSS 信源。"""

    TIMEOUT = 35
    MODULES = frozenset({"A"})

    def __init__(
        self,
        *,
        retry_attempts: int = 3,
        retry_backoff: float = 1.5,
    ) -> None:
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff

    def collect_for_module(
        self,
        module_code: str,
        *,
        hours: int = 24,
        max_items: int = 40,
    ) -> list[GodivaSourceHit]:
        if str(module_code).upper() not in self.MODULES:
            return []

        # ICCO/COCOBOD 仅为配置中的行业背景；新闻仍按 hours 过滤。
        news_cutoff = datetime.utcnow() - timedelta(hours=max(1, hours))

        hits: list[GodivaSourceHit] = []
        seen: set[str] = set()

        collectors = (
            ("COCOBOD", self._collect_cocobod),
            ("ICCO posts", self._collect_icco_posts),
            ("ICCO stats", self._collect_icco_stats_pdf),
        )
        for label, fn in collectors:
            try:
                part = fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Godiva 信源采集失败 [%s]: %s", label, exc)
                continue
            for hit in part:
                key = (hit.url or "").rstrip("/").lower() or f"{hit.feed_label}|{hit.title}"
                if key in seen:
                    continue
                if not self._pass_recency(hit, news_cutoff):
                    continue
                seen.add(key)
                hits.append(hit)
                if len(hits) >= max_items:
                    return hits

        # 明确记录无法自动准确采集的源，便于运维对照文档
        logger.info(
            "Godiva 信源跳过（无法稳定准确采集）: ICE Cocoa Futures、"
            "shop.godiva.co.jp/stores、conseilcafecacao.ci"
        )
        return hits

    def _pass_recency(
        self,
        hit: GodivaSourceHit,
        news_cutoff: datetime,
    ) -> bool:
        # 月度官方资料以页面最新为准，不做时间切窗
        if hit.feed_label.startswith("ICCO月报"):
            return True
        if not hit.published_at:
            return True
        pub = parse_published_at(hit.published_at)
        if pub is None:
            return True
        return pub.replace(tzinfo=None) >= news_cutoff

    def _client(self) -> httpx.Client:
        return get_http_client()

    def _get_text(self, url: str, *, label: str) -> str:
        def _get() -> str:
            client = self._client()
            resp = client.get(
                url,
                timeout=self.TIMEOUT,
                headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                },
            )
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            return resp.text

        return with_retries(
            _get,
            attempts=self.retry_attempts,
            backoff_seconds=self.retry_backoff,
            label=label,
        )

    def _get_json(self, url: str, *, label: str, params: Optional[dict] = None) -> Any:
        def _get():
            client = self._client()
            resp = client.get(
                url,
                params=params,
                timeout=self.TIMEOUT,
                headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                },
            )
            resp.raise_for_status()
            return resp.json()

        return with_retries(
            _get,
            attempts=self.retry_attempts,
            backoff_seconds=self.retry_backoff,
            label=label,
        )

    def _collect_cocobod(self) -> list[GodivaSourceHit]:
        from bs4 import BeautifulSoup

        html = self._get_text(_COCOBOD_NEWS, label="cocobod-news")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[GodivaSourceHit] = []
        for card in soup.select("div.card"):
            title_a = card.select_one("a.or-txt, h6.card-title")
            if title_a and title_a.name != "a":
                title_a = title_a.find_parent("a") or card.select_one("a.or-txt")
            if not title_a:
                title_a = card.select_one("a[href*='/news/']")
            if not title_a:
                continue
            title = title_a.get_text(" ", strip=True)
            if not title or title.lower() == "read more":
                h6 = card.select_one("h6.card-title")
                title = h6.get_text(" ", strip=True) if h6 else ""
            href = (title_a.get("href") or "").strip()
            if not title or not href:
                continue
            if "/news/" not in href:
                continue
            url = urljoin(_COCOBOD_NEWS, href)
            sn_el = card.select_one("p.card-text")
            snippet = sn_el.get_text(" ", strip=True) if sn_el else f"【COCOBOD】{title}"
            hits.append(
                GodivaSourceHit(
                    title=title,
                    url=url,
                    snippet=snippet,
                    published_at=None,
                    source_domain="cocobod.gh",
                    feed_label="加纳可可局 COCOBOD",
                )
            )
        return hits

    def _collect_icco_posts(self) -> list[GodivaSourceHit]:
        rows = self._get_json(
            _ICCO_POSTS,
            label="icco-posts",
            params={
                "per_page": 10,
                "_fields": "id,date,link,title,excerpt",
            },
        )
        hits: list[GodivaSourceHit] = []
        if not isinstance(rows, list):
            return hits
        for row in rows:
            title = _clean_html((row.get("title") or {}).get("rendered") or "")
            link = str(row.get("link") or "").strip()
            if not title or not link:
                continue
            excerpt = _clean_html((row.get("excerpt") or {}).get("rendered") or "")
            date_raw = str(row.get("date") or "").strip() or None
            hits.append(
                GodivaSourceHit(
                    title=title,
                    url=link,
                    snippet=excerpt or f"【ICCO】{title}",
                    published_at=date_raw,
                    source_domain="www.icco.org",
                    feed_label="国际可可组织 ICCO",
                )
            )
        return hits

    def _collect_icco_stats_pdf(self) -> list[GodivaSourceHit]:
        from bs4 import BeautifulSoup

        html = self._get_text(_ICCO_STATS, label="icco-stats")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[GodivaSourceHit] = []
        for a in soup.select("a[href$='.pdf'], a[href*='.pdf']"):
            href = (a.get("href") or "").strip()
            text = a.get_text(" ", strip=True) or ""
            if not href:
                continue
            low = f"{href} {text}".lower()
            if (
                "cocoa-market-report" not in low
                and "market report" not in low
                and "cocoa_market_report" not in low
            ):
                continue
            url = urljoin(_ICCO_STATS, href)
            title = text if text and text.lower() not in {"form", "pdf", "download"} else ""
            if not title:
                title = "ICCO Monthly Cocoa Market Report"
            elif "icco" not in title.lower():
                title = f"ICCO Monthly Cocoa Market Report — {title}"
            pub = _guess_month_from_text(title) or _guess_month_from_text(href)
            hits.append(
                GodivaSourceHit(
                    title=title,
                    url=url,
                    snippet="国际可可组织月度可可市场报告（统计页官方 PDF）。",
                    published_at=pub,
                    source_domain="www.icco.org",
                    feed_label="ICCO月报",
                )
            )
            break  # 只要最新一份
        if not hits:
            # 回退：任意含 Cocoa-Market-Report 的 PDF
            for a in soup.select("a[href*='Cocoa-Market-Report'], a[href*='cocoa-market-report']"):
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                url = urljoin(_ICCO_STATS, href)
                text = a.get_text(" ", strip=True) or "ICCO Monthly Cocoa Market Report"
                hits.append(
                    GodivaSourceHit(
                        title=text,
                        url=url,
                        snippet="国际可可组织月度可可市场报告（统计页官方 PDF）。",
                        published_at=_guess_month_from_text(text) or _guess_month_from_text(href),
                        source_domain="www.icco.org",
                        feed_label="ICCO月报",
                    )
                )
                break
        return hits

    def _collect_eu_files(self, list_url: str, label: str) -> list[GodivaSourceHit]:
        from bs4 import BeautifulSoup

        html = self._get_text(list_url, label=f"eu-{label}")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[GodivaSourceHit] = []
        prefer = ("dashboard", "market-situation", "factsheet", "balance-sheet")
        files = soup.select(".ecl-file")
        ranked: list[tuple[int, GodivaSourceHit]] = []
        for f in files:
            a = f.select_one("a[href]")
            if not a:
                continue
            href = (a.get("href") or "").strip()
            if not href:
                continue
            title_el = f.select_one(".ecl-file__title, .ecl-link__label")
            title = (
                title_el.get_text(" ", strip=True)
                if title_el
                else _filename_title(href)
            )
            if not title:
                continue
            url = urljoin(list_url, href)
            score = 0
            low = (title + " " + href).lower()
            for i, key in enumerate(prefer):
                if key in low:
                    score += 10 - i
            ranked.append(
                (
                    score,
                    GodivaSourceHit(
                        title=f"{label}：{title}",
                        url=url,
                        snippet=f"【{label}】官方资料包。来源页：{list_url}",
                        published_at=None,
                        source_domain="agriculture.ec.europa.eu",
                        feed_label=label,
                    ),
                )
            )
        ranked.sort(key=lambda x: -x[0])
        for _, hit in ranked[:4]:
            hits.append(hit)
        return hits

    def _collect_depart_sales(self) -> list[GodivaSourceHit]:
        from bs4 import BeautifulSoup

        html = self._get_text(_DEPART_SALE, label="depart-sale")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[GodivaSourceHit] = []

        for h4 in soup.select("h4.acdTgl, h4.bg_glay"):
            month_text = h4.get_text(" ", strip=True)
            m = _MONTH_JP.search(month_text)
            if not m:
                continue
            year, month = int(m.group(1)), int(m.group(2))
            pub = f"{year:04d}-{month:02d}-28T00:00:00"
            # 标题下的折叠区：取全国売上高概況 PDF
            box = h4.find_next_sibling()
            if box is None:
                box = h4.parent
            if box is None:
                continue
            overview = None
            yoy = None
            for a in box.select("a[href*='.pdf']"):
                href = (a.get("href") or "").strip()
                label = a.get_text(" ", strip=True)
                if not href:
                    continue
                url = urljoin(_DEPART_SALE, href)
                href_l = href.lower()
                # 优先全国概況（zenkokupp），避免误取东京等地区表
                if "zenkokupp" in href_l:
                    overview = (url, label, pub)
                elif overview is None and ("売上高概況" in label and "tokyo" not in href_l):
                    overview = (url, label, pub)
                if "zenkoku4" in href_l or "増減率" in label or "対前年" in label:
                    if "zenkoku4" in href_l or yoy is None:
                        yoy = (url, label, pub)
            if overview:
                url, label, published = overview
                hits.append(
                    GodivaSourceHit(
                        title=f"日本百货店协会 {year}年{month}月 全国売上高概況",
                        url=url,
                        snippet=(
                            f"【日本百货店协会】{year}年{month}月全国百货店销售额概況 PDF。"
                            "可用于观察销售端增减幅度（参考，不直接等同歌帝梵业绩）。"
                        ),
                        published_at=published,
                        source_domain="www.depart.or.jp",
                        feed_label="日本百货店协会销售额",
                    )
                )
            if yoy:
                url, label, published = yoy
                hits.append(
                    GodivaSourceHit(
                        title=f"日本百货店协会 {year}年{month}月 地区·商品别対前年増减率",
                        url=url,
                        snippet=(
                            f"【日本百货店协会】{year}年{month}月対前年増减率表。"
                            "文档建议重点关注增减幅度。"
                        ),
                        published_at=published,
                        source_domain="www.depart.or.jp",
                        feed_label="日本百货店协会销售额",
                    )
                )
            if hits:
                break  # 只取最新月份
        return hits


def _clean_html(raw: str) -> str:
    text = unescape(_STRIP_TAGS.sub(" ", raw or ""))
    return _WS.sub(" ", text).strip()


def _filename_title(href: str) -> str:
    name = urlparse(href).path.rsplit("/", 1)[-1]
    if "filename=" in href:
        m = re.search(r"filename=([^&]+)", href)
        if m:
            name = m.group(1)
    name = re.sub(r"\.(pdf|xlsx|xls)$", "", name, flags=re.I)
    return name.replace("-", " ").replace("_", " ").strip()


def _guess_month_from_text(text: str) -> Optional[str]:
    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    low = (text or "").lower()
    m = re.search(
        r"(january|february|march|april|may|june|july|august|september|"
        r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
        r"[_\s\-]*?(20\d{2})",
        low,
    )
    if m:
        return f"{int(m.group(2)):04d}-{months[m.group(1)]:02d}-15T00:00:00"
    m2 = _PDF_MONTH.search(text or "")
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-28T00:00:00"
    return None
