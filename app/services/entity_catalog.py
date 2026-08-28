"""可配置的主体监控清单与公开信源目录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Iterable

from app.config import get_settings
from app.services.rss_config import RssQuerySpec


_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _ROOT / "config" / "entity_targets.yaml"


@dataclass(frozen=True)
class EntitySourceSpec:
    label: str
    url: str
    source_type: str
    relation: str = "direct"
    priority: int = 10
    query: str | None = None
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "url": self.url,
            "source_type": self.source_type,
            "relation": self.relation,
            "priority": self.priority,
            "query": self.query,
            "enabled": self.enabled,
        }


FINANCIAL_STATEMENT_KEYS = ("income", "balance", "cashflow")
FINANCIAL_STATEMENT_TITLES = {
    "income": "损益表要点 (通期)",
    "balance": "资产负债表要点 (通期)",
    "cashflow": "现金流量表要点 (通期)",
}
FINANCIAL_STATEMENT_COLUMNS = {
    "income": (
        ("period", "报告期", "left"),
        ("revenue", "营业收入", "right"),
        ("operating_profit", "营业利润", "right"),
        ("ordinary_profit", "经常利润", "right"),
        ("net_profit", "净利润", "right"),
        ("eps", "每股收益", "right"),
        ("dps", "每股分红", "right"),
        ("released_at", "发布日", "right"),
    ),
    "balance": (
        ("period", "报告期", "left"),
        ("bps", "每股净资产", "right"),
        ("equity_ratio", "股东权益比率(%)", "right"),
        ("total_assets", "总资产", "right"),
        ("equity", "股东权益", "right"),
        ("retained_earnings", "留存收益", "right"),
        ("interest_bearing_debt_ratio", "有息负债倍率", "right"),
        ("released_at", "发布日", "right"),
    ),
    "cashflow": (
        ("period", "报告期", "left"),
        ("operating_profit", "营业益", "right"),
        ("free_cash_flow", "自由现金流", "right"),
        ("operating_cash_flow", "经营现金流", "right"),
        ("investing_cash_flow", "投资现金流", "right"),
        ("financing_cash_flow", "筹资现金流", "right"),
        ("cash_equivalents", "现金及等价物", "right"),
        ("cash_ratio", "现金比率", "right"),
    ),
}
DEFAULT_FINANCIAL_UNIT = "单位: 百万日元 (株探口径)"
DEFAULT_FINANCIAL_SOURCE_LABEL = "株探同期表 (整理自公开数据)"


@dataclass(frozen=True)
class FinancialSourceSpec:
    statement: str
    label: str
    url: str
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "title": FINANCIAL_STATEMENT_TITLES.get(self.statement, self.statement),
            "label": self.label,
            "url": self.url,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class FinancialContextMetric:
    """非上市主体可核验的经营/财务替代指标。

    这类数字通常来自母公司或集团披露，不能写入主体自己的三张财务报表。
    """

    label: str
    value: str
    as_of: str | None = None
    note: str | None = None
    url: str | None = None
    # 修改记录：2026-08-25 | DingJiaye
    # 同源、同口径的上次披露数值，用于财务面板的披露对比与图表；无可比口径时保持为空。
    previous_value: str | None = None
    previous_as_of: str | None = None
    previous_url: str | None = None
    current_numeric: float | None = None
    previous_numeric: float | None = None
    chart_group: str | None = None
    comparison_note: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "label": self.label,
            "value": self.value,
            "as_of": self.as_of,
            "note": self.note,
            "url": self.url,
            "previous_value": self.previous_value,
            "previous_as_of": self.previous_as_of,
            "previous_url": self.previous_url,
            "current_numeric": self.current_numeric,
            "previous_numeric": self.previous_numeric,
            "chart_group": self.chart_group,
            "comparison_note": self.comparison_note,
        }


@dataclass(frozen=True)
class EntityCategory:
    key: str
    label: str
    jump: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "jump": self.jump}


DEFAULT_ENTITY_CATEGORIES = (
    EntityCategory(key="重点关注", label="重点关注", jump="重点"),
    EntityCategory(key="五大商社", label="五大商社", jump="商社"),
    EntityCategory(key="三大银行", label="三大银行", jump="银行"),
    EntityCategory(key="三大券商", label="三大券商", jump="券商"),
    EntityCategory(key="其他", label="其他", jump="其他"),
)
FALLBACK_CATEGORY_KEY = "其他"
_VALID_PARTY_ROLES = {"parent", "shareholder", "supplier", "customer", "counterparty"}
_SOURCE_LABEL_STRIP = re.compile(
    r"\s*(newsroom|press room|新闻中心|新闻室|媒体中心|media center|"
    r"ir library|\bir\b|官网|官方网站|news release)\s*$",
    re.I,
)
_GENERIC_SOURCE_LABEL = re.compile(
    r"检索|fda|sec|监管|交易所|google|跨媒体|edinet|tdnet",
    re.I,
)


@dataclass(frozen=True)
class RelatedPartySpec:
    name: str
    aliases: tuple[str, ...] = ()
    role: str = "counterparty"

    @property
    def all_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.name, *self.aliases)))


@dataclass(frozen=True)
class VerifiedEntityEvent:
    """配置内、可追溯的历史事件；启动时仅按来源链接去重入库一次。"""

    title: str
    summary: str
    source_name: str
    source_url: str
    published_at: str
    risk_category: str = "供应链/关联方监控"
    risk_level: str = "中"
    related_company: str | None = None
    impact_analysis: str | None = None


@dataclass(frozen=True)
class EntityProfile:
    key: str
    display_name: str
    aliases: tuple[str, ...] = ()
    category: str = FALLBACK_CATEGORY_KEY
    industry: str | None = None
    region: str | None = None
    stock_code: str | None = None
    sources: tuple[EntitySourceSpec, ...] = field(default_factory=tuple)
    briefing_sources: tuple[EntitySourceSpec, ...] = field(default_factory=tuple)
    financial_sources: tuple[FinancialSourceSpec, ...] = field(default_factory=tuple)
    financial_source_page: str | None = None
    financial_source_label: str | None = None
    financial_unit: str | None = None
    financial_pdf_hint: str | None = None
    financial_context_metrics: tuple[FinancialContextMetric, ...] = field(default_factory=tuple)
    financial_context_notice: str | None = None
    financial_comparison_title: str | None = None
    financial_comparison_summary: str | None = None
    # 某些主体（如 GLP）虽有证券代码，但页面财务口径指定为其官方 IR 报告。
    # 此时不应以第三方株探数据覆盖官方来源。
    prefer_financial_pdf: bool = False
    related_parties: tuple[RelatedPartySpec, ...] = field(default_factory=tuple)
    executives: tuple[str, ...] = field(default_factory=tuple)
    # 修改记录：2026-08-25 | DingJiaye
    # 仅保存人工复核所需的持续监测清单；它们不是已发生风险或事实结论。
    monitoring_focus: tuple[str, ...] = field(default_factory=tuple)
    alert_rules: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)
    verified_events: tuple[VerifiedEntityEvent, ...] = field(default_factory=tuple)

    @property
    def all_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.key, self.display_name, *self.aliases)))

    def resolved_related_parties(self) -> tuple[RelatedPartySpec, ...]:
        """配置的股东/母公司/对手方，加上官方信源标签里推断出的集团名。"""
        seen = {_normalize_name(name) for name in self.all_names if name}
        out: list[RelatedPartySpec] = []
        for party in self.related_parties:
            compact = _normalize_name(party.name)
            if not compact or compact in seen:
                continue
            out.append(party)
            seen.update(_normalize_name(name) for name in party.all_names if name)
        for src in self.sources:
            if not src.enabled or src.source_type != "official":
                continue
            if src.relation == "contextual":
                continue
            label = _SOURCE_LABEL_STRIP.sub("", src.label).strip(" -–|/")
            if not label or _GENERIC_SOURCE_LABEL.search(label):
                continue
            compact = _normalize_name(label)
            if not compact or compact in seen:
                continue
            if any(token and token in compact for token in seen):
                continue
            out.append(RelatedPartySpec(name=label, role="parent"))
            seen.add(compact)
        return tuple(out)

    def rss_queries(self) -> list[RssQuerySpec]:
        specs: list[RssQuerySpec] = []
        for source in (*self.sources, *self.briefing_sources):
            if not source.enabled or not source.query:
                continue
            specs.append(
                RssQuerySpec(
                    label=source.label,
                    query=source.query or "",
                    priority=source.priority,
                    enabled=source.enabled,
                    entity_key=self.key,
                    relation=source.relation,
                    source_type=source.source_type,
                    source_url=source.url,
                )
            )
        return specs

    def as_seed(self) -> dict[str, str | None]:
        return {
            "name": self.key,
            "display_name": self.display_name,
            "aliases": ",".join(self.aliases),
            "industry": self.industry,
            "region": self.region,
        }


@dataclass(frozen=True)
class EntityCatalog:
    profiles: tuple[EntityProfile, ...]
    categories: tuple[EntityCategory, ...] = DEFAULT_ENTITY_CATEGORIES

    def find(self, names: Iterable[str | None]) -> EntityProfile | None:
        needles = [_normalize_name(name) for name in names if name and str(name).strip()]
        if not needles:
            return None
        for profile in self.profiles:
            candidates = {_normalize_name(name) for name in profile.all_names}
            if any(
                needle == candidate or needle in candidate or candidate in needle
                for needle in needles
                for candidate in candidates
                if candidate
            ):
                return profile
        return None


def _normalize_name(value: str | None) -> str:
    return "".join(str(value or "").casefold().split())


_BILINGUAL_NAME_RE = re.compile(
    r"^(?P<zh>.+?)\s+(?P<en>[A-Za-z][A-Za-z0-9&./'+ -]*)$"
)
_BILINGUAL_PAREN_RE = re.compile(
    r"^(?P<zh>.+?)\s*[（(](?P<en>[A-Za-z][^）)]*)[）)]\s*$"
)

# 目录外历史主体：按库内 name/display_name 归一后匹配。
_BILINGUAL_DISPLAY_OVERRIDES = {
    "bfinternational": "BF国际 BF International",
    "internationalcocoainitiative(ici)": "国际可可倡议 ICI",
    "internationalcocoainitiative": "国际可可倡议 ICI",
    "ici": "国际可可倡议 ICI",
    "加纳可可局(cocobod)": "加纳可可局 COCOBOD",
    "加纳可可局": "加纳可可局 COCOBOD",
    "cocobod": "加纳可可局 COCOBOD",
    "欧盟委员会": "欧盟委员会 European Commission",
    "europeancommission": "欧盟委员会 European Commission",
}


def split_bilingual_display_name(value: str | None) -> dict[str, str]:
    """把「中文 English」或「中文 (English)」显示名拆成两段。"""
    text = str(value or "").strip()
    for pattern in (_BILINGUAL_NAME_RE, _BILINGUAL_PAREN_RE):
        match = pattern.match(text)
        if not match:
            continue
        zh = match.group("zh").strip()
        en = match.group("en").strip()
        if zh and en:
            return {"zh": zh, "en": en}
    return {"zh": text, "en": ""}


def canonical_bilingual_display_name(*names: str | None) -> str:
    """目录外主体也尽量归一成中文加英文。"""
    for raw in names:
        key = _normalize_name(raw)
        if key in _BILINGUAL_DISPLAY_OVERRIDES:
            return _BILINGUAL_DISPLAY_OVERRIDES[key]
        stripped = _normalize_name(re.sub(r"[（(][^）)]*[）)]", "", str(raw or "")))
        if stripped in _BILINGUAL_DISPLAY_OVERRIDES:
            return _BILINGUAL_DISPLAY_OVERRIDES[stripped]
    for raw in names:
        parts = split_bilingual_display_name(raw)
        if parts["zh"] and parts["en"]:
            return f"{parts['zh']} {parts['en']}"
    return next((str(item).strip() for item in names if item and str(item).strip()), "")


def apply_bilingual_display_name(entity: Any) -> Any:
    """把拆分后的中英文名挂到主体对象上，供模板直接读取。"""
    canonical = canonical_bilingual_display_name(
        getattr(entity, "display_name", None),
        getattr(entity, "name", None),
        getattr(entity, "aliases", None),
    )
    parts = split_bilingual_display_name(canonical)
    setattr(entity, "name_zh", parts["zh"])
    setattr(entity, "name_en", parts["en"])
    return entity


def _parse_financial_source(row: dict[str, Any]) -> FinancialSourceSpec | None:
    if not isinstance(row, dict):
        return None
    statement = str(row.get("statement") or "").strip().lower()
    aliases = {
        "income": "income",
        "损益": "income",
        "损益表": "income",
        "利润表": "income",
        "balance": "balance",
        "负债": "balance",
        "负债表": "balance",
        "资产负债表": "balance",
        "cashflow": "cashflow",
        "流量": "cashflow",
        "流量表": "cashflow",
        "现金流量表": "cashflow",
        "现金流": "cashflow",
    }
    statement = aliases.get(statement, statement)
    label = str(row.get("label") or "").strip()
    url = str(row.get("url") or "").strip()
    if statement not in FINANCIAL_STATEMENT_KEYS or not label or not url:
        return None
    return FinancialSourceSpec(
        statement=statement,
        label=label,
        url=url,
        enabled=bool(row.get("enabled", True)),
    )


def _parse_financial_context_metric(row: dict[str, Any]) -> FinancialContextMetric | None:
    if not isinstance(row, dict):
        return None
    label = str(row.get("label") or "").strip()
    value = str(row.get("value") or "").strip()
    if not label or not value:
        return None
    return FinancialContextMetric(
        label=label,
        value=value,
        as_of=str(row["as_of"]).strip() if row.get("as_of") else None,
        note=str(row["note"]).strip() if row.get("note") else None,
        url=str(row["url"]).strip() if row.get("url") else None,
        previous_value=(
            str(row["previous_value"]).strip() if row.get("previous_value") else None
        ),
        previous_as_of=(
            str(row["previous_as_of"]).strip() if row.get("previous_as_of") else None
        ),
        previous_url=str(row["previous_url"]).strip() if row.get("previous_url") else None,
        current_numeric=_as_optional_float(row.get("current_numeric")),
        previous_numeric=_as_optional_float(row.get("previous_numeric")),
        chart_group=str(row["chart_group"]).strip() if row.get("chart_group") else None,
        comparison_note=(
            str(row["comparison_note"]).strip() if row.get("comparison_note") else None
        ),
    )


def _as_optional_float(value: Any) -> float | None:
    """配置中的图表数值仅接受可安全转换的数字，展示文本仍保留原始口径。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_related_party(row: dict[str, Any]) -> RelatedPartySpec | None:
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    role = str(row.get("role") or "counterparty").strip().lower()
    if role not in _VALID_PARTY_ROLES:
        role = "counterparty"
    aliases = tuple(
        str(alias).strip()
        for alias in (row.get("aliases") or [])
        if str(alias).strip()
    )
    return RelatedPartySpec(name=name, aliases=aliases, role=role)


