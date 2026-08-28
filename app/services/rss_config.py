"""外置 RSS 配置加载（YAML，失败时回退内置默认）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _ROOT / "config" / "rss_feeds.yaml"


@dataclass
class RssQuerySpec:
    label: str
    query: str
    priority: int = 10
    enabled: bool = True
    max_items: Optional[int] = None
    google_hl: Optional[str] = None
    google_gl: Optional[str] = None
    google_ceid: Optional[str] = None
    entity_key: Optional[str] = None
    relation: str = "unscoped"
    source_type: str = "media"
    source_url: Optional[str] = None


@dataclass
class RssFeedSpec:
    label: str
    url: str
    modules: tuple[str, ...]
    feed_type: str = "direct"  # direct | google
    priority: int = 10
    enabled: bool = True
    max_items: Optional[int] = None
    google_hl: Optional[str] = None
    google_gl: Optional[str] = None
    google_ceid: Optional[str] = None
    entity_key: Optional[str] = None
    relation: str = "unscoped"
    source_type: str = "media"


@dataclass
class ModuleRssLocale:
    google_hl: Optional[str] = None
    google_gl: Optional[str] = None
    google_ceid: Optional[str] = None


@dataclass
class MarketQuoteSpec:
    label: str
    url: str
    group: str = "index"

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "url": self.url, "group": self.group}


@dataclass
class RssConfig:
    queries: dict[str, list[RssQuerySpec]] = field(default_factory=dict)
    feeds: list[RssFeedSpec] = field(default_factory=list)
    max_items_per_feed: int = 12
    google_hl: str = "zh-CN"
    google_gl: str = "CN"
    google_ceid: str = "CN:zh-Hans"
    module_defaults: dict[str, ModuleRssLocale] = field(default_factory=dict)
    market_quotes: list[MarketQuoteSpec] = field(default_factory=list)

    def resolve_google_locale(
        self,
        module_code: str,
        *,
        hl: Optional[str] = None,
        gl: Optional[str] = None,
        ceid: Optional[str] = None,
    ) -> tuple[str, str, str]:
        """优先级：条目覆盖 > 模块默认 > 全局默认。"""
        mod = self.module_defaults.get(str(module_code).upper())
        out_hl = hl or (mod.google_hl if mod else None) or self.google_hl
        out_gl = gl or (mod.google_gl if mod else None) or self.google_gl
        out_ceid = ceid or (mod.google_ceid if mod else None) or self.google_ceid
        return out_hl, out_gl, out_ceid


def _parse_config(data: dict[str, Any]) -> RssConfig:
    defaults = data.get("defaults") or {}
    cfg = RssConfig(
        max_items_per_feed=int(defaults.get("max_items_per_feed") or 12),
        google_hl=str(defaults.get("google_hl") or "zh-CN"),
        google_gl=str(defaults.get("google_gl") or "CN"),
        google_ceid=str(defaults.get("google_ceid") or "CN:zh-Hans"),
    )
    for code, row in (data.get("module_defaults") or {}).items():
        if not isinstance(row, dict):
            continue
        cfg.module_defaults[str(code).upper()] = ModuleRssLocale(
            google_hl=str(row["google_hl"]) if row.get("google_hl") else None,
            google_gl=str(row["google_gl"]) if row.get("google_gl") else None,
            google_ceid=str(row["google_ceid"]) if row.get("google_ceid") else None,
        )

    for code, rows in (data.get("queries") or {}).items():
        specs: list[RssQuerySpec] = []
        for row in rows or []:
            if not row.get("query"):
                continue
            specs.append(
                RssQuerySpec(
                    label=str(row.get("label") or row["query"][:40]),
                    query=str(row["query"]),
                    priority=int(row.get("priority") or 10),
                    enabled=bool(row.get("enabled", True)),
                    max_items=row.get("max_items"),
                    google_hl=str(row["google_hl"]) if row.get("google_hl") else None,
                    google_gl=str(row["google_gl"]) if row.get("google_gl") else None,
                    google_ceid=str(row["google_ceid"]) if row.get("google_ceid") else None,
                    entity_key=str(row["entity_key"]).strip() if row.get("entity_key") else None,
                    relation=str(row.get("relation") or "unscoped").lower(),
                    source_type=str(row.get("source_type") or "media").lower(),
                    source_url=str(row["source_url"]).strip() if row.get("source_url") else None,
                )
            )
        cfg.queries[str(code).upper()] = specs

    for row in data.get("feeds") or []:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        modules = tuple(str(m).upper() for m in (row.get("modules") or []))
        cfg.feeds.append(
            RssFeedSpec(
                label=str(row.get("label") or url),
                url=url,
                modules=modules,
                feed_type=str(row.get("type") or "direct").lower(),
                priority=int(row.get("priority") or 10),
                enabled=bool(row.get("enabled", True)),
                max_items=row.get("max_items"),
                google_hl=str(row["google_hl"]) if row.get("google_hl") else None,
                google_gl=str(row["google_gl"]) if row.get("google_gl") else None,
                google_ceid=str(row["google_ceid"]) if row.get("google_ceid") else None,
                entity_key=str(row["entity_key"]).strip() if row.get("entity_key") else None,
                relation=str(row.get("relation") or "unscoped").lower(),
                source_type=str(row.get("source_type") or "media").lower(),
            )
        )
    for row in data.get("market_quotes") or []:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        cfg.market_quotes.append(
            MarketQuoteSpec(
                label=str(row.get("label") or url),
                url=url,
                group=str(row.get("group") or "index"),
            )
        )
    return cfg


@lru_cache(maxsize=4)
def load_rss_config(path: str | None = None) -> RssConfig:
    target = Path(path) if path else _DEFAULT_PATH
    if not target.is_file():
        logger.warning("RSS 配置文件不存在: %s，使用空配置", target)
        return RssConfig()
    try:
        import yaml
    except ImportError:
        logger.warning("未安装 PyYAML，RSS 外置配置不可用")
        return RssConfig()
    try:
        with target.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = _parse_config(data if isinstance(data, dict) else {})
        logger.info(
            "已加载 RSS 配置: %d 组查询, %d 个直连源 (%s)",
            sum(len(v) for v in cfg.queries.values()),
            len(cfg.feeds),
            target,
        )
        return cfg
    except Exception as exc:
        logger.warning("RSS 配置解析失败 %s: %s", target, exc)
        return RssConfig()


def reload_rss_config(path: str | None = None) -> RssConfig:
    load_rss_config.cache_clear()
    return load_rss_config(path)
