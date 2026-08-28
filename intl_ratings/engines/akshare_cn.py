"""AkShare：中国货币网 / 巨潮资讯 国内公告与财报（库内封装，不手写 HTTP）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.models import EntityMapping

logger = logging.getLogger(__name__)


def _preview_df(df: Any, n: int = 5) -> Any:
    if df is None:
        return None
    if hasattr(df, "head"):
        try:
            return {
                "columns": list(df.columns),
                "nrows": len(df),
                "head": df.head(n).to_dict(orient="records"),
            }
        except Exception:
            return str(df)[:2000]
    return str(df)[:2000]


class AkshareCnClient:
    """调用 akshare 已封装的巨潮 / 货币网相关接口。"""

    def __init__(
        self,
        raw_store: Optional[RawResponseStore] = None,
        error_log: Optional[ErrorIssuerLogger] = None,
    ) -> None:
        self.raw_store = raw_store
        self.error_log = error_log

    def fetch_cn_disclosures(self, mapping: EntityMapping) -> dict[str, Any]:
        """巨潮资讯披露 + 公告；代码优先 inherit/stock A 股。"""
        try:
            import akshare as ak  # type: ignore
        except ImportError:
            return {"ok": False, "error": "akshare_not_installed"}

        code = self._a_share_code(mapping)
        out: dict[str, Any] = {"ok": True, "code": code, "sources": {}}

        # 巨潮资讯：个股公告
        if code:
            for fn_name, kwargs in (
                ("stock_zh_a_disclosure_report_cninfo", {"symbol": code}),
                ("stock_notice_report", {"symbol": code, "date": None}),
            ):
                fn = getattr(ak, fn_name, None)
                if not callable(fn):
                    out["sources"][fn_name] = {"skipped": "api_missing"}
                    continue
                try:
                    # stock_notice_report 部分版本要求 date
                    if fn_name == "stock_notice_report":
                        try:
                            df = fn(symbol=code)
                        except TypeError:
                            from datetime import datetime

                            df = fn(symbol=code, date=datetime.now().strftime("%Y%m%d"))
                    else:
                        df = fn(**{k: v for k, v in kwargs.items() if v is not None})
                    payload = _preview_df(df)
                    out["sources"][fn_name] = payload
                    if self.raw_store:
                        self.raw_store.save(mapping.issuer_name, f"akshare_{fn_name}", payload)
                except Exception as exc:
                    out["sources"][fn_name] = {"error": str(exc)}
                    logger.debug("%s 失败: %s", fn_name, exc)

        # 中国货币网相关：银行间/债券公开数据（截面，按发行体名称难精确匹配）
        for fn_name in (
            "bond_china_close_return_query",
            "bond_zh_us_rate",
            "macro_china_bond_public",
            "bond_cash_summary_sse",
        ):
            fn = getattr(ak, fn_name, None)
            if not callable(fn):
                out["sources"][fn_name] = {"skipped": "api_missing"}
                continue
            try:
                df = fn()
                payload = _preview_df(df, n=3)
                out["sources"][fn_name] = payload
                if self.raw_store:
                    self.raw_store.save(mapping.issuer_name, f"akshare_{fn_name}", payload)
            except Exception as exc:
                out["sources"][fn_name] = {"error": str(exc)}

        return out

    @staticmethod
    def _a_share_code(mapping: EntityMapping) -> str:
        for t in (mapping.inherit_ticker, mapping.stock_ticker):
            if not t:
                continue
            code = t.split(".")[0]
            suf = t.split(".")[-1].upper() if "." in t else ""
            if code.isdigit() and len(code) == 6 and suf in {"", "SH", "SZ", "SS"}:
                return code
        return ""