def _parse_verified_event(row: dict[str, Any]) -> VerifiedEntityEvent | None:
    """读取可追溯的基准事件，缺少日期或来源链接时不入库。"""
    if not isinstance(row, dict):
        return None
    title = str(row.get("title") or "").strip()
    summary = str(row.get("summary") or "").strip()
    source_name = str(row.get("source_name") or "").strip()
    source_url = str(row.get("source_url") or "").strip()
    published_at = str(row.get("published_at") or "").strip()
    if not all((title, summary, source_name, source_url, published_at)):
        return None
    return VerifiedEntityEvent(
        title=title,
        summary=summary,
        source_name=source_name,
        source_url=source_url,
        published_at=published_at,
        risk_category=str(row.get("risk_category") or "供应链/关联方监控").strip(),
        risk_level=str(row.get("risk_level") or "中").strip(),
        related_company=str(row["related_company"]).strip() if row.get("related_company") else None,
        impact_analysis=str(row["impact_analysis"]).strip() if row.get("impact_analysis") else None,
    )


def _parse_source(row: dict[str, Any]) -> EntitySourceSpec | None:
    if not isinstance(row, dict):
        return None
    label = str(row.get("label") or "").strip()
    url = str(row.get("url") or "").strip()
    source_type = str(row.get("source_type") or "media").strip().lower()
    relation = str(row.get("relation") or "direct").strip().lower()
    if not label or not url:
        return None
    if relation not in {"direct", "contextual"}:
        relation = "direct"
    return EntitySourceSpec(
        label=label,
        url=url,
        source_type=source_type,
        relation=relation,
        priority=int(row.get("priority") or 10),
        query=str(row["query"]).strip() if row.get("query") else None,
        enabled=bool(row.get("enabled", True)),
    )


