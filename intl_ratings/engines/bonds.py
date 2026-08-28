"""市场价格信号：债券优先，缺失时使用上市主体股票作为明确标注的代理。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from intl_ratings.config import BondPriceConfig, SourcesConfig
from intl_ratings.engines.tvdatafeed_client import TvDatafeedClient
from intl_ratings.formulas import calc_month_change_pct, judge_bond_price_drop
from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.models import NO_PUBLIC_TRADE, BondPriceSnapshot, EntityMapping

logger = logging.getLogger(__name__)


class BondPriceEngine:
    def __init__(
        self,
        sources: SourcesConfig,
        bond_cfg: BondPriceConfig,
        raw_store: Optional[RawResponseStore] = None,
        error_log: Optional[ErrorIssuerLogger] = None,
        no_public_label: str = NO_PUBLIC_TRADE,
        tv_client: Optional[TvDatafeedClient] = None,
    ) -> None:
        self.sources = sources
        self.bond_cfg = bond_cfg
        self.raw_store = raw_store
        self.error_log = error_log
        self.no_public_label = no_public_label
        self.threshold = -abs(float(bond_cfg.drop_threshold_pct))
        self.tv_client = tv_client

    def fetch(self, mapping: EntityMapping) -> BondPriceSnapshot:
        if not self.sources.bond_price_public:
            return BondPriceSnapshot(
                price_drop_flag=self.no_public_label,
                raw_source="disabled",
            )

        details: list[dict[str, Any]] = []
        best_change: Optional[float] = None
        checked: list[str] = []
        market_proxy = "债券"

        # 1) tvDatafeed（TradingView 债券行情优先）
        if self.sources.tvdatafeed and self.tv_client is not None:
            chg, meta = self.tv_client.month_change_pct(mapping)
            details.append(meta)
            if chg is not None:
                best_change = chg
                checked.append(f"TV:{meta.get('exchange')}:{meta.get('symbol')}")

        # 2) yfinance：bond_tickers
        tickers = [t for t in (mapping.bond_tickers or []) if t and ":" not in t]
        if best_change is None and tickers and self.sources.yfinance:
            for bt in tickers:
                chg, meta = self._yf_1mo_change(bt)
                details.append(meta)
                if chg is None:
                    continue
                checked.append(bt)
                if best_change is None or chg < best_change:
                    best_change = chg

        # 3) 没有可公开取得的债券行情时，使用上市主体的股票近一个月表现。
        # 这不是债券价格，故在返回字段中明确标记为“股票市场代理”。
        if best_change is None and self.sources.yfinance and mapping.financial_ticker:
            ticker = mapping.financial_ticker
            chg, meta = self._yf_1mo_change(ticker)
            meta["market_proxy"] = "equity"
            details.append(meta)
            if chg is not None:
                best_change = chg
                market_proxy = "股票市场代理"
                checked.append(ticker)

        # 4) AkShare 可转债截面（通常无法算 30 日）
        if best_change is None and self.sources.akshare:
            chg, meta = self._ak_bond_zh_cov(mapping)
            details.append(meta)
            if chg is not None:
                best_change = chg
                checked.extend(meta.get("matched") or [])

        if self.raw_store:
            self.raw_store.save(
                mapping.issuer_name,
                "bond_price",
                {"details": details, "threshold": self.threshold, "best_change_pct": best_change},
            )

        if best_change is None:
            if self.error_log and not tickers and not mapping.tv_symbol:
                self.error_log.log(
                    mapping.issuer_name,
                    "债券价格",
                    "无 tv_symbol / bond_tickers；无法计算月环比",
                )
            return BondPriceSnapshot(
                price_drop_flag=self.no_public_label,
                tickers_checked=checked,
                raw_source="no_public_data",
            )

        flag = judge_bond_price_drop(
            best_change,
            threshold_pct=self.threshold,
            no_public_label=self.no_public_label,
        )
        label = f"{market_proxy}近1月 {best_change:+.1f}%"
        if flag == "是":
            label += "（跌幅超过5%）"
        return BondPriceSnapshot(
            price_drop_flag=label,
            change_pct=best_change,
            max_drop_pct=(-best_change if best_change < 0 else 0.0),
            tickers_checked=checked,
            raw_source=f"{market_proxy}:tvdatafeed|yfinance|akshare",
        )

    def _yf_1mo_change(self, ticker: str) -> tuple[Optional[float], dict[str, Any]]:
        meta: dict[str, Any] = {"ticker": ticker, "source": "yfinance.history(period=1mo)"}
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            # yfinance 是可选依赖。没有安装时直接调用 Yahoo Finance 的公开图表端点，
            # 使已映射的上市主体仍能生成可复核的市场代理信号。
            return self._yahoo_chart_1mo_change(ticker, meta)
        try:
            hist = yf.Ticker(ticker).history(period="1mo")
            if hist is None or hist.empty or "Close" not in hist.columns:
                meta["status"] = "no_data"
                return self._yahoo_chart_1mo_change(ticker, meta)
            closes = hist["Close"].dropna()
            if len(closes) < 2:
                meta["status"] = "too_short"
                return self._yahoo_chart_1mo_change(ticker, meta)
            price_30d_ago = float(closes.iloc[0])
            price_now = float(closes.iloc[-1])
            change_pct = calc_month_change_pct(price_now, price_30d_ago)
            meta.update(
                {
                    "price_30d_ago": price_30d_ago,
                    "price_now": price_now,
                    "change_pct": change_pct,
                    "n_bars": len(closes),
                }
            )
            return change_pct, meta
        except Exception as exc:
            meta["error"] = str(exc)
            return self._yahoo_chart_1mo_change(ticker, meta)

    @staticmethod
    def _yahoo_chart_1mo_change(
        ticker: str, previous_meta: Optional[dict[str, Any]] = None
    ) -> tuple[Optional[float], dict[str, Any]]:
        """Yahoo Finance 公开图表端点兜底；只读取近一个月收盘价。"""
        meta = dict(previous_meta or {})
        meta["ticker"] = ticker
        meta["source"] = "Yahoo Finance chart (public)"
        try:
            import requests

            response = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker,
                params={"range": "1mo", "interval": "1d", "events": "history"},
                timeout=15,
                headers={"User-Agent": "RiskIntelSystem/1.0 (market-monitoring)"},
            )
            response.raise_for_status()
            result = (response.json().get("chart") or {}).get("result") or []
            if not result:
                meta["status"] = "no_data"
                return None, meta
            closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            values = [float(value) for value in closes if value is not None]
            if len(values) < 2:
                meta["status"] = "too_short"
                return None, meta
            change_pct = calc_month_change_pct(values[-1], values[0])
            meta.update(
                {
                    "price_30d_ago": values[0],
                    "price_now": values[-1],
                    "change_pct": change_pct,
                    "n_bars": len(values),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return change_pct, meta
        except Exception as exc:
            meta["error"] = str(exc)
            return None, meta

    def _ak_bond_zh_cov(self, mapping: EntityMapping) -> tuple[Optional[float], dict[str, Any]]:
        meta: dict[str, Any] = {"source": "ak.bond_zh_cov", "matched": []}
        try:
            import akshare as ak  # type: ignore
        except ImportError:
            meta["error"] = "akshare_missing"
            return None, meta
        fn = getattr(ak, "bond_zh_cov", None)
        if not callable(fn):
            meta["error"] = "bond_zh_cov_missing"
            return None, meta
        try:
            df = fn()
            if self.raw_store:
                self.raw_store.save(
                    mapping.issuer_name,
                    "akshare_bond_zh_cov_head",
                    {"columns": list(getattr(df, "columns", [])), "nrows": len(df) if df is not None else 0},
                )
            if df is None or getattr(df, "empty", True):
                meta["status"] = "empty"
                return None, meta
            meta["status"] = "section_only_no_30d_history"
            meta["note"] = "ak.bond_zh_cov 为可转债最新截面，无 30 日历史，无法按公式计算"
            return None, meta
        except Exception as exc:
            meta["error"] = str(exc)
            return None, meta
