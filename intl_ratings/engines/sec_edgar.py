"""sec-edgar-downloader：美股主体 SEC 官方财报下载（库封装）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.models import EntityMapping

logger = logging.getLogger(__name__)


class SecEdgarClient:
    def __init__(
        self,
        download_dir: Path,
        company_name: str = "RiskIntelSystem",
        email: str = "risk-intel@example.com",
        raw_store: Optional[RawResponseStore] = None,
        error_log: Optional[ErrorIssuerLogger] = None,
    ) -> None:
        self.download_dir = download_dir
        self.company_name = company_name
        self.email = email
        self.raw_store = raw_store
        self.error_log = error_log
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def download_filings(
        self,
        mapping: EntityMapping,
        *,
        forms: tuple[str, ...] = ("10-K", "10-Q"),
        limit: int = 1,
    ) -> dict[str, Any]:
        us_id = (mapping.us_ticker or mapping.cik or "").strip()
        # 纯美股代码：无交易所后缀
        if not us_id and mapping.stock_ticker and "." not in mapping.stock_ticker:
            us_id = mapping.stock_ticker.strip()
        if not us_id:
            return {"ok": False, "skipped": "no_us_ticker_or_cik"}

        try:
            from sec_edgar_downloader import Downloader
        except ImportError:
            msg = "未安装 sec-edgar-downloader"
            if self.error_log:
                self.error_log.log(mapping.issuer_name, "sec_edgar", msg)
            return {"ok": False, "error": msg}

        result: dict[str, Any] = {
            "ok": True,
            "ticker_or_cik": us_id,
            "forms": {},
            "download_dir": str(self.download_dir),
        }
        try:
            dl = Downloader(self.company_name, self.email, str(self.download_dir))
            for form in forms:
                try:
                    n = dl.get(form, us_id, limit=limit)
                    result["forms"][form] = {"downloaded": n}
                except Exception as exc:
                    result["forms"][form] = {"error": str(exc)}
                    logger.warning("SEC %s %s 失败: %s", form, us_id, exc)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            if self.error_log:
                self.error_log.log(mapping.issuer_name, "sec_edgar", str(exc))

        if self.raw_store:
            self.raw_store.save(mapping.issuer_name, "sec_edgar_downloader", result)
        return result
