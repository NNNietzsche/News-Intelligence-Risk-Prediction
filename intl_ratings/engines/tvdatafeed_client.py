"""tvDatafeed：TradingView 债券/标的日线行情（库封装，不手写 WS/HTTP）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from intl_ratings.formulas import calc_month_change_pct
from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.models import EntityMapping

logger = logging.getLogger(__name__)


class TvDatafeedClient:
    def __init__(
        self,
        username: str = "",
        password: str = "",
        raw_store: Optional[RawResponseStore] = None,
        error_log: Optional[ErrorIssuerLogger] = None,
    ) -> None:
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.raw_store = raw_store
        self.error_log = error_log
        self._tv = None

    def _client(self) -> Any:
        if self._tv is not None:
            return self._tv
        try:
            from tvDatafeed import TvDatafeed  # type: ignore
        except ImportError:
            try:
                from tvdatafeed import TvDatafeed  # type: ignore
            except ImportError as exc:
                raise ImportError("未安装 tvdatafeed / tvDatafeed") from exc
        if self.username and self.password:
            self._tv = TvDatafeed(username=self.username, password=self.password)
        else:
            # 匿名模式：可用但条数/稳定性受限
            self._tv = TvDatafeed()
        return self._tv

    def month_change_pct(self, mapping: EntityMapping) -> tuple[Optional[float], dict[str, Any]]:
        """用日线近约 30 根 K 线计算月环比涨跌幅。"""
        symbol = (mapping.tv_symbol or "").strip()
        exchange = (mapping.tv_exchange or "").strip() or "TVC"
        meta: dict[str, Any] = {"symbol": symbol, "exchange": exchange, "source": "tvDatafeed"}

        if not symbol:
            # 回退：bond_tickers 第一项可能是 TV 符号（EXCHANGE:SYMBOL 或 SYMBOL）
            for bt in mapping.bond_tickers or []:
                if ":" in bt:
                    exchange, symbol = bt.split(":", 1)
                    meta["symbol"] = symbol
                    meta["exchange"] = exchange
                    break
                if bt:
                    symbol = bt
                    meta["symbol"] = symbol
                    break
        if not symbol:
            meta["status"] = "no_tv_symbol"
            return None, meta

        try:
            from tvDatafeed import Interval  # type: ignore
        except ImportError:
            try:
                from tvdatafeed import Interval  # type: ignore
            except ImportError:
                meta["error"] = "tvdatafeed_not_installed"
                return None, meta

        try:
            tv = self._client()
            df = tv.get_hist(
                symbol=symbol,
                exchange=exchange,
                interval=Interval.in_daily,
                n_bars=35,
            )
            if df is None or getattr(df, "empty", True):
                meta["status"] = "empty"
                return None, meta

            # 列名通常为 open/high/low/close/volume
            close_col = "close" if "close" in df.columns else ("Close" if "Close" in df.columns else None)
            if close_col is None:
                meta["status"] = "no_close_col"
                meta["columns"] = list(df.columns)
                return None, meta

            closes = df[close_col].dropna()
            if len(closes) < 2:
                meta["status"] = "too_short"
                return None, meta

            # TV 返回通常旧→新；取首尾近似 30 日
            window = closes.iloc[-min(len(closes), 30) :]
            price_30d_ago = float(window.iloc[0])
            price_now = float(window.iloc[-1])
            change_pct = calc_month_change_pct(price_now, price_30d_ago)
            meta.update(
                {
                    "price_30d_ago": price_30d_ago,
                    "price_now": price_now,
                    "change_pct": change_pct,
                    "n_bars": len(closes),
                }
            )
            if self.raw_store:
                self.raw_store.save(
                    mapping.issuer_name,
                    "tvdatafeed_hist",
                    {**meta, "tail": df.tail(3).to_dict()},
                )
            return change_pct, meta
        except Exception as exc:
            meta["error"] = str(exc)
            logger.warning("tvDatafeed 失败 [%s]: %s", mapping.issuer_name, exc)
            if self.error_log:
                self.error_log.log(mapping.issuer_name, "tvdatafeed", str(exc))
            return None, meta
