"""Conservative, deterministic normalization for evidence comparison."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional

from app.database.models import IndustryDataSource, IndustryEvidenceCard


SUBJECT_ALIASES: dict[str, str] = {}
METRIC_ALIASES = {
    "营业收入": "revenue", "营收": "revenue", "销售收入": "revenue", "revenue": "revenue",
    "净利润": "net_profit", "归母净利润": "net_profit_parent", "营业利润": "operating_profit",
    "ebitda": "ebitda", "债务": "debt", "总债务": "total_debt", "有息负债": "interest_bearing_debt",
    "装机容量": "installed_capacity", "产能": "capacity", "产量": "output",
    "项目数量": "project_count", "项目数": "project_count", "市场份额": "market_share",
    "增长率": "growth_rate", "增速": "growth_rate", "价格": "price", "单价": "unit_price",
    "建设成本": "construction_cost", "资本开支": "capex", "资本性支出": "capex",
}
POWER_FACTORS = {"kW": Decimal("1000"), "MW": Decimal("1000000"), "GW": Decimal("1000000000")}
ENERGY_FACTORS = {"kWh": Decimal("1000"), "MWh": Decimal("1000000"), "GWh": Decimal("1000000000")}
APPROXIMATE_MARKERS = ("约", "大约", "近", "超过", "不少于", "不低于", "最高", "最低", "区间", "左右", "approx")


def normalize_text_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s\-—_·,，.。:：;；()（）\[\]【】]+", "", value)


def normalize_subject(value: Optional[str], aliases: Optional[Mapping[str, str]] = None) -> Optional[str]:
    if not value:
        return None
    key = normalize_text_key(value)
    suffixes = ("株式会社", "有限责任公司", "股份有限公司", "有限公司", "inc", "corp", "corporation", "ltd")
    for suffix in suffixes:
        normalized_suffix = normalize_text_key(suffix)
        if key.startswith(normalized_suffix):
            key = key[len(normalized_suffix):] + normalized_suffix
            break
    alias_map = {normalize_text_key(k): normalize_text_key(v) for k, v in (aliases or SUBJECT_ALIASES).items()}
    return alias_map.get(key, key) or None


def normalize_metric(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = normalize_text_key(value)
    normalized_aliases = {normalize_text_key(k): v for k, v in METRIC_ALIASES.items()}
    return normalized_aliases.get(key, f"raw:{key}")


def normalize_period(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    compact = re.sub(r"\s+", "", text)
    match = re.fullmatch(r"(?:FY|fy)(\d{4})", compact)
    if match:
        return f"FY{match.group(1)}"
    match = re.fullmatch(r"(\d{4})(?:财年|財年)", compact)
    if match:
        return f"FY{match.group(1)}"
    match = re.fullmatch(r"(\d{4})年度", compact)
    if match:
        return f"ANNUAL{match.group(1)}"
    match = re.fullmatch(r"(\d{4})年", compact)
    if match:
        return f"CY{match.group(1)}"
    match = re.fullmatch(r"(\d{4})年?[Qq]([1-4])", compact)
    if match:
        return f"CY{match.group(1)}Q{match.group(2)}"
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月", compact)
    if match:
        return f"MONTH:{match.group(1)}-{int(match.group(2)):02d}"
    match = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", compact)
    if match:
        return f"DATE:{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return f"RAW:{normalize_text_key(text)}"


def claim_category(card: IndustryEvidenceCard) -> str:
    if card.claim_type == "forecast":
        quote = card.original_quote.casefold()
        if any(marker in quote for marker in ("目标", "力争", "target")):
            return "target"
        return "forecast"
    if card.claim_type == "reported_opinion":
        return "reported_opinion"
    return "actual"


def determine_dimension(metric_key: str, unit: Optional[str], currency: Optional[str]) -> tuple[str, Optional[str], Optional[Decimal]]:
    if unit in POWER_FACTORS:
        return "power", "W", POWER_FACTORS[unit]
    if unit in ENERGY_FACTORS:
        return "energy", "Wh", ENERGY_FACTORS[unit]
    if unit == "%":
        return "percentage", "ratio", Decimal("1")
    if unit == "倍":
        return "ratio", "ratio", Decimal("1")
    if currency:
        dimension = "price" if metric_key in {"price", "unit_price"} else "money"
        return dimension, currency, Decimal("1")
    if metric_key in {"project_count"}:
        return "count", "count", Decimal("1")
    return "other", unit, Decimal("1")


@dataclass(frozen=True)
class NormalizedEvidence:
    card: IndustryEvidenceCard
    source: IndustryDataSource
    subject_key: str
    metric_key: str
    period_key: str
    claim_category: str
    currency_key: Optional[str]
    dimension_key: str
    base_unit: Optional[str]
    comparison_value: Decimal
    approximate: bool
    restricted: bool
    restriction_reasons: tuple[str, ...]

    @property
    def strict_group_key(self) -> tuple[str, str, str, str, Optional[str], str]:
        return (
            self.subject_key, self.metric_key, self.period_key, self.claim_category,
            self.currency_key, self.dimension_key,
        )


def normalize_evidence(
    card: IndustryEvidenceCard,
    source: IndustryDataSource,
    aliases: Optional[Mapping[str, str]] = None,
) -> Optional[NormalizedEvidence]:
    subject = normalize_subject(card.subject, aliases)
    metric = normalize_metric(card.metric_name)
    period = normalize_period(card.period)
    if not subject or not metric or not period or card.normalized_value is None:
        return None
    try:
        numeric = Decimal(card.normalized_value)
    except InvalidOperation:
        return None
    dimension, base_unit, factor = determine_dimension(metric, card.unit, card.currency)
    if card.unit == "%" and "百分点" in card.original_quote:
        dimension, base_unit = "percentage_point", "percentage_point"
    if factor is None:
        return None
    if dimension == "money" or dimension == "price":
        if card.currency not in {"JPY", "CNY", "USD", "EUR"}:
            return None
    reasons = []
    if card.validation_status != "verified":
        reasons.append(card.validation_status)
    if card.requires_manual_review:
        reasons.append("manual_review")
    if card.evidence_grade == "partial_text" or source.is_truncated:
        reasons.append("partial_text")
    if source.used_ocr:
        reasons.append("ocr")
    if card.claim_type == "reported_opinion":
        reasons.append("reported_opinion")
    approximate = any(marker in card.original_quote.casefold() for marker in APPROXIMATE_MARKERS)
    return NormalizedEvidence(
        card=card, source=source, subject_key=subject, metric_key=metric,
        period_key=period, claim_category=claim_category(card), currency_key=card.currency,
        dimension_key=dimension, base_unit=base_unit, comparison_value=numeric * factor,
        approximate=approximate, restricted=bool(reasons),
        restriction_reasons=tuple(sorted(set(reasons))),
    )


def decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