def _parse_categories(data: dict[str, Any]) -> tuple[EntityCategory, ...]:
    rows = data.get("categories")
    if not isinstance(rows, list) or not rows:
        return DEFAULT_ENTITY_CATEGORIES
    parsed: list[EntityCategory] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        label = str(row.get("label") or key).strip() or key
        jump = str(row.get("jump") or label).strip() or label
        parsed.append(EntityCategory(key=key, label=label, jump=jump))
    return tuple(parsed) if parsed else DEFAULT_ENTITY_CATEGORIES


def _resolve_category(raw: Any, valid: set[str]) -> str:
    key = str(raw or "").strip() or FALLBACK_CATEGORY_KEY
    if key in valid:
        return key
    if FALLBACK_CATEGORY_KEY in valid:
        return FALLBACK_CATEGORY_KEY
    return next(iter(valid), FALLBACK_CATEGORY_KEY)


def _parse_catalog(data: dict[str, Any]) -> EntityCatalog:
    categories = _parse_categories(data)
    valid_categories = {item.key for item in categories}
    profiles: list[EntityProfile] = []
    seen: set[str] = set()
    for row in data.get("entities") or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key or _normalize_name(key) in seen:
            continue
        seen.add(_normalize_name(key))
        sources = tuple(
            source
            for source in (_parse_source(item) for item in (row.get("sources") or []))
            if source is not None
        )
        briefing_sources = tuple(
            source
            for source in (_parse_source(item) for item in (row.get("briefing_sources") or []))
            if source is not None
        )
        financial_sources = tuple(
            source
            for source in (
                _parse_financial_source(item) for item in (row.get("financial_sources") or [])
            )
            if source is not None
        )
        financial_context_metrics = tuple(
            metric
            for metric in (
                _parse_financial_context_metric(item)
                for item in (row.get("financial_context_metrics") or [])
            )
            if metric is not None
        )
        related_parties = tuple(
            party
            for party in (
                _parse_related_party(item) for item in (row.get("related_parties") or [])
            )
            if party is not None
        )
        verified_events = tuple(
            event
            for event in (
                _parse_verified_event(item) for item in (row.get("verified_events") or [])
            )
            if event is not None
        )
        executives = tuple(
            str(item).strip()
            for item in (row.get("executives") or [])
            if str(item).strip()
        )
        monitoring_focus = tuple(
            str(item).strip()
            for item in (row.get("monitoring_focus") or [])
            if str(item).strip()
        )
        alert_rules: list[tuple[str, tuple[str, ...]]] = []
        raw_alert_rules = row.get("alert_rules") or {}
        if isinstance(raw_alert_rules, dict):
            for level in ("红色预警", "橙色预警", "黄色关注"):
                items = raw_alert_rules.get(level) or []
                if isinstance(items, str):
                    items = [items]
                normalized = tuple(str(item).strip() for item in items if str(item).strip())
                if normalized:
                    alert_rules.append((level, normalized))
        profiles.append(
            EntityProfile(
                key=key,
                display_name=str(row.get("display_name") or key).strip(),
                aliases=tuple(
                    str(alias).strip()
                    for alias in (row.get("aliases") or [])
                    if str(alias).strip()
                ),
                category=_resolve_category(row.get("category"), valid_categories),
                industry=str(row["industry"]).strip() if row.get("industry") else None,
                region=str(row["region"]).strip() if row.get("region") else None,
                stock_code=str(row["stock_code"]).strip() if row.get("stock_code") else None,
                sources=sources,
                briefing_sources=briefing_sources,
                financial_sources=financial_sources,
                related_parties=related_parties,
                executives=executives,
                monitoring_focus=monitoring_focus,
                alert_rules=tuple(alert_rules),
                verified_events=verified_events,
                financial_source_page=(
                    str(row["financial_source_page"]).strip()
                    if row.get("financial_source_page")
                    else None
                ),
                financial_source_label=(
                    str(row["financial_source_label"]).strip()
                    if row.get("financial_source_label")
                    else None
                ),
                financial_unit=(
                    str(row["financial_unit"]).strip() if row.get("financial_unit") else None
                ),
                financial_pdf_hint=(
                    str(row["financial_pdf_hint"]).strip()
                    if row.get("financial_pdf_hint")
                    else None
                ),
                financial_context_metrics=financial_context_metrics,
                financial_context_notice=(
                    str(row["financial_context_notice"]).strip()
                    if row.get("financial_context_notice")
                    else None
                ),
                financial_comparison_title=(
                    str(row["financial_comparison_title"]).strip()
                    if row.get("financial_comparison_title")
                    else None
                ),
                financial_comparison_summary=(
                    str(row["financial_comparison_summary"]).strip()
                    if row.get("financial_comparison_summary")
                    else None
                ),
                prefer_financial_pdf=bool(row.get("prefer_financial_pdf", False)),
            )
        )
    return EntityCatalog(tuple(profiles), categories)


