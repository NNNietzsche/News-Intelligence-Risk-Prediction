"""实体映射层：issuers.json + OpenFIGI + LLM(openai SDK) + 缓存。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from intl_ratings.config import EntityMapperConfig, IntlRatingsEnv, get_env
from intl_ratings.issuers_store import IssuerRecord, IssuersStore
from intl_ratings.llm_client import LlmClient
from intl_ratings.logging_utils import RawResponseStore
from intl_ratings.models import EntityMapping
from intl_ratings.openfigi import OpenFigiClient

logger = logging.getLogger(__name__)

MAPPER_SYSTEM_PROMPT = """你是信用分析助手，负责将债券发行体（常为离岸 SPV）映射到母公司/担保人及公开市场代码。
只输出合法 JSON 对象，字段如下：
- parent_name: 母公司或实际信用主体中文或官方常用名（字符串）
- parent_aliases: 别名数组
- stock_ticker: 有效股票代码，如 6902.T / 00883.HK / 600938.SH；无则空字符串
- bond_tickers: 关联债券公开交易代码数组（未知则 []）
- isin: ISIN（未知则空字符串）
- figi: FIGI（未知则空字符串）
- is_offshore_spv: 是否离岸融资 SPV（布尔）
- guarantor_name: 担保人（无则空字符串）
- notes: 简短说明
不要编造不确定的股票代码或 ISIN；不确定时置空。不要输出 markdown。"""


class EntityMapper:
    def __init__(
        self,
        mapper_cfg: EntityMapperConfig,
        cache_path: Path,
        issuers_store: Optional[IssuersStore] = None,
        openfigi: Optional[OpenFigiClient] = None,
        llm: Optional[LlmClient] = None,
        raw_store: Optional[RawResponseStore] = None,
        env: Optional[IntlRatingsEnv] = None,
        timeout: int = 60,
    ) -> None:
        self.cfg = mapper_cfg
        self.cache_path = cache_path
        self.issuers_store = issuers_store
        self.openfigi = openfigi
        self.llm = llm or LlmClient(env=env or get_env(), raw_store=raw_store, timeout=timeout, temperature=mapper_cfg.temperature)
        self.raw_store = raw_store
        self.env = env or get_env()
        self.timeout = timeout
        self._cache = self._load_cache()

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_path.is_file():
            return {}
        try:
            with self.cache_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)

    def map(self, issuer_name: str) -> EntityMapping:
        name = (issuer_name or "").strip()

        # 1) config/issuers.json
        if self.issuers_store is not None:
            rec = self.issuers_store.get(name)
            if rec is not None:
                mapping = self._from_record(name, rec, source="issuers.json")
                return self._enrich_openfigi(mapping)

        # 2) yaml static_map（兼容）
        static = self.cfg.static_map.get(name)
        if static is None:
            for k, v in self.cfg.static_map.items():
                if k.lower() == name.lower():
                    static = v
                    break
        if static is not None:
            mapping = EntityMapping(
                issuer_name=name,
                parent_name=static.parent_name,
                parent_aliases=list(static.parent_aliases),
                stock_ticker=static.stock_ticker,
                bond_tickers=list(static.bond_tickers),
                is_offshore_spv=static.is_offshore_spv,
                guarantor_name=static.parent_name if static.is_offshore_spv else "",
                mapping_source="static_yaml",
            )
            return self._enrich_openfigi(mapping)

        # 3) 本地缓存
        cached = self._cache.get(name)
        if isinstance(cached, dict):
            try:
                m = EntityMapping.model_validate({"issuer_name": name, **cached})
                m.mapping_source = "cache"
                return self._enrich_openfigi(m)
            except Exception:
                pass

        # 4) LLM
        if self.cfg.use_llm and self.llm.available:
            mapped = self._map_via_llm(name)
            if mapped is not None:
                mapped = self._enrich_openfigi(mapped)
                self._cache[name] = mapped.model_dump(exclude={"issuer_name", "mapping_source"})
                self._save_cache()
                return mapped

        # 5) 兜底 + OpenFIGI 名称查询
        mapping = EntityMapping(
            issuer_name=name,
            is_offshore_spv=bool(
                re.search(r"cayman|funding|finance\s*co|mtn|limited$", name, re.I)
            ),
            mapping_source="fallback",
            notes="未命中 issuers.json / 静态映射且 LLM 不可用",
        )
        return self._enrich_openfigi(mapping)

    def _from_record(self, name: str, rec: IssuerRecord, *, source: str) -> EntityMapping:
        return EntityMapping(
            issuer_name=name,
            parent_name=rec.parent_name,
            parent_aliases=list(rec.parent_aliases),
            stock_ticker=rec.stock_ticker,
            inherit_ticker=rec.inherit_ticker,
            bond_tickers=list(rec.bond_tickers),
            isin=rec.isin,
            figi=rec.figi,
            tv_symbol=rec.tv_symbol,
            tv_exchange=rec.tv_exchange,
            us_ticker=rec.us_ticker,
            cik=rec.cik,
            official_rating_url=rec.official_rating_url,
            is_offshore_spv=rec.is_offshore_spv,
            guarantor_name=rec.guarantor_name
            or (rec.parent_name if rec.is_offshore_spv else ""),
            mapping_source=source,
            notes=rec.notes,
        )

    def _enrich_openfigi(self, mapping: EntityMapping) -> EntityMapping:
        if self.openfigi is None:
            return mapping
        # 已有股票代码则不必强查；缺 ISIN/代码时补充
        if mapping.stock_ticker and mapping.isin:
            return mapping
        hit: dict[str, str] = {}
        if mapping.isin:
            hit = self.openfigi.map_by_isin(mapping.issuer_name, mapping.isin)
        if not hit:
            hit = self.openfigi.map_by_name(mapping.issuer_name)
        if not hit:
            return mapping
        if not mapping.isin and hit.get("isin"):
            mapping.isin = hit["isin"]
        if not mapping.figi and hit.get("figi"):
            mapping.figi = hit["figi"]
        if not mapping.stock_ticker:
            yf_ticker = OpenFigiClient.to_yfinance_ticker(hit)
            if yf_ticker:
                mapping.stock_ticker = yf_ticker
                mapping.notes = (mapping.notes + "; OpenFIGI补代码").strip("; ")
        return mapping

    def _map_via_llm(self, issuer_name: str) -> Optional[EntityMapping]:
        parsed = self.llm.chat_json(
            MAPPER_SYSTEM_PROMPT,
            f"发行体名称: {issuer_name}\n请完成实体映射。",
            issuer=issuer_name,
            source="entity_mapper_llm",
        )
        if not parsed:
            return None
        return EntityMapping(
            issuer_name=issuer_name,
            parent_name=str(parsed.get("parent_name") or ""),
            parent_aliases=list(parsed.get("parent_aliases") or []),
            stock_ticker=str(parsed.get("stock_ticker") or "").strip(),
            inherit_ticker=str(parsed.get("inherit_ticker") or "").strip(),
            bond_tickers=[str(x) for x in (parsed.get("bond_tickers") or []) if x],
            isin=str(parsed.get("isin") or "").strip(),
            figi=str(parsed.get("figi") or "").strip(),
            is_offshore_spv=bool(parsed.get("is_offshore_spv")),
            guarantor_name=str(parsed.get("guarantor_name") or ""),
            mapping_source="llm",
            notes=str(parsed.get("notes") or ""),
        )
