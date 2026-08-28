"""加载 / 合并 config/issuers.json 发行体映射表。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class IssuerRecord(BaseModel):
    parent_name: str = ""
    parent_aliases: list[str] = Field(default_factory=list)
    guarantor_name: str = ""
    stock_ticker: str = ""
    inherit_ticker: str = ""
    bond_tickers: list[str] = Field(default_factory=list)
    isin: str = ""
    figi: str = ""
    tv_symbol: str = ""
    tv_exchange: str = ""
    us_ticker: str = ""
    cik: str = ""
    official_rating_url: str = ""
    is_offshore_spv: bool = False
    notes: str = ""


class IssuersStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, IssuerRecord] = {}
        self.reload()

    def reload(self) -> None:
        self._data = {}
        if not self.path.is_file():
            logger.warning("发行体映射表不存在: %s", self.path)
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("读取 issuers.json 失败: %s", exc)
            return
        issuers = raw.get("issuers") if isinstance(raw, dict) else None
        if not isinstance(issuers, dict):
            return
        for name, meta in issuers.items():
            if not isinstance(meta, dict):
                continue
            try:
                self._data[str(name)] = IssuerRecord.model_validate(meta)
            except Exception as exc:
                logger.warning("跳过无效发行体映射 %s: %s", name, exc)

    def get(self, issuer_name: str) -> Optional[IssuerRecord]:
        name = (issuer_name or "").strip()
        if name in self._data:
            return self._data[name]
        for k, v in self._data.items():
            if k.lower() == name.lower():
                return v
        return None

    def upsert(self, issuer_name: str, record: IssuerRecord, *, persist: bool = False) -> None:
        self._data[issuer_name] = record
        if persist:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 1,
            "description": "发行体映射表（自动/手工维护）",
            "issuers": {k: v.model_dump() for k, v in sorted(self._data.items())},
        }
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def all_names(self) -> list[str]:
        return list(self._data.keys())
