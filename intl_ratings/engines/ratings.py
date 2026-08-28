"""评级抓取引擎。

优先官方 API（待补充）；未上市且仍为 NR 时，用 Playwright 抓取
新浪/东财债券相关公开页与评级机构公开发布页。
"""

from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import urlparse
from typing import Any, Optional

from intl_ratings.config import SourcesConfig
from intl_ratings.engines.listing import looks_like_ticker
from intl_ratings.engines.playwright_ratings import PlaywrightRatingsScraper, parse_ratings_from_text
from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.models import NEED_REVIEW, NO, NR, EntityMapping, RatingSnapshot

logger = logging.getLogger(__name__)

PENDING_RATING_SOURCES = (
    "moodys_api（Moody's Analytics / RatingsHub）",
    "sp_api（S&P Capital IQ / RatingsDirect）",
    "fitch_api（Fitch Connect）",
    "rating_change_feed（近 90 日评级变动历史）",
    "中债/中国货币网主体评级正式接口（函数名与鉴权待确认）",
)

_OFFICIAL_DOMAINS = {
    "moodys": ("moodys.com",),
    "sp": ("spglobal.com",),
    "fitch": ("fitchratings.com",),
}

_MOODYS_VALUE = r"(Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]|Ca|C)"
_SP_FITCH_VALUE = r"(AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB\+|BB-|BB|B\+|B-|B|CCC\+|CCC-|CCC|CC|C|D|RD)"


def _parse_issuer_rating_page(text: str) -> dict[str, str]:
    """解析发行人 IR 页的常见“机构名称 + Long-term”及表格格式。"""
    found = parse_ratings_from_text(text)
    patterns = {
        "moodys": rf"(?:Moody['’]?s(?:\s+Investors\s+Service)?){{1}}[\s\S]{{0,220}}?Long[-\s]?term\s*:\s*{_MOODYS_VALUE}",
        "sp": rf"(?:Standard\s*&?\s*Poor['’]?s|S&P)[\s\S]{{0,220}}?Long[-\s]?term\s*:\s*{_SP_FITCH_VALUE}",
        "fitch": rf"Fitch[\s\S]{{0,220}}?Long[-\s]?term\s*:\s*{_SP_FITCH_VALUE}",
    }
    for agency, pattern in patterns.items():
        if agency in found:
            continue
        hit = re.search(pattern, text, flags=re.I)
        if hit:
            found[agency] = hit.group(1)

    # ORIX 等 IR 页面使用固定表格顺序：R&I / S&P / Fitch / Moody's / JCR。
    if not all(key in found for key in ("sp", "fitch", "moodys")):
        table = re.search(
            r"S&P\s+Fitch\s+Moody['’]?s\s+JCR[\s\S]{0,600}?Long[-\s]?Term\s+Debt\s+(.{0,180})",
            text,
            flags=re.I,
        )
        if table:
            values = re.findall(
                r"(?:Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB\+|BB-|BB|B\+|B-|B)",
                table.group(1),
            )
            if len(values) >= 4:
                found.setdefault("sp", values[1])
                found.setdefault("fitch", values[2])
                found.setdefault("moodys", values[3])
    return found


def _official_agency_for_url(url: str) -> str:
    host = (urlparse(url or "").netloc or "").lower().removeprefix("www.")
    for agency, domains in _OFFICIAL_DOMAINS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return agency
    return ""


def _entity_tokens(mapping: EntityMapping) -> list[str]:
    """用于拒绝“同名但非本发行体”的检索结果。"""
    names = [mapping.issuer_name, mapping.parent_name, mapping.guarantor_name]
    out: list[str] = []
    for name in names:
        words = [word.lower() for word in (name or "").replace(",", " ").split() if len(word) >= 4]
        if words:
            out.append(" ".join(words[:3]))
    return out


