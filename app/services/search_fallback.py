"""秘塔空结果后的 DuckDuckGo 新闻补缺：范围判定与查询改写。"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.config import MODULE_C_COMPANIES
from app.services.entity_catalog import EntityProfile, configured_entity_catalog

_EMPTY_MARKERS = (
    "未找到相关数据",
    "未找到相关",
    "no result",
    "not found",
    "zero results",
)
_JP_CHAR_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
_ASCII_NAME_RE = re.compile(r"[A-Za-z]")
_RECENCY_TAIL_RE = re.compile(
    r"(?:\s+最新\s+过去(?:24小时|一周)\b.*)$|(?:\s+\d{4}-\d{2}-\d{2}\s+OR\s+\d{4}年.*)$",
    re.I | re.S,
)
_A_CATEGORY_MARKERS = (
    "司法与行政监管",
    "金融与经营数据",
    "公开舆论与社交媒体",
    "供应链与关联方",
    "新闻 动态",
)
DDG_NEWS_TERMS = "適時開示 OR ニュースリリース OR IR OR 決算 OR earnings"
_ALLOWED_DDG_BACKENDS = frozenset({"duckduckgo"})


def is_empty_search_error(exc: BaseException | str | None) -> bool:
    text = str(exc or "").lower()
    if not text:
        return False
    return any(marker.lower() in text for marker in _EMPTY_MARKERS)


def is_japan_region(region: str | None) -> bool:
    return "日本" in str(region or "")


def allowed_ddg_backend(value: str | None) -> str:
    name = str(value or "duckduckgo").strip().lower() or "duckduckgo"
    if name not in _ALLOWED_DDG_BACKENDS:
        return "duckduckgo"
    return name


def should_fallback_to_ddg(
    module_code: str,
    *,
    enabled: bool = True,
    calendar_day=None,
    metadata: Optional[dict[str, Any]] = None,
    profile: Optional[EntityProfile] = None,
) -> bool:
    """仅模块 C，以及主体评估中的日本主体。历史日补采不用 DDG（无按日过滤）。"""
    if not enabled or calendar_day is not None:
        return False
    code = str(module_code or "").upper()
    if code == "C":
        return True
    if code != "A":
        return False
    return _japan_entity_for_query(metadata or {}, profile) is not None


def rewrite_ddg_news_query(
    query: str,
    module_code: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
    profile: Optional[EntityProfile] = None,
) -> str:
    """去掉中文类别词和时效套话，改成日英社名 + 披露/IR 词。"""
    meta = metadata or {}
    code = str(module_code or "").upper()
    if code == "C":
        names = _module_c_names(meta.get("company"))
        if names:
            return f"{_or_names(names)} {DDG_NEWS_TERMS}"
        stripped = _RECENCY_TAIL_RE.sub("", query or "").strip()
        return stripped or str(query or "").strip()
    names = _entity_names_for_ddg(meta, profile)
    if names:
        return f"{_or_names(names)} {DDG_NEWS_TERMS}"
    cleaned = str(query or "")
    for marker in _A_CATEGORY_MARKERS:
        cleaned = cleaned.replace(marker, " ")
    cleaned = _RECENCY_TAIL_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip() or str(query or "").strip()


def _japan_entity_for_query(
    metadata: dict[str, Any],
    profile: Optional[EntityProfile],
) -> EntityProfile | None:
    target = str(metadata.get("target") or metadata.get("company") or "").strip()
    if target:
        found = configured_entity_catalog().find([target])
        if found and is_japan_region(found.region):
            return found
    if profile and is_japan_region(profile.region):
        if not target:
            return profile
        needles = {_norm(name) for name in profile.all_names}
        if _norm(target) in needles:
            return profile
    return None


def _module_c_names(company: Any) -> list[str]:
    needle = _norm(company)
    if not needle:
        return []
    for jp_name, en_name in MODULE_C_COMPANIES:
        if needle in {_norm(jp_name), _norm(en_name)}:
            return [jp_name, en_name]
    return [str(company).strip()] if str(company).strip() else []


def _entity_names_for_ddg(
    metadata: dict[str, Any],
    profile: Optional[EntityProfile],
) -> list[str]:
    entity = _japan_entity_for_query(metadata, profile)
    if entity:
        return _pick_search_names(entity.all_names)
    target = str(metadata.get("target") or metadata.get("company") or "").strip()
    return [target] if target else []


def _pick_search_names(names: tuple[str, ...] | list[str]) -> list[str]:
    jp: list[str] = []
    en: list[str] = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        if _JP_CHAR_RE.search(name) and name not in jp:
            jp.append(name)
        elif _ASCII_NAME_RE.search(name) and name not in en:
            en.append(name)
    picked: list[str] = []
    for name in (*jp[:2], *en[:2]):
        if name not in picked:
            picked.append(name)
    return picked[:4]


def _or_names(names: list[str]) -> str:
    parts: list[str] = []
    for name in names:
        token = name.strip()
        if not token:
            continue
        if " " in token:
            parts.append(f'"{token}"')
        else:
            parts.append(token)
    return " OR ".join(parts)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()
