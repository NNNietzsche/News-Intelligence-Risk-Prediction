"""上市 / 退市状态：有效股票代码 + Delisted/Suspended/ST/*ST 判定。"""

from __future__ import annotations

import logging
import re
from typing import Optional

from intl_ratings.config import SourcesConfig
from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.models import NEED_REVIEW, NO, NOT_LISTED, YES, EntityMapping, ListingSnapshot

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,15}$")
_ST_RE = re.compile(r"(?:\*ST|\bST\b)", re.I)
_BAD_STATUS_RE = re.compile(r"DELIST(?:ED)?|SUSPEND(?:ED)?|\*ST|\bST\b|HALTED", re.I)


def looks_like_ticker(ticker: str) -> bool:
    t = (ticker or "").strip()
    if not t or not _TICKER_RE.match(t):
        return False
    if "." in t:
        return True
    return t.isalpha() and 1 <= len(t) <= 5


class ListingEngine:
    def __init__(
        self,
        sources: SourcesConfig,
        raw_store: Optional[RawResponseStore] = None,
        error_log: Optional[ErrorIssuerLogger] = None,
        not_listed_label: str = NOT_LISTED,
    ) -> None:
        self.sources = sources
        self.raw_store = raw_store
        self.error_log = error_log
        self.not_listed_label = not_listed_label

    def fetch(self, mapping: EntityMapping) -> ListingSnapshot:
        ticker = (mapping.stock_ticker or "").strip()
        if not looks_like_ticker(ticker):
            return ListingSnapshot(
                is_listed=NO,
                is_delisted_or_st=self.not_listed_label,
                ticker="",
                status_label="no_valid_ticker",
                raw_source="ticker_check",
            )

        if not self.sources.yfinance:
            return ListingSnapshot(
                is_listed=YES,
                is_delisted_or_st=NEED_REVIEW,
                ticker=ticker,
                status_label="yfinance_disabled",
                raw_source="ticker_only",
            )

        return self._from_yfinance(mapping.issuer_name, ticker)

    def _from_yfinance(self, issuer: str, ticker: str) -> ListingSnapshot:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            if self.error_log:
                self.error_log.log(issuer, "上市状态", "未安装 yfinance")
            return ListingSnapshot(
                is_listed=YES,
                is_delisted_or_st=NEED_REVIEW,
                ticker=ticker,
                status_label="yfinance_missing",
                raw_source="import_error",
            )

        try:
            t = yf.Ticker(ticker)
            info: dict = {}
            try:
                info = t.info or {}
            except Exception as exc:
                err = str(exc)
                if "Too Many Requests" in err or "429" in err:
                    return ListingSnapshot(
                        is_listed=YES,
                        is_delisted_or_st=NEED_REVIEW,
                        ticker=ticker,
                        status_label="rate_limited",
                        raw_source="yfinance",
                    )

            name = str(info.get("shortName") or info.get("longName") or "")
            quote_type = str(info.get("quoteType") or "")
            price = info.get("regularMarketPrice") or info.get("previousClose")
            status_text = " ".join(
                str(info.get(k) or "")
                for k in ("quoteType", "shortName", "longName", "marketState", "exchange")
            )

            if self.raw_store:
                self.raw_store.save(
                    issuer,
                    "yfinance_listing",
                    {
                        "ticker": ticker,
                        "info": {
                            k: info.get(k)
                            for k in (
                                "symbol",
                                "shortName",
                                "longName",
                                "quoteType",
                                "exchange",
                                "marketState",
                                "regularMarketPrice",
                                "previousClose",
                            )
                            if k in info
                        },
                    },
                )

            # 名称/状态含 Delisted / Suspended / ST / *ST → 是
            if _BAD_STATUS_RE.search(status_text) or _ST_RE.search(name):
                label = "st" if _ST_RE.search(name) else "delisted_or_suspended"
                return ListingSnapshot(
                    is_listed=YES,
                    is_delisted_or_st=YES,
                    ticker=ticker,
                    status_label=label,
                    raw_source="yfinance",
                )

            if not info or (price is None and not name):
                try:
                    hist = t.history(period="5d")
                except Exception as exc:
                    if "Too Many Requests" in str(exc) or "429" in str(exc):
                        return ListingSnapshot(
                            is_listed=YES,
                            is_delisted_or_st=NEED_REVIEW,
                            ticker=ticker,
                            status_label="rate_limited",
                            raw_source="yfinance",
                        )
                    hist = None
                if hist is None or getattr(hist, "empty", True):
                    return ListingSnapshot(
                        is_listed=YES,
                        is_delisted_or_st=NEED_REVIEW,
                        ticker=ticker,
                        status_label="no_quote_data",
                        raw_source="yfinance",
                    )

            return ListingSnapshot(
                is_listed=YES,
                is_delisted_or_st=NO,
                ticker=ticker,
                status_label="active",
                raw_source=f"yfinance:{quote_type or 'equity'}",
            )
        except Exception as exc:
            logger.warning("上市状态查询失败 [%s]: %s", issuer, exc)
            if self.error_log:
                self.error_log.log(issuer, "上市废止", str(exc))
            return ListingSnapshot(
                is_listed=YES,
                is_delisted_or_st=NEED_REVIEW,
                ticker=ticker,
                status_label="error",
                raw_source="yfinance_error",
            )