@lru_cache(maxsize=4)
def load_entity_catalog(path: str | None = None) -> EntityCatalog:
    target = Path(path) if path else _DEFAULT_PATH
    if not target.is_absolute():
        target = _ROOT / target
    if not target.is_file():
        return EntityCatalog(())
    try:
        import yaml

        with target.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, ValueError):
        return EntityCatalog(())
    return _parse_catalog(data if isinstance(data, dict) else {})


def configured_entity_catalog() -> EntityCatalog:
    path = getattr(get_settings(), "entity_targets_config_path", None) or None
    return load_entity_catalog(path)


def reload_entity_catalog(path: str | None = None) -> EntityCatalog:
    load_entity_catalog.cache_clear()
    return load_entity_catalog(path)


def _entity_sort_key(entity: Any, catalog: EntityCatalog) -> tuple[int, str]:
    matched = catalog.find(
        (
            getattr(entity, "name", None),
            getattr(entity, "display_name", None),
            getattr(entity, "aliases", None),
        )
    )
    order = {item.key: index for index, item in enumerate(catalog.profiles)}
    key = matched.key if matched else str(getattr(entity, "name", "") or "")
    label = str(getattr(entity, "display_name", None) or getattr(entity, "name", "") or "")
    return (order.get(key, 10_000), label)


def group_monitored_entities(
    entities: Iterable[Any],
    catalog: EntityCatalog | None = None,
) -> list[dict[str, Any]]:
    """按目录分类分组监控主体，未匹配项归入「其他」。空分类仍保留，便于索引跳转。"""
    catalog = catalog or configured_entity_catalog()
    categories = catalog.categories or DEFAULT_ENTITY_CATEGORIES
    buckets: dict[str, list[Any]] = {item.key: [] for item in categories}
    fallback = (
        FALLBACK_CATEGORY_KEY
        if FALLBACK_CATEGORY_KEY in buckets
        else categories[-1].key
    )
    ordered = sorted(list(entities), key=lambda item: _entity_sort_key(item, catalog))
    for entity in ordered:
        profile = catalog.find(
            (
                getattr(entity, "name", None),
                getattr(entity, "display_name", None),
                getattr(entity, "aliases", None),
            )
        )
        category = profile.category if profile else fallback
        if category not in buckets:
            category = fallback
        buckets[category].append(apply_bilingual_display_name(entity))
    return [
        {
            "key": item.key,
            "label": item.label,
            "jump": item.jump,
            "entities": buckets[item.key],
        }
        for item in categories
    ]
