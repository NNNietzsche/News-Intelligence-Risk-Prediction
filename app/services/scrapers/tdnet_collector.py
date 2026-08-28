"""TDnet 适时应披露采集：やのしん列表 API 优先，官方日列表页回退。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import unquote

import httpx

from app.services.http_client import get_http_client
from app.services.http_retry import with_retries

logger = logging.getLogger(__name__)

_UA = "RiskIntelBot/1.3 (+local; TDnet disclosure collector)"

# 监控企业：日文名 → 证券代码（东证 4 位）
MODULE_C_TDNET_CODES: dict[str, str] = {
    "三菱商事": "8058",
    "三井物産": "8031",
    "伊藤忠商事": "8001",
    "住友商事": "8053",
    "丸紅": "8002",
    "デンソー": "6902",
    "日本郵船": "9101",
    "大和証券": "8601",
}

_YANOSHIN_BASE = "https://webapi.yanoshin.jp/webapi/tdnet/list"


@dataclass
class TdnetHit:
    title: str
    url: str
    company_name: str
    company_code: str
    published_at: Optional[str]
    snippet: str
    source: str = "TDnet"


def normalize_stock_code(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 4:
        return digits[:4]
    return digits


def unwrap_document_url(url: str) -> str:
    """解开やのしん转发，尽量落到 release.tdnet.info 原文链接。"""
    if not url:
        return ""
    u = url.strip()
    if "rd.php?" in u:
        # .../rd.php?https://www.release.tdnet.info/...
        part = u.split("rd.php?", 1)[1]
        part = unquote(part)
        if part.startswith("http"):
            return part
    return u


class TdnetCollector:
    """拉取监控企业近 N 小时的适时应披露（标题 + PDF 链接 + 元数据摘要）。"""

    TIMEOUT = 30

    def __init__(
        self,
        *,
        company_codes: Optional[dict[str, str]] = None,
        retry_attempts: int = 3,
        retry_backoff: float = 1.5,
    ) -> None:
        self.company_codes = company_codes or dict(MODULE_C_TDNET_CODES)
        self.code_to_name = {v: k for k, v in self.company_codes.items()}
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff

    def collect(self, *, hours: int = 24, max_items: int = 40) -> list[TdnetHit]:
        codes = sorted(set(self.company_codes.values()))
        hits: list[TdnetHit] = []
        try:
            hits = self._fetch_yanoshin(codes, limit=max(max_items, 50))
            logger.info("TDnet やのしん取得 %d 条（过滤前）", len(hits))
        except Exception as exc:
            logger.warning("TDnet やのしん失败，回退官方日列表: %s", exc)
            hits = self._fetch_official_daily(codes)

        cutoff = datetime.utcnow() - timedelta(hours=max(1, hours))
        filtered: list[TdnetHit] = []
        seen: set[str] = set()
        for hit in hits:
            code = normalize_stock_code(hit.company_code)
            if code not in self.code_to_name and hit.company_name not in self.company_codes:
                # 再按名称模糊：官方表会社名可能带「株式会社」
                if not self._name_match(hit.company_name):
                    continue
            key = hit.url or f"{code}|{hit.title}|{hit.published_at}"
            if key in seen:
                continue
            if hit.published_at:
                try:
                    pub = datetime.fromisoformat(hit.published_at.replace(" ", "T"))
                    if pub < cutoff:
                        continue
                except ValueError:
                    pass
            seen.add(key)
            # 规范公司展示名
            if code in self.code_to_name:
                hit.company_name = self.code_to_name[code]
                hit.company_code = code
            filtered.append(hit)
            if len(filtered) >= max_items:
                break
        return filtered

    def _name_match(self, company_name: str) -> bool:
        name = (company_name or "").replace("株式会社", "").strip()
        for jp in self.company_codes:
            if jp in name or name in jp:
                return True
        return False

    def _fetch_yanoshin(self, codes: list[str], *, limit: int) -> list[TdnetHit]:
        key = "-".join(codes)
        url = f"{_YANOSHIN_BASE}/{key}.json?limit={int(limit)}"

        def _get() -> dict:
            client = get_http_client()
            resp = client.get(url, headers={"User-Agent": _UA}, timeout=self.TIMEOUT)
            resp.raise_for_status()
            return resp.json()

        data = with_retries(
            _get,
            attempts=self.retry_attempts,
            backoff_seconds=self.retry_backoff,
            label="tdnet-yanoshin",
        )
        items = data.get("items") or []
        hits: list[TdnetHit] = []
        for row in items:
            node = row.get("Tdnet") or row.get("TDnet") or row
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            doc = unwrap_document_url(str(node.get("document_url") or ""))
            if not title or not doc:
                continue
            code = normalize_stock_code(str(node.get("company_code") or ""))
            company = str(node.get("company_name") or "").strip()
            pub = str(node.get("pubdate") or "").strip() or None
            snippet = (
                f"【TDnet適時開示】{company}（{code}）{title}"
                + (f"。公表日時：{pub}" if pub else "")
            )
            hits.append(
                TdnetHit(
                    title=title,
                    url=doc,
                    company_name=company,
                    company_code=code,
                    published_at=pub,
                    snippet=snippet,
                )
            )
        return hits

    def _fetch_official_daily(self, codes: list[str]) -> list[TdnetHit]:
        """官方 I_list_001_YYYYMMDD.html 今日+昨日列表回退。"""
        wanted = set(codes)
        hits: list[TdnetHit] = []
        days = [date.today(), date.today() - timedelta(days=1)]
        for day in days:
            day_s = day.strftime("%Y%m%d")
            list_url = f"https://www.release.tdnet.info/inbs/I_list_001_{day_s}.html"
            try:
                html = self._get_text(list_url)
            except Exception as exc:
                logger.debug("TDnet 官方列表失败 %s: %s", list_url, exc)
                continue
            hits.extend(self._parse_official_list(html, day, wanted))
        return hits

    def _get_text(self, url: str) -> str:
        def _get() -> str:
            client = get_http_client()
            resp = client.get(url, headers={"User-Agent": _UA}, timeout=self.TIMEOUT)
            if resp.status_code == 404:
                return ""
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            return resp.text

        return with_retries(
            _get,
            attempts=self.retry_attempts,
            backoff_seconds=self.retry_backoff,
            label="tdnet-official",
        )

    def _parse_official_list(
        self, html: str, day: date, wanted_codes: set[str]
    ) -> list[TdnetHit]:
        if not html:
            return []
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except ImportError:
            # 无 bs4 时用简易正则
            return self._parse_official_regex(html, day, wanted_codes)

        soup = BeautifulSoup(html, "html.parser")
        hits: list[TdnetHit] = []
        for row in soup.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) < 4:
                continue
            time_s = cols[0].get_text(strip=True)
            code = normalize_stock_code(cols[1].get_text(strip=True))
            company = cols[2].get_text(strip=True)
            title_cell = cols[3]
            title = title_cell.get_text(strip=True)
            href = ""
            a = title_cell.find("a", href=True)
            if a:
                href = a["href"]
                if href.startswith("/"):
                    href = "https://www.release.tdnet.info" + href
                elif not href.startswith("http"):
                    href = "https://www.release.tdnet.info/inbs/" + href.lstrip("./")
            if code not in wanted_codes or not title or not href:
                continue
            pub = f"{day.isoformat()} {time_s}:00" if re.match(r"\d{1,2}:\d{2}", time_s) else day.isoformat()
            hits.append(
                TdnetHit(
                    title=title,
                    url=href,
                    company_name=company,
                    company_code=code,
                    published_at=pub,
                    snippet=f"【TDnet適時開示】{company}（{code}）{title}。公表日時：{pub}",
                )
            )
        return hits

    def _parse_official_regex(
        self, html: str, day: date, wanted_codes: set[str]
    ) -> list[TdnetHit]:
        """无 BeautifulSoup 时的宽松回退解析。"""
        hits: list[TdnetHit] = []
        # 粗匹配：代码、PDF、标题
        pattern = re.compile(
            r"(\d{4,5})\s*</td>\s*<td[^>]*>\s*([^<]+)\s*</td>\s*<td[^>]*>\s*"
            r'<a[^>]+href="([^"]+\.pdf)"[^>]*>([^<]+)</a>',
            re.I,
        )
        for m in pattern.finditer(html):
            code = normalize_stock_code(m.group(1))
            if code not in wanted_codes:
                continue
            company = m.group(2).strip()
            href = m.group(3).strip()
            title = m.group(4).strip()
            if not href.startswith("http"):
                href = "https://www.release.tdnet.info/inbs/" + href.lstrip("./")
            pub = day.isoformat()
            hits.append(
                TdnetHit(
                    title=title,
                    url=href,
                    company_name=company,
                    company_code=code,
                    published_at=pub,
                    snippet=f"【TDnet適時開示】{company}（{code}）{title}",
                )
            )
        return hits
