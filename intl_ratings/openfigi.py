"""OpenFIGI 证券代码映射：https://api.openfigi.com/v3/mapping"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from intl_ratings.logging_utils import RawResponseStore

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


class OpenFigiClient:
    def __init__(
        self,
        api_key: str = "",
        timeout: int = 30,
        raw_store: Optional[RawResponseStore] = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.raw_store = raw_store

    def map_by_name(
        self,
        issuer_name: str,
        *,
        market_sec_des: str | None = None,
        exch_code: str | None = None,
    ) -> dict[str, str]:
        """按名称查询，返回 {isin, figi, ticker, exchCode, name}（可能为空）。"""
        query: dict[str, Any] = {"query": issuer_name}
        if market_sec_des:
            query["marketSecDes"] = market_sec_des
        if exch_code:
            query["exchCode"] = exch_code
        return self._post(issuer_name, [query])

    def map_by_isin(self, issuer_name: str, isin: str) -> dict[str, str]:
        return self._post(issuer_name, [{"idType": "ID_ISIN", "idValue": isin}])

    def _post(self, issuer_name: str, body: list[dict[str, Any]]) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(OPENFIGI_URL, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("OpenFIGI 请求失败 [%s]: %s", issuer_name, exc)
            return {}

        if self.raw_store:
            self.raw_store.save(issuer_name, "openfigi", data)

        try:
            block = data[0] if isinstance(data, list) and data else {}
            data_list = block.get("data") if isinstance(block, dict) else None
            if not data_list:
                return {}
            first = data_list[0]
            return {
                "isin": str(first.get("isin") or ""),
                "figi": str(first.get("figi") or ""),
                "ticker": str(first.get("ticker") or ""),
                "exchCode": str(first.get("exchCode") or ""),
                "name": str(first.get("name") or ""),
                "marketSector": str(first.get("marketSector") or ""),
                "securityType": str(first.get("securityType") or ""),
            }
        except Exception:
            return {}

    @staticmethod
    def to_yfinance_ticker(figi_hit: dict[str, str]) -> str:
        """将 OpenFIGI 结果尽量转成 yfinance 代码。"""
        ticker = (figi_hit.get("ticker") or "").strip()
        exch = (figi_hit.get("exchCode") or "").upper()
        if not ticker:
            return ""
        # 常见交易所后缀
        suffix_map = {
            "HK": ".HK",
            "HKG": ".HK",
            "T": ".T",
            "TOKYO": ".T",
            "JP": ".T",
            "SH": ".SS",
            "SS": ".SS",
            "SZ": ".SZ",
            "US": "",
            "UN": "",
            "UQ": "",
            "UW": "",
        }
        if "." in ticker:
            return ticker
        suffix = suffix_map.get(exch, "")
        # 港股补齐 5 位
        if suffix == ".HK" and ticker.isdigit():
            ticker = ticker.zfill(5)
        return f"{ticker}{suffix}"
