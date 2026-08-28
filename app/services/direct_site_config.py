"""外置直连网站（无 RSS）配置加载。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _ROOT / "config" / "direct_sites.yaml"


@dataclass
class DirectSiteSpec:
    label: str
    list_url: str
    modules: tuple[str, ...]
    item_selector: str
    title_selector: str
    link_selector: str
    site_type: str = "html_list"
    priority: int = 10
    enabled: bool = True
    max_items: Optional[int] = None
    date_selector: Optional[str] = None
    date_attr: Optional[str] = None
    snippet_selector: Optional[str] = None
    link_attr: str = "href"
    base_url: Optional[str] = None
    source_domain: Optional[str] = None
    encoding: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class DirectSitesConfig:
    sites: list[DirectSiteSpec] = field(default_factory=list)
    max_items_per_site: int = 20
    timeout_seconds: int = 30

    def sites_for_module(self, module_code: str) -> list[DirectSiteSpec]:
        code = str(module_code).upper()
        rows = [
            s
            for s in self.sites
            if s.enabled and code in s.modules and s.list_url and s.item_selector
        ]
        return sorted(rows, key=lambda s: (-s.priority, s.label))


def _parse_config(data: dict[str, Any]) -> DirectSitesConfig:
    defaults = data.get("defaults") or {}
    cfg = DirectSitesConfig(
        max_items_per_site=int(defaults.get("max_items_per_site") or 20),
        timeout_seconds=int(defaults.get("timeout_seconds") or 30),
    )
    for row in data.get("sites") or []:
        if not isinstance(row, dict):
            continue
        list_url = str(row.get("list_url") or "").strip()
        item_selector = str(row.get("item_selector") or "").strip()
        title_selector = str(row.get("title_selector") or "").strip()
        if not list_url or not item_selector or not title_selector:
            continue
        link_selector = str(row.get("link_selector") or title_selector).strip()
        modules = tuple(str(m).upper() for m in (row.get("modules") or []))
        headers_raw = row.get("headers") or {}
        headers = {
            str(k): str(v)
            for k, v in headers_raw.items()
            if k is not None and v is not None
        }
        cfg.sites.append(
            DirectSiteSpec(
                label=str(row.get("label") or list_url),
                list_url=list_url,
                modules=modules,
                item_selector=item_selector,
                title_selector=title_selector,
                link_selector=link_selector,
                site_type=str(row.get("type") or "html_list").lower(),
                priority=int(row.get("priority") or 10),
                enabled=bool(row.get("enabled", True)),
                max_items=row.get("max_items"),
                date_selector=str(row["date_selector"]).strip()
                if row.get("date_selector")
                else None,
                date_attr=str(row["date_attr"]).strip() if row.get("date_attr") else None,
                snippet_selector=str(row["snippet_selector"]).strip()
                if row.get("snippet_selector")
                else None,
                link_attr=str(row.get("link_attr") or "href"),
                base_url=str(row["base_url"]).strip() if row.get("base_url") else None,
                source_domain=str(row["source_domain"]).strip()
                if row.get("source_domain")
                else None,
                encoding=str(row["encoding"]).strip() if row.get("encoding") else None,
                headers=headers,
            )
        )
    return cfg


@lru_cache(maxsize=4)
def load_direct_sites_config(path: str | None = None) -> DirectSitesConfig:
    target = Path(path) if path else _DEFAULT_PATH
    if not target.is_file():
        logger.warning("直连站点配置不存在: %s，使用空配置", target)
        return DirectSitesConfig()
    try:
        import yaml
    except ImportError:
        logger.warning("未安装 PyYAML，直连站点配置不可用")
        return DirectSitesConfig()
    try:
        with target.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = _parse_config(data if isinstance(data, dict) else {})
        enabled = sum(1 for s in cfg.sites if s.enabled)
        logger.info(
            "已加载直连站点配置: %d 个站点（启用 %d）(%s)",
            len(cfg.sites),
            enabled,
            target,
        )
        return cfg
    except Exception as exc:
        logger.warning("直连站点配置解析失败 %s: %s", target, exc)
        return DirectSitesConfig()


def reload_direct_sites_config(path: str | None = None) -> DirectSitesConfig:
    load_direct_sites_config.cache_clear()
    return load_direct_sites_config(path)
