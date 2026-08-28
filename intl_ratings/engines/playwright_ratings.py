"""Playwright 兜底：未上市且 API 无评级时，抓取新浪/东财债券频道与评级机构公开页。"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import quote

from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.models import NR, EntityMapping

logger = logging.getLogger(__name__)

# 穆迪 / 标普 / 惠誉常见主体评级符号
# 带修饰符的写法须写在裸 AA/A 之前，避免 AA- 被截成 AA
_RATING_SP_FITCH = (
    r"(AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB\+|BB-|BB|B\+|B-|B|CCC\+|CCC-|CCC|CC|C|D|RD|NR)"
)

_END = r"(?=\s|[;；,，。:：]|\"|'|”|’|$)"

_MOODYS_RE = re.compile(
    rf"(?:Moody's|Moodys|穆迪)\s*[:：]?\s*(Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]|Ca|C|WR|NR){_END}",
    re.I,
)
_SP_RE = re.compile(
    rf"(?:S&P|Standard\s*&?\s*Poor'?s|标普|标准普尔)\s*[:：]?\s*{_RATING_SP_FITCH}{_END}",
    re.I,
)
_FITCH_RE = re.compile(
    rf"(?:Fitch|惠誉)\s*[:：]?\s*{_RATING_SP_FITCH}{_END}",
    re.I,
)

# 宽松：机构名与评级之间只允许空白/标点，不能吞字母
_SEP = r"[\s:：\-—|,，、]{0,16}"
_MOODYS_LOOSE = re.compile(
    rf"(?:穆迪|Moody's){_SEP}(Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]){_END}",
    re.I,
)
_SP_LOOSE = re.compile(rf"(?:标普|标准普尔|S&P|Standard\s*&?\s*Poor'?s){_SEP}{_RATING_SP_FITCH}{_END}", re.I)
_FITCH_LOOSE = re.compile(rf"(?:惠誉|Fitch){_SEP}{_RATING_SP_FITCH}{_END}", re.I)


def _search_queries(mapping: EntityMapping) -> list[str]:
    names = [
        mapping.issuer_name,
        mapping.parent_name,
        mapping.guarantor_name,
        *mapping.parent_aliases,
    ]
    out: list[str] = []
    for n in names:
        n = (n or "").strip()
        if n and n not in out:
            out.append(n)
    return out[:3]


def parse_ratings_from_text(text: str) -> dict[str, str]:
    """从公开页正文提取三大机构评级；未命中则为空。"""
    blob = text or ""
    found: dict[str, str] = {}

    for pattern, key in (
        (_MOODYS_RE, "moodys"),
        (_MOODYS_LOOSE, "moodys"),
        (_SP_RE, "sp"),
        (_SP_LOOSE, "sp"),
        (_FITCH_RE, "fitch"),
        (_FITCH_LOOSE, "fitch"),
    ):
        if key in found:
            continue
        m = pattern.search(blob)
        if m:
            found[key] = m.group(1).strip()
    return found


class PlaywrightRatingsScraper:
    """复杂未上市主体评级兜底抓取。"""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 45000,
        raw_store: Optional[RawResponseStore] = None,
        error_log: Optional[ErrorIssuerLogger] = None,
        enable_agency_pages: bool = True,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.raw_store = raw_store
        self.error_log = error_log
        self.enable_agency_pages = enable_agency_pages

    def scrape(self, mapping: EntityMapping) -> dict[str, Any]:
        """
        返回:
          {
            moodys, sp, fitch,  # 可能为空字符串
            sources: [...],
            ok: bool,
          }
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            msg = "未安装 playwright，请执行: pip install playwright && playwright install chromium"
            if self.error_log:
                self.error_log.log(mapping.issuer_name, "playwright_ratings", msg)
            return {"ok": False, "error": msg, "moodys": "", "sp": "", "fitch": "", "sources": []}

        queries = _search_queries(mapping)
        if not queries:
            return {"ok": False, "error": "empty_query", "moodys": "", "sp": "", "fitch": "", "sources": []}

        merged: dict[str, str] = {}
        sources: list[dict[str, Any]] = []

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    locale="zh-CN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)

                for q in queries:
                    for name, builder in (
                        ("sina_bond", self._url_sina),
                        ("eastmoney_bond", self._url_eastmoney),
                    ):
                        url = builder(q)
                        hit = self._fetch_and_parse(page, mapping.issuer_name, name, url, q)
                        sources.append(hit)
                        for k, v in (hit.get("ratings") or {}).items():
                            if v and k not in merged:
                                merged[k] = v

                    if self.enable_agency_pages and len(merged) < 3:
                        for name, builder in (
                            ("moodys_public", self._url_moodys),
                            ("sp_public", self._url_sp),
                            ("fitch_public", self._url_fitch),
                        ):
                            url = builder(q)
                            hit = self._fetch_and_parse(page, mapping.issuer_name, name, url, q)
                            sources.append(hit)
                            for k, v in (hit.get("ratings") or {}).items():
                                if v and k not in merged:
                                    merged[k] = v

                    if len(merged) >= 3:
                        break

                context.close()
                browser.close()
        except Exception as exc:
            logger.warning("Playwright 评级抓取失败 [%s]: %s", mapping.issuer_name, exc)
            if self.error_log:
                self.error_log.log(mapping.issuer_name, "playwright_ratings", str(exc))
            return {
                "ok": False,
                "error": str(exc),
                "moodys": merged.get("moodys", ""),
                "sp": merged.get("sp", ""),
                "fitch": merged.get("fitch", ""),
                "sources": sources,
            }

        result = {
            "ok": True,
            "moodys": merged.get("moodys", ""),
            "sp": merged.get("sp", ""),
            "fitch": merged.get("fitch", ""),
            "sources": sources,
            "queries": queries,
        }
        if self.raw_store:
            self.raw_store.save(mapping.issuer_name, "playwright_ratings", result)
        if not merged and self.error_log:
            self.error_log.log(
                mapping.issuer_name,
                "playwright_ratings",
                "公开页未解析到穆迪/标普/惠誉评级符号",
            )
        return result

    def _fetch_and_parse(
        self,
        page: Any,
        issuer: str,
        source_name: str,
        url: str,
        query: str,
    ) -> dict[str, Any]:
        hit: dict[str, Any] = {"source": source_name, "url": url, "query": query}
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            # 尽量取正文；失败则用全页文本
            text = ""
            for sel in ("article", ".main", ".content", "#article", "body"):
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        text = loc.inner_text(timeout=3000)
                        if text and len(text) > 80:
                            break
                except Exception:
                    continue
            if not text:
                text = page.inner_text("body")
            text = (text or "")[:80000]
            ratings = parse_ratings_from_text(text)
            hit["ratings"] = ratings
            hit["text_preview"] = text[:1500]
            if self.raw_store:
                self.raw_store.save(
                    issuer,
                    f"playwright_{source_name}",
                    {"url": url, "query": query, "ratings": ratings, "text_preview": text[:3000]},
                )
        except Exception as exc:
            hit["error"] = str(exc)
            logger.debug("抓取失败 %s %s: %s", source_name, url, exc)
        return hit

    @staticmethod
    def _url_sina(query: str) -> str:
        # 新浪财经搜索（债券/资讯）
        return (
            "https://search.sina.com.cn/?q="
            + quote(query)
            + "&range=all&c=news&sort=time"
        )

    @staticmethod
    def _url_eastmoney(query: str) -> str:
        # 东方财富资讯搜索（债券相关公开报道）
        return "https://so.eastmoney.com/news/s?keyword=" + quote(query)

    @staticmethod
    def _url_moodys(query: str) -> str:
        return "https://www.moodys.com/search?searchfrom=Search&searchString=" + quote(query)

    @staticmethod
    def _url_sp(query: str) -> str:
        return (
            "https://www.spglobal.com/ratings/en/search/results?"
            "q=" + quote(query) + "&tab=ratings"
        )

    @staticmethod
    def _url_fitch(query: str) -> str:
        return "https://www.fitchratings.com/search?query=" + quote(query)
