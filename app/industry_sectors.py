"""深度研报行业分类与独立数据库键（本地永久清单）。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

SECTORS_PATH = Path("data/industry_sectors.json")
# 兼容旧文件名
CUSTOM_SECTORS_PATH = SECTORS_PATH


@dataclass(frozen=True, slots=True)
class IndustrySector:
    key: str
    label: str
    default_industry_name: str
    keywords: tuple[str, ...] = ()


BUILTIN_SECTORS: dict[str, IndustrySector] = {
    sector.key: sector
    for sector in (
        IndustrySector("aviation", "航空", "航空运输", ("航空", "飞机", "机场", "航司")),
        IndustrySector("shipping", "航运", "航运", ("航运", "船舶", "港口", "海运", "船")),
        IndustrySector("passenger_cars", "乘用车", "乘用车", ("乘用车", "汽车", "车企", "整车")),
        IndustrySector("tires", "轮胎", "轮胎", ("轮胎", "橡胶")),
        IndustrySector("energy_storage", "储能", "储能", ("储能", "电池", "电芯")),
        IndustrySector("power", "电力", "电力", ("电力", "发电", "电网", "火电", "水电", "光伏", "风电")),
    )
}

# 运行时可变；其他模块以引用方式共享。
INDUSTRY_SECTORS: dict[str, IndustrySector] = dict(BUILTIN_SECTORS)


def _sector_to_json(sector: IndustrySector) -> dict:
    data = asdict(sector)
    data["keywords"] = list(sector.keywords)
    return data


def _sector_from_json(item: dict) -> IndustrySector:
    keywords = item.get("keywords") or ()
    return IndustrySector(
        key=str(item["key"]).strip(),
        label=str(item["label"]).strip(),
        default_industry_name=str(
            item.get("default_industry_name") or item.get("label") or ""
        ).strip(),
        keywords=tuple(str(k).strip() for k in keywords if str(k).strip()),
    )


def _read_raw() -> object | None:
    if not SECTORS_PATH.is_file():
        return None
    try:
        return json.loads(SECTORS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_registry() -> dict[str, IndustrySector]:
    """装载本地行业清单。无文件时返回内置六行业；兼容旧版仅含自定义列表的文件。"""
    raw = _read_raw()
    if raw is None:
        return dict(BUILTIN_SECTORS)

    items: list = []
    sole_source = False
    if isinstance(raw, dict) and isinstance(raw.get("sectors"), list):
        items = raw["sectors"]
        sole_source = True
    elif isinstance(raw, list):
        items = raw
    else:
        return dict(BUILTIN_SECTORS)

    parsed: dict[str, IndustrySector] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            sector = _sector_from_json(item)
        except (KeyError, TypeError, ValueError):
            continue
        if not sector.key or not sector.label:
            continue
        parsed[sector.key] = sector

    if sole_source:
        return parsed if parsed else dict(BUILTIN_SECTORS)

    # 旧格式：内置 + 文件中的自定义（同 key 时自定义覆盖）
    merged = dict(BUILTIN_SECTORS)
    merged.update(parsed)
    return merged


def save_registry(sectors: dict[str, IndustrySector]) -> None:
    SECTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "sectors": [_sector_to_json(s) for s in sectors.values()],
    }
    SECTORS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def refresh_sectors() -> dict[str, IndustrySector]:
    INDUSTRY_SECTORS.clear()
    INDUSTRY_SECTORS.update(load_registry())
    if not INDUSTRY_SECTORS:
        INDUSTRY_SECTORS.update(BUILTIN_SECTORS)
    return INDUSTRY_SECTORS


def ensure_registry_file() -> None:
    """首次启动时把当前清单落到本地，之后增删改都持久化。"""
    if SECTORS_PATH.is_file():
        return
    refresh_sectors()
    save_registry(INDUSTRY_SECTORS)


def sector_keys() -> tuple[str, ...]:
    return tuple(INDUSTRY_SECTORS.keys())


def require_sector_key(sector_key: str) -> IndustrySector:
    sector = INDUSTRY_SECTORS.get(sector_key)
    if not sector:
        refresh_sectors()
        sector = INDUSTRY_SECTORS.get(sector_key)
    if not sector:
        raise KeyError(sector_key)
    return sector


def guess_sector_key(industry_name: str, default: str = "aviation") -> str:
    text = (industry_name or "").strip()
    if not INDUSTRY_SECTORS:
        refresh_sectors()
    if not text:
        return default if default in INDUSTRY_SECTORS else next(iter(INDUSTRY_SECTORS))
    for sector in INDUSTRY_SECTORS.values():
        if any(keyword in text for keyword in sector.keywords):
            return sector.key
        if sector.label and sector.label in text:
            return sector.key
    return default if default in INDUSTRY_SECTORS else next(iter(INDUSTRY_SECTORS))


def _slug_candidate(label: str) -> str:
    text = label.strip()
    ascii_slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
    has_word = bool(re.search(r"[a-z]{2,}", ascii_slug or ""))
    if has_cjk or not ascii_slug or not has_word:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        return f"sector_{digest}"
    return ascii_slug


def make_unique_sector_key(label: str) -> str:
    base = _slug_candidate(label)
    if base not in INDUSTRY_SECTORS:
        return base
    for idx in range(2, 1000):
        candidate = f"{base}_{idx}"
        if candidate not in INDUSTRY_SECTORS:
            return candidate
    digest = hashlib.sha1(f"{label}-{len(INDUSTRY_SECTORS)}".encode("utf-8")).hexdigest()[:10]
    return f"sector_{digest}"


def _assert_unique_label(label: str, *, exclude_key: str | None = None) -> None:
    for sector in INDUSTRY_SECTORS.values():
        if exclude_key and sector.key == exclude_key:
            continue
        if sector.label == label:
            raise ValueError("该行业名称已存在")


def add_sector(label: str, *, default_industry_name: str | None = None) -> IndustrySector:
    refresh_sectors()
    normalized = " ".join((label or "").split())
    if not normalized:
        raise ValueError("行业名称不能为空")
    if len(normalized) > 64:
        raise ValueError("行业名称不能超过 64 个字符")
    _assert_unique_label(normalized)
    key = make_unique_sector_key(normalized)
    sector = IndustrySector(
        key=key,
        label=normalized,
        default_industry_name=(default_industry_name or normalized).strip() or normalized,
        keywords=(normalized,),
    )
    INDUSTRY_SECTORS[key] = sector
    save_registry(INDUSTRY_SECTORS)
    return sector


def rename_sector(sector_key: str, label: str) -> IndustrySector:
    refresh_sectors()
    current = INDUSTRY_SECTORS.get(sector_key)
    if not current:
        raise ValueError("行业不存在")
    normalized = " ".join((label or "").split())
    if not normalized:
        raise ValueError("行业名称不能为空")
    if len(normalized) > 64:
        raise ValueError("行业名称不能超过 64 个字符")
    _assert_unique_label(normalized, exclude_key=sector_key)
    default_name = current.default_industry_name
    if default_name == current.label:
        default_name = normalized
    keywords = tuple(dict.fromkeys((normalized, *current.keywords)))
    updated = IndustrySector(
        key=current.key,
        label=normalized,
        default_industry_name=default_name,
        keywords=keywords,
    )
    INDUSTRY_SECTORS[sector_key] = updated
    save_registry(INDUSTRY_SECTORS)
    return updated


def remove_sector(sector_key: str) -> IndustrySector:
    refresh_sectors()
    current = INDUSTRY_SECTORS.get(sector_key)
    if not current:
        raise ValueError("行业不存在")
    del INDUSTRY_SECTORS[sector_key]
    save_registry(INDUSTRY_SECTORS)
    return current


# 兼容旧调用名
def load_custom_sectors() -> dict[str, IndustrySector]:
    registry = load_registry()
    return {k: v for k, v in registry.items() if k not in BUILTIN_SECTORS}


def save_custom_sectors(custom: dict[str, IndustrySector]) -> None:
    refresh_sectors()
    for key in list(INDUSTRY_SECTORS):
        if key not in BUILTIN_SECTORS:
            del INDUSTRY_SECTORS[key]
    INDUSTRY_SECTORS.update(custom)
    save_registry(INDUSTRY_SECTORS)


refresh_sectors()