class RatingsEngine:
    def __init__(
        self,
        sources: SourcesConfig,
        raw_store: Optional[RawResponseStore] = None,
        error_log: Optional[ErrorIssuerLogger] = None,
        placeholders_nr: str = NR,
        playwright_scraper: Optional[PlaywrightRatingsScraper] = None,
    ) -> None:
        self.sources = sources
        self.raw_store = raw_store
        self.error_log = error_log
        self.nr = placeholders_nr
        self.playwright_scraper = playwright_scraper
        self._public_search_count = 0

    def fetch(self, mapping: EntityMapping) -> RatingSnapshot:
        moodys = self.nr
        sp = self.nr
        fitch = self.nr
        changed = NO
        source_bits: list[str] = []
        source_urls: list[str] = []

        if not (self.sources.moodys_api or self.sources.sp_api or self.sources.fitch_api):
            source_bits.append("skipped_official_apis")
        else:
            if self.error_log:
                self.error_log.log(
                    mapping.issuer_name,
                    "ratings",
                    "sources.*_api 已开启但适配器未实现",
                )
            moodys = NEED_REVIEW
            sp = NEED_REVIEW
            fitch = NEED_REVIEW
            source_bits.append("official_apis_stub")

        if self.sources.akshare:
            self._probe_zjl_em(mapping)
            source_bits.append("ak_zjl_em_probed_not_rating")

        # 发行人 IR 页是免费模式的首选：内容由发行人直接披露，通常比搜索摘要稳定。
        issuer_page = self._fetch_issuer_official_ratings(mapping)
        for key, value in issuer_page.get("ratings", {}).items():
            if key == "moodys" and value:
                moodys = value
            elif key == "sp" and value:
                sp = value
            elif key == "fitch" and value:
                fitch = value
        source_urls.extend(issuer_page.get("source_urls") or [])
        source_bits.append(issuer_page.get("source") or "issuer_ir_no_url")

        # 免费公开模式：通过已配置的秘塔检索发现公开评级行动，仅接收三大机构官网。
        # 该路径不把媒体转载、搜索摘要或无主体匹配的资料当作正式评级。
        if self.sources.official_public_ratings:
            public = self._fetch_official_public_ratings(mapping)
            for key, value in public.get("ratings", {}).items():
                if key == "moodys" and value:
                    moodys = value
                elif key == "sp" and value:
                    sp = value
                elif key == "fitch" and value:
                    fitch = value
            source_urls.extend(public.get("source_urls") or [])
            source_bits.append(public.get("source") or "official_public_no_hit")

        # 未上市 + 三大仍为 NR → Playwright 公开页兜底
        unlisted = not looks_like_ticker(mapping.stock_ticker)
        all_nr = self._is_nr(moodys) and self._is_nr(sp) and self._is_nr(fitch)
        if (
            unlisted
            and all_nr
            and self.sources.playwright_ratings
            and self.playwright_scraper is not None
        ):
            scraped = self.playwright_scraper.scrape(mapping)
            source_bits.append("playwright_fallback")
            if scraped.get("moodys"):
                moodys = str(scraped["moodys"])
            if scraped.get("sp"):
                sp = str(scraped["sp"])
            if scraped.get("fitch"):
                fitch = str(scraped["fitch"])
            if not (scraped.get("moodys") or scraped.get("sp") or scraped.get("fitch")):
                source_bits.append("playwright_no_hit")
        elif unlisted and all_nr and not self.sources.playwright_ratings:
            source_bits.append("playwright_disabled")

        if self.sources.rating_change_feed:
            changed = NEED_REVIEW
            if self.error_log:
                self.error_log.log(
                    mapping.issuer_name,
                    "评级是否变化",
                    "rating_change_feed 已开启但适配器未实现",
                )
        else:
            changed = NO
            source_bits.append("rating_change_skipped")

        snap = RatingSnapshot(
            moodys=moodys or self.nr,
            sp=sp or self.nr,
            fitch=fitch or self.nr,
            rating_changed=changed,
            raw_source=",".join(source_bits) or "none",
            source_urls=list(dict.fromkeys(source_urls)),
        )
        if self.raw_store:
            self.raw_store.save(mapping.issuer_name, "ratings_engine", snap.model_dump())
        return snap

    def _fetch_issuer_official_ratings(self, mapping: EntityMapping) -> dict[str, Any]:
        """读取映射表指定的发行人 IR 评级页，不从第三方转载页取数。"""
        url = (mapping.official_rating_url or "").strip()
        if not url:
            return {"ratings": {}, "source_urls": [], "source": "issuer_ir_no_url"}
        try:
            import requests

            response = requests.get(
                url,
                timeout=20,
                headers={"User-Agent": "RiskIntelSystem/1.0 (public-rating-monitor)"},
            )
            response.raise_for_status()
            html = response.text
            text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", unescape(text))
            ratings = _parse_issuer_rating_page(text)
            if self.raw_store:
                self.raw_store.save(
                    mapping.issuer_name,
                    "issuer_official_ratings",
                    {"url": url, "ratings": ratings, "text_preview": text[:3000]},
                )
            return {
                "ratings": ratings,
                "source_urls": [url] if ratings else [],
                "source": "issuer_ir" if ratings else "issuer_ir_no_rating",
            }
        except Exception as exc:
            if self.error_log:
                self.error_log.log(mapping.issuer_name, "issuer_official_ratings", str(exc))
            return {"ratings": {}, "source_urls": [], "source": "issuer_ir_fetch_failed"}

    def _fetch_official_public_ratings(self, mapping: EntityMapping) -> dict[str, Any]:
        """从三大机构官网公开页面中提取带机构标签的评级；异常只降级为 NR。"""
        max_queries = max(0, int(self.sources.official_public_rating_max_queries_per_run or 0))
        if max_queries and self._public_search_count >= max_queries:
            return {"ratings": {}, "source_urls": [], "source": "official_public_query_limit"}
        self._public_search_count += 1
        try:
            from app.services.mita_search import MitaSearchClient
        except Exception as exc:
            return {"ratings": {}, "source_urls": [], "source": f"official_public_unavailable:{exc}"}

        names = [mapping.financial_entity_name, mapping.issuer_name, mapping.parent_name]
        query_name = next((name.strip() for name in names if (name or "").strip()), "")
        if not query_name:
            return {"ratings": {}, "source_urls": [], "source": "official_public_empty_name"}
        try:
            response = MitaSearchClient().search(
                f'"{query_name}" credit rating Moody\'s S&P Fitch',
                max_results=max(1, int(self.sources.official_public_rating_max_results or 8)),
            )
        except Exception as exc:
            if self.error_log:
                self.error_log.log(mapping.issuer_name, "official_public_ratings", str(exc))
            return {"ratings": {}, "source_urls": [], "source": "official_public_search_failed"}

        tokens = _entity_tokens(mapping)
        ratings: dict[str, str] = {}
        source_urls: list[str] = []
        evidence: list[dict[str, str]] = []
        for item in response.items:
            agency = _official_agency_for_url(item.url)
            if not agency:
                continue
            agency_label = {"moodys": "Moody's", "sp": "S&P", "fitch": "Fitch"}[agency]
            blob = f"{agency_label}\n{item.title}\n{item.snippet}"
            normalized = blob.lower()
            # 至少须命中发行体／母公司名称的一个连续词组，降低错配概率。
            if tokens and not any(token in normalized for token in tokens):
                continue
            parsed = parse_ratings_from_text(blob)
            value = (parsed.get(agency) or "").strip()
            if not value or value.upper() in {"NR", "N/A"}:
                continue
            if agency not in ratings:
                ratings[agency] = value
                source_urls.append(item.url)
                evidence.append({"agency": agency, "url": item.url, "title": item.title[:300]})

        if self.raw_store:
            self.raw_store.save(
                mapping.issuer_name,
                "official_public_ratings",
                {"query": query_name, "ratings": ratings, "evidence": evidence},
            )
        return {
            "ratings": ratings,
            "source_urls": source_urls,
            "source": "official_public" if ratings else "official_public_no_hit",
        }

    @staticmethod
    def _is_nr(value: str) -> bool:
        raw = (value or "").strip()
        if raw == NEED_REVIEW:
            return True
        s = raw.upper()
        return s in {"", NR, "N/A", "—", "-"}

    def _probe_zjl_em(self, mapping: EntityMapping) -> None:
        ticker = (mapping.stock_ticker or "").split(".")[0]
        if not (ticker.isdigit() and len(ticker) == 6):
            return
        try:
            import akshare as ak  # type: ignore
        except ImportError:
            return
        fn = getattr(ak, "stock_comment_detail_zjl_em", None)
        if not callable(fn):
            return
        try:
            raw = fn(symbol=ticker)
            if self.raw_store:
                self.raw_store.save(
                    mapping.issuer_name,
                    "akshare_stock_comment_detail_zjl_em",
                    {
                        "symbol": ticker,
                        "note": "该接口为资金流向，非穆迪/标普/惠誉主体评级",
                        "preview": str(raw)[:2000],
                    },
                )
        except Exception as exc:
            logger.debug("zjl_em 探测失败: %s", exc)


def ratings_pending_notice() -> str:
    return "【待补充数据源】" + "；".join(PENDING_RATING_SOURCES)
