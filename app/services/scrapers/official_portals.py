"""官方披露源参考链接（TDnet / EDINET）— 仅作人工核对入口，不是新闻正文。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import quote

import httpx

from app.services.http_client import get_http_client

logger = logging.getLogger(__name__)


@dataclass
class ScrapeHit:
    source: str
    title: str
    url: str
    snippet: str
    # True = 检索入口占位，禁止当作新闻入库
    reference_only: bool = True


class OfficialPortalScraper:
    """
    生成权威披露门户的人工核对链接。
    当前不抓取公告正文，因此产物不得发布到风险日报资讯流。
    """

    TIMEOUT = 30

    def tdnet_search_url(self, company_keyword: str, day: date) -> str:
        q = quote(f"{company_keyword} {day.isoformat()}")
        return f"https://www.release.tdnet.info/inbs/I_main_00.html?query={q}"

    def edinet_search_hint(self, company_name: str) -> str:
        return (
            "https://disclosure.edinet-fsa.go.jp/E01EW/BLMainController.jsp"
            f"?cmd=WJEWZZ0101&company={quote(company_name)}"
        )

    def fetch_page_title(self, url: str) -> Optional[str]:
        try:
            client = get_http_client()
            resp = client.get(
                url,
                headers={"User-Agent": "RiskIntelBot/1.0"},
                timeout=self.TIMEOUT,
            )
            if resp.status_code >= 400:
                return None
            text = resp.text
            lower = text.lower()
            start = lower.find("<title>")
            end = lower.find("</title>")
            if start >= 0 and end > start:
                return text[start + 7 : end].strip()
        except httpx.HTTPError as exc:
            logger.warning("抓取失败 %s: %s", url, exc)
        return None

    def collect_reference_hits(self, company: str, day: date) -> list[ScrapeHit]:
        tdnet = self.tdnet_search_url(company, day)
        edinet = self.edinet_search_hint(company)
        return [
            ScrapeHit(
                source="TDnet",
                title=f"{company} 披露检索",
                url=tdnet,
                snippet=f"目标日期 {day.isoformat()}，请在 TDnet 核实即时公告。",
                reference_only=True,
            ),
            ScrapeHit(
                source="EDINET",
                title=f"{company} 法定披露",
                url=edinet,
                snippet="金融商品交易法披露文档检索入口。",
                reference_only=True,
            ),
        ]
