"""财报抓取：yfinance.financials['Net Income'] + AkShare 指定接口。离岸 SPV 继承母公司。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from intl_ratings.config import SourcesConfig
from intl_ratings.formulas import judge_loss_from_net_income
from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.models import NEED_REVIEW, EntityMapping, FinancialSnapshot

logger = logging.getLogger(__name__)


class FinancialsEngine:
    def __init__(
        self,
        sources: SourcesConfig,
        raw_store: Optional[RawResponseStore] = None,
        error_log: Optional[ErrorIssuerLogger] = None,
    ) -> None:
        self.sources = sources
        self.raw_store = raw_store
        self.error_log = error_log

    def fetch(self, mapping: EntityMapping) -> FinancialSnapshot:
        ticker = mapping.financial_ticker
        inherited = mapping.financial_entity_name if mapping.is_offshore_spv else ""

        # A 股代码优先 AkShare 指定接口
        if ticker and self.sources.akshare and self._is_a_share(ticker):
            ak_snap = self._from_akshare_a_share(mapping.issuer_name, ticker)
            if ak_snap is not None:
                if inherited:
                    ak_snap.inherited_from = inherited
                return ak_snap

        if ticker and self.sources.yfinance:
            yf_snap = self._from_yfinance(mapping.issuer_name, ticker)
            if yf_snap is not None:
                if inherited:
                    yf_snap.inherited_from = inherited
                return yf_snap

        if self.sources.akshare and ticker:
            ak_snap = self._from_akshare_a_share(mapping.issuer_name, ticker)
            if ak_snap is not None:
                if inherited:
                    ak_snap.inherited_from = inherited
                return ak_snap

        if self.error_log:
            self.error_log.log(
                mapping.issuer_name,
                "決算亏损",
                f"无法获取归母净利润 ticker={ticker or '(空)'}",
            )
        return FinancialSnapshot(
            is_loss=NEED_REVIEW,
            inherited_from=inherited,
            raw_source="fetch_failed",
        )

    @staticmethod
    def _is_a_share(ticker: str) -> bool:
        code = ticker.split(".")[0]
        suf = ticker.split(".")[-1].upper() if "." in ticker else ""
        return code.isdigit() and len(code) == 6 and suf in {"SH", "SZ", "SS", ""}

    def _from_yfinance(self, issuer: str, ticker: str) -> Optional[FinancialSnapshot]:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            logger.info("未安装 yfinance，跳过海外财报")
            return None

        try:
            t = yf.Ticker(ticker)
            net_income: Optional[float] = None
            period = "financials"
            payload: dict[str, Any] = {"ticker": ticker}

            # 指定口径：ticker.financials.loc['Net Income']
            try:
                fin = t.financials
                if fin is not None and not getattr(fin, "empty", True):
                    if "Net Income" in fin.index:
                        series = fin.loc["Net Income"]
                        net_income = float(series.iloc[0])
                        payload["row"] = "Net Income"
                        payload["columns"] = [str(c) for c in list(series.index)[:4]]
                        payload["net_income"] = net_income
            except Exception as exc:
                payload["financials_error"] = str(exc)

            # 季度兜底
            if net_income is None:
                try:
                    q = t.quarterly_financials
                    if q is not None and not getattr(q, "empty", True) and "Net Income" in q.index:
                        series = q.loc["Net Income"]
                        net_income = float(series.iloc[0])
                        period = "quarterly_financials"
                        payload["row"] = "Net Income"
                        payload["net_income"] = net_income
                except Exception as exc:
                    payload["quarterly_error"] = str(exc)

            if self.raw_store:
                self.raw_store.save(issuer, "yfinance_net_income", payload)

            if net_income is None:
                return None

            return FinancialSnapshot(
                is_loss=judge_loss_from_net_income(net_income),
                net_income=net_income,
                period_label=period,
                raw_source=f"yfinance.financials:{ticker}",
            )
        except Exception as exc:
            logger.warning("yfinance 财报失败 [%s %s]: %s", issuer, ticker, exc)
            if self.error_log:
                self.error_log.log(issuer, "決算亏损", f"yfinance error: {exc}")
            return None

    def _from_akshare_a_share(self, issuer: str, ticker: str) -> Optional[FinancialSnapshot]:
        try:
            import akshare as ak  # type: ignore
        except ImportError:
            logger.info("未安装 akshare，跳过 A 股财报")
            return None

        code = ticker.split(".")[0]
        # 1) ak.stock_financial_abstract
        snap = self._ak_financial_abstract(ak, issuer, code)
        if snap is not None:
            return snap
        # 2) ak.stock_financial_analysis_indicator
        return self._ak_financial_indicator(ak, issuer, code)

    def _ak_financial_abstract(self, ak: Any, issuer: str, code: str) -> Optional[FinancialSnapshot]:
        fn = getattr(ak, "stock_financial_abstract", None)
        if not callable(fn):
            return None
        try:
            df = fn(symbol=code)
            preview = df.head(20).to_dict() if hasattr(df, "head") else str(df)[:1500]
            if self.raw_store:
                self.raw_store.save(issuer, "akshare_stock_financial_abstract", {"code": code, "preview": preview})

            net_income = self._extract_net_income_from_df(df)
            if net_income is None:
                return None
            return FinancialSnapshot(
                is_loss=judge_loss_from_net_income(net_income),
                net_income=net_income,
                period_label="stock_financial_abstract",
                raw_source=f"ak.stock_financial_abstract:{code}",
            )
        except Exception as exc:
            logger.warning("ak.stock_financial_abstract 失败 [%s]: %s", code, exc)
            return None

    def _ak_financial_indicator(self, ak: Any, issuer: str, code: str) -> Optional[FinancialSnapshot]:
        fn = getattr(ak, "stock_financial_analysis_indicator", None)
        if not callable(fn):
            return None
        try:
            # 部分版本需要 symbol=sh600000 形式
            try:
                df = fn(symbol=code)
            except TypeError:
                df = fn(stock=code)
            preview = df.head(5).to_dict() if hasattr(df, "head") else str(df)[:1500]
            if self.raw_store:
                self.raw_store.save(
                    issuer,
                    "akshare_stock_financial_analysis_indicator",
                    {"code": code, "preview": preview},
                )
            net_income = self._extract_net_income_from_df(df)
            if net_income is None:
                return None
            return FinancialSnapshot(
                is_loss=judge_loss_from_net_income(net_income),
                net_income=net_income,
                period_label="stock_financial_analysis_indicator",
                raw_source=f"ak.stock_financial_analysis_indicator:{code}",
            )
        except Exception as exc:
            logger.warning("ak.stock_financial_analysis_indicator 失败 [%s]: %s", code, exc)
            return None

    @staticmethod
    def _extract_net_income_from_df(df: Any) -> Optional[float]:
        if df is None or getattr(df, "empty", True):
            return None
        # 行式：指标名在某一列
        for col in getattr(df, "columns", []):
            cs = str(col)
            if any(k in cs for k in ("归母净利润", "归属母公司股东的净利润", "净利润")):
                try:
                    return float(df.iloc[0][col])
                except Exception:
                    pass
        # 指标在 index / 第一列
        name_col = None
        for c in getattr(df, "columns", []):
            if str(c) in {"指标", "选项", "指标名称", "名称"} or "指标" in str(c):
                name_col = c
                break
        if name_col is not None:
            for _, row in df.iterrows():
                label = str(row.get(name_col, ""))
                if "归母" in label and "净利" in label:
                    for c in df.columns:
                        if c == name_col:
                            continue
                        try:
                            return float(row[c])
                        except Exception:
                            continue
                if label in {"净利润", "归属于母公司股东的净利润"}:
                    for c in df.columns:
                        if c == name_col:
                            continue
                        try:
                            return float(row[c])
                        except Exception:
                            continue
        # index 为指标名
        try:
            for key in df.index:
                ks = str(key)
                if ("归母" in ks and "净利" in ks) or ks in {"Net Income", "净利润"}:
                    val = df.loc[key]
                    if hasattr(val, "iloc"):
                        return float(val.iloc[0])
                    return float(val)
        except Exception:
            pass
        return None
