"""主体评估的确定性归属门禁与信号字段归一化。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

from app.database.models import TargetEntity
from app.services.entity_catalog import EntityProfile, RelatedPartySpec


_IMPACT_TO_RISK = {
    "none": "低",
    "low": "中",
    "medium": "高",
    "high": "极高",
    "critical": "极高",
}
_VALID_DIRECTIONS = {"positive", "neutral", "negative", "unknown"}
_VALID_IMPACTS = set(_IMPACT_TO_RISK)
_VALID_IMPORTANCE = {"低", "中", "高", "极高"}
_OFFICIAL_SOURCE_TYPES = {"official", "regulatory", "exchange"}

# 行业协会/商品组织：不能当成上下游企业。
_INDUSTRY_BODIES = (
    "icco",
    "cocobod",
    "ici",
    "opec",
    "eia",
    "fao",
    "wto",
    "国际可可组织",
    "加纳可可局",
    "国际可可倡议",
    "cocoa board",
    "cocoa organization",
)

# 行业词不得代替具体公司名。
_INDUSTRY_STANDINS = (
    "可可",
    "巧克力",
    "cocoa",
    "chocolate",
    "供应链",
    "供應鏈",
    "上游",
    "下游",
    "产季",
    "期货",
    "futures",
    "harvest",
    "crop",
)

_FALSE_COMPANY = {
    "monthly cocoa",
    "cocoa market",
    "cocoa board",
    "cocoa prices",
    "supply chain",
    "chocolate industry",
    "gift boxes",
    "new york",
    "hong kong",
    "united states",
    "ivory coast",
    "costa rica",
}

# 未写入 related_parties 的零售渠道/商场，不得当成默认关联方。
_GENERIC_CHANNELS = (
    "walmart",
    "沃尔玛",
    "costco",
    "tesco",
    "target",
    "amazon",
    "亚马逊",
    "carrefour",
    "家乐福",
    "aeon",
    "永旺",
    "伊藤洋华堂",
    "shopping mall",
    "shopping center",
    "department store",
    "购物中心",
    "商场",
    "百货",
    "mall",
)

_GENERIC_INDUSTRY_TOKENS = {
    "消费品",
    "消費品",
    "制造",
    "製造",
    "行业",
    "行業",
    "产业",
    "產業",
    "全球",
    "综合",
    "綜合",
    "集团",
    "集團",
    "公司",
    "其他",
    "general",
    "other",
}
_INDUSTRY_TOKEN_ALIASES = {
    "巧克力": ("巧克力", "chocolate", "chocolat", "cocoa", "可可"),
}


def _normalize(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _normalize(value))


def _contains_name(text: str, name: str) -> bool:
    needle = _normalize(name).strip()
    if len(needle) < 2:
        return False
    if re.fullmatch(r"[a-z0-9&.\- ]+", needle):
        pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return needle in text


def _contains_name_loose(text: str, name: str) -> bool:
    """名称匹配；域名/无空格写法也算命中。"""
    if _contains_name(text, name):
        return True
    compact_name = _compact(name)
    compact_text = _compact(text)
    if len(compact_name) >= 5 and compact_name in compact_text:
        return True
    return False


def entity_names(entity: TargetEntity, profile: EntityProfile | None) -> tuple[str, ...]:
    names: list[str] = [entity.name, entity.display_name or ""]
    if entity.aliases:
        names.extend(alias.strip() for alias in entity.aliases.split(",") if alias.strip())
    if profile:
        names.extend(profile.all_names)
    return tuple(dict.fromkeys(name for name in names if name))


def related_party_specs(
    entity: TargetEntity | None,
    profile: EntityProfile | None,
) -> tuple[RelatedPartySpec, ...]:
    if profile:
        return profile.resolved_related_parties()
    return ()


def _related_party_names(
    entity: TargetEntity | None,
    profile: EntityProfile | None,
) -> tuple[str, ...]:
    names: list[str] = []
    for party in related_party_specs(entity, profile):
        names.extend(party.all_names)
    return tuple(dict.fromkeys(name for name in names if name))


def _row_title(row: dict[str, Any]) -> str:
    source_item = row.get("_source_item") if isinstance(row.get("_source_item"), dict) else {}
    return str(row.get("标题") or source_item.get("title") or "")


def _row_blob(row: dict[str, Any]) -> str:
    source_item = row.get("_source_item") if isinstance(row.get("_source_item"), dict) else {}
    parts = (
        row.get("标题"),
        row.get("关联企业"),
        row.get("核心摘要"),
        row.get("影响分析"),
        row.get("来源名称"),
        source_item.get("title"),
        source_item.get("snippet"),
        source_item.get("body"),
        source_item.get("company"),
        source_item.get("publisher"),
        source_item.get("feed"),
        source_item.get("url"),
        source_item.get("source_domain"),
    )
    return " ".join(str(value or "") for value in parts)


_MARKETING_RE = re.compile(
    r"礼盒|快闪|快閃|代言|评测|評測|旅游攻略|旅遊攻略|新品(?:上市|发布|發布)|"
    r"口味|情人节礼物|聖誕禮|圣诞礼|holiday collection|"
    r"gift\s*guide|pop[- ]?up|endorsement|unboxing|taste\s*test|new flavor|"
    r"(?:best|top)\s+\d*.{0,24}(?:chocolate|chocolat)|"
    r"巧克力(?:榜单|推薦|推荐|排行)|opens seasonal shop|seasonal (?:shop|collection)",
    re.I,
)
_LISTICLE_RE = re.compile(
    r"including|such as|ranked|榜单|推薦|推荐|排行|盘点|盤點|例如|譬如",
    re.I,
)
_PASSING_MENTION_RE = re.compile(
    r"(?:including|such as|like|among them|包括|例如|譬如|其中)\s*$",
    re.I,
)
_INDUSTRY_MACRO_RE = re.compile(
    r"\bicco\b|\bcocobod\b|国际可可组织|加纳可可局|"
    r"可可价格|可可期货|cocoa price|cocoa futures|"
    r"产季|crop forecast|harvest season|大宗商品|"
    r"行业(?:综述|报告|展望|esg|背景)|"
    r"industry (?:outlook|report|esg|background)|"
    r"协会报告|association report",
    re.I,
)
_MATERIAL_EVENT_RE = re.compile(
    r"处罚|處罰|调查|調查|诉讼|訴訟|起诉|起訴|召回|制裁|罚款|罰款|合规|合規|"
    r"财报|財報|決算|业绩|業績|评级|評級|融資|融资|债务|債務|违约|違約|债券|債券|"
    r"关停|關停|停产|停產|重组|重組|破产|破產|倒闭|倒閉|"
    r"核心业务中断|经营中断|履约|履約|质量事故|質量事故|"
    r"lawsuit|litigation|recall|sanction|earnings|default|"
    r"downgrade|investigation|fine|penalty|restructuring|"
    r"bankruptcy|shutdown|class action",
    re.I,
)
_PARTY_EVENT_RE = re.compile(
    r"持股|增持|减持|減持|收购|收購|出售|控制权|控制權|控股|"
    r"债务|債務|评级|評級|重组|重組|处罚|處罰|调查|調查|"
    r"任免|任命|辞职|辭職|就任|解任|更换|"
    r"交货中断|交貨中斷|交付中断|交付中斷|停供|断供|斷供|"
    r"质量事故|質量事故|制裁|破产|破產|"
    r"许可|授權|授权|licensing|\blicen[cs]e[sd]?\b|"
    r"资产(?:出售|转让|轉讓|交易)|asset (?:sale|deal)|"
    r"debt|refinance|downgrade|acquisition|divest|"
    r"delivery (?:halt|delay)|halted (?:deliveries|shipments)|"
    r"supply disruption|sanction|bankruptcy",
    re.I,
)
_PERSONNEL_RE = re.compile(
    r"高管|董事长|社長|役員|董事|CEO|CFO|COO|人事|任命|就任|辞任|辞职|辭職|"
    r"退任|解任|更换|appoint(?:ed|s|ment)?|resign(?:ed|s|ation)?|steps down",
    re.I,
)
_INDUSTRY_SHOCK_RE = re.compile(
    r"尽职调查|盡職調查|due diligence|新规|新規|新监管|新監管|"
    r"(?:eu|欧盟|歐盟).{0,32}(?:regulation|法规|法規)|"
    r"deforestation|\beudr\b|\bcsddd\b|进口禁令|進口禁令",
    re.I,
)
_MARKET_OPERATOR_RE = re.compile(
    r"进口商|進口商|制造商|製造商|生产商|生產商|厂商|廠商|"
    r"importer|manufacturer|producer",
    re.I,
)
_LEADING_SUBJECT_RE = re.compile(
    r"^(?:the\s+)?(?P<name>[A-Z][\w&.\-']+(?:\s+[A-Z][\w&.\-']+){0,4}|"
    r"[\u4e00-\u9fff]{2,24}(?:公司|集团|集團|控股)?)"
    r"\s+"
    r"(?P<rest>面临|宣布|公布|发布|發布|召回|起诉|起訴|任命|辞职|辭職|"
    r"收购|收購|出售|lawsuit|recall|appoints|faces|files|announces|"
    r"reports|halts|sells)",
    re.I,
)
_OFFICIAL_HINT_RE = re.compile(
    r"fda\.gov|sec\.gov|\.tdnet\.|edinetinfo|sgx\.com|jpx\.co\.jp|"
    r"监管局|交易所|证监会|證監會|適時開示|官方公告|"
    r"newsroom|media center|新闻中心|新闻室|press room|press-room|"
    r"ir library|\bir\b",
    re.I,
)
_RELATION_RE = re.compile(
    r"供应商|供應商|客户|客戶|经销商|經銷商|代工|物流商|承包商|"
    r"母公司|控股股东|控股股東|主要股东|主要股東|"
    r"supplier|customer|distributor|parent company|"
    r"controlling shareholder|holding company|counterparty",
    re.I,
)
_COMPANY_SUFFIX_RE = re.compile(
    r"\b[A-Z][\w&.\-']{1,40}"
    r"(?:\s+[A-Z][\w&.\-']{1,30}){0,4}"
    r"\s+(?:Inc\.?|Ltd\.?|LLC|Corp\.?|GmbH|S\.?A\.?|AG|PLC|"
    r"Holding|Holdings|Group|株式会社)\b"
    r"|[\u4e00-\u9fff]{2,20}(?:公司|集团|集團|控股|株式会社)",
)
_PROPER_NAME_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,3}\b")
_NEAR_RELATION_RE = re.compile(
    r"(.{0,48})("
    r"供应商|供應商|客户|客戶|经销商|經銷商|代工|物流商|"
    r"母公司|控股股东|控股股東|主要股东|主要股東|"
    r"supplier|customer|distributor|parent company|"
    r"controlling shareholder"
    r")(.{0,48})",
    re.I,
)


def _is_industry_body(name: str) -> bool:
    compact = _compact(name)
    if not compact:
        return False
    return any(_compact(body) and _compact(body) in compact for body in _INDUSTRY_BODIES)


def _is_industry_standin(name: str) -> bool:
    compact = _compact(name)
    return compact in {_compact(item) for item in _INDUSTRY_STANDINS}


def _is_marketing_copy(text: str) -> bool:
    return bool(_MARKETING_RE.search(text or ""))


def _is_industry_macro(text: str) -> bool:
    return bool(_INDUSTRY_MACRO_RE.search(text or ""))


def _has_material_company_event(text: str) -> bool:
    return bool(_MATERIAL_EVENT_RE.search(text or ""))


def _has_party_event(text: str) -> bool:
    return bool(_PARTY_EVENT_RE.search(text or ""))


def _is_listicle(text: str) -> bool:
    return bool(_LISTICLE_RE.search(text or ""))


def _is_passing_mention(text: str, names: tuple[str, ...]) -> bool:
    normalized = _normalize(text)
    for name in names:
        needle = _normalize(name).strip()
        if len(needle) < 2:
            continue
        for match in re.finditer(re.escape(needle), normalized):
            prefix = normalized[max(0, match.start() - 24) : match.start()]
            if _PASSING_MENTION_RE.search(prefix):
                return True
    return False


def _looks_like_brand_list(title: str, names: tuple[str, ...]) -> bool:
    if not any(_contains_name(_normalize(title), name) for name in names):
        return False
    if title.count(",") >= 1 and re.search(r"\b(and|与|和|/)\b", title, re.I):
        return True
    return False


def _entity_is_subject(title: str, blob: str, names: tuple[str, ...]) -> bool:
    """名称出现不等于主语。营销榜单、并列品牌里的点名不算。"""
    title_n = _normalize(title)
    blob_n = _normalize(blob)
    in_title = any(_contains_name(title_n, name) for name in names)
    if in_title:
        if _is_passing_mention(title, names):
            return False
        if _looks_like_brand_list(title, names) and not _has_material_company_event(title):
            return False
        if _is_listicle(title) and not _has_material_company_event(title):
            return False
        return True
    lead = blob_n[:280]
    if any(_contains_name(lead, name) for name in names):
        if _is_passing_mention(blob[:280], names) or (
            _is_listicle(blob[:280]) and not _has_material_company_event(blob[:280])
        ):
            return False
        return True
    return False


def _iter_company_candidates(text: str) -> list[str]:
    found: list[str] = []
    for match in _COMPANY_SUFFIX_RE.finditer(text or ""):
        found.append(match.group(0).strip())
    for match in _PROPER_NAME_RE.finditer(text or ""):
        found.append(match.group(0).strip())
    return found


def _is_generic_channel(name: str) -> bool:
    normalized = _normalize(name)
    compact = _compact(name)
    for item in _GENERIC_CHANNELS:
        item_n = _normalize(item)
        if len(item_n) <= 4 and re.fullmatch(r"[a-z]+", item_n):
            if re.search(r"(?<![a-z])" + re.escape(item_n) + r"(?![a-z])", normalized):
                return True
            continue
        item_c = _compact(item)
        if item_c and item_c in compact:
            return True
    return False


def _is_usable_company_name(name: str, exclude: tuple[str, ...]) -> bool:
    cleaned = name.strip(" ,.;:()[]\"'")
    if len(cleaned) < 3:
        return False
    if _normalize(cleaned) in _FALSE_COMPANY:
        return False
    if _is_industry_body(cleaned) or _is_industry_standin(cleaned) or _is_generic_channel(cleaned):
        return False
    if any(_contains_name_loose(cleaned, item) for item in exclude if item):
        return False
    return True


def _named_counterparty_in_text(text: str, exclude: tuple[str, ...]) -> str | None:
    """材料里点名的具体公司；行业词、协会名不算。"""
    if not _RELATION_RE.search(text or ""):
        return None
    for match in _NEAR_RELATION_RE.finditer(text or ""):
        window = f"{match.group(1)} {match.group(3)}"
        for candidate in _iter_company_candidates(window):
            if _is_usable_company_name(candidate, exclude):
                return candidate
    for candidate in _iter_company_candidates(text or ""):
        if _is_usable_company_name(candidate, exclude):
            return candidate
    return None


def _mentions_related_party(text: str, parties: tuple[str, ...]) -> bool:
    return any(_contains_name_loose(text, name) for name in parties if name)


def _entity_own_event(title: str, names: tuple[str, ...]) -> bool:
    """目标企业名称后紧跟其自身事件，而不是股东/供应商事件。"""
    title_n = _normalize(title)
    if not title_n:
        return False
    for name in names:
        needle = _normalize(name).strip()
        if len(needle) < 2:
            continue
        for match in re.finditer(re.escape(needle), title_n):
            after = title_n[match.end() : match.end() + 28]
            if _MATERIAL_EVENT_RE.search(after) or _EXEC_RE.search(after) or _FINANCE_RE.search(after):
                return True
    return False


def _executive_names(profile: EntityProfile | None) -> tuple[str, ...]:
    if profile is None:
        return ()
    return tuple(name for name in profile.executives if name)


def _is_named_executive_event(text: str, profile: EntityProfile | None) -> bool:
    names = _executive_names(profile)
    if not names or not any(_contains_name_loose(text, name) for name in names):
        return False
    return bool(_PERSONNEL_RE.search(text or "") or _has_party_event(text))


def _profile_industry_tokens(profile: EntityProfile | None) -> tuple[str, ...]:
    if profile is None or not profile.industry:
        return ()
    tokens: list[str] = []
    for part in re.split(r"[/,，、|]", profile.industry):
        token = part.strip()
        if len(token) < 2 or token in _GENERIC_INDUSTRY_TOKENS:
            continue
        tokens.append(token)
        tokens.extend(_INDUSTRY_TOKEN_ALIASES.get(token, ()))
    return tuple(dict.fromkeys(tokens))


def _is_industry_shock_for_entity(
    text: str,
    *,
    names: tuple[str, ...],
    related_names: tuple[str, ...],
    profile: EntityProfile | None,
) -> bool:
    """行业冲击仅当点名关联方，或明确约束该市场进口商/制造商时保留。"""
    if not _INDUSTRY_SHOCK_RE.search(text or ""):
        return False
    if any(_contains_name_loose(text, name) for name in (*names, *related_names) if name):
        return True
    if not _MARKET_OPERATOR_RE.search(text or ""):
        return False
    text_n = _normalize(text)
    return any(_contains_name(text_n, token) for token in _profile_industry_tokens(profile))


def _foreign_company_subject(
    title: str,
    names: tuple[str, ...],
    related_names: tuple[str, ...],
) -> bool:
    """标题主语是其他公司，且不是目标企业或已配置关联方。"""
    if any(_contains_name(_normalize(title), name) for name in names if name):
        return False
    if any(_contains_name_loose(title, name) for name in related_names if name):
        return False
    match = _LEADING_SUBJECT_RE.match(str(title or "").strip())
    if not match:
        return False
    lead = match.group("name")
    if any(_contains_name_loose(lead, name) for name in (*names, *related_names) if name):
        return False
    return True


def _is_official_disclosure(risk: Any, blob: str) -> bool:
    source = " ".join(
        str(getattr(risk, key, None) or "")
        for key in ("source_name", "source_url")
    )
    return bool(_OFFICIAL_HINT_RE.search(source) or _OFFICIAL_HINT_RE.search(blob or ""))


def _named_related_party_event(
    text: str,
    *,
    entity_names_: tuple[str, ...],
    related_names: tuple[str, ...] = (),
    parties: tuple[RelatedPartySpec, ...] = (),
    require_target: bool = True,
) -> bool:
    """已点名的股东/母公司/上下游企业新闻。无公司名的行业背景不算。"""
    names = related_names or tuple(name for party in parties for name in party.all_names)
    if parties:
        for party in parties:
            if not _mentions_related_party(text, party.all_names):
                continue
            if _has_party_event(text) or _has_material_company_event(text):
                return True
            if party.role in {"parent", "shareholder"} and _PERSONNEL_RE.search(text or ""):
                return True
    elif _mentions_related_party(text, names) and (
        _has_party_event(text) or _has_material_company_event(text)
    ):
        return True
    mentioned_target = any(_contains_name_loose(text, name) for name in entity_names_ if name)
    if require_target and entity_names_ and not mentioned_target:
        return False
    counterparty = _named_counterparty_in_text(text, entity_names_)
    if not counterparty:
        return False
    if _is_industry_macro(text) and not _RELATION_RE.search(text or ""):
        return False
    return _has_party_event(text) or _has_material_company_event(text)


def classify_entity_relevance(
    entity: TargetEntity,
    row: dict[str, Any],
    profile: EntityProfile | None,
) -> str:
    """返回 direct/contextual/unrelated；不单独信任模型给出的归属结论。"""
    source_item = row.get("_source_item") if isinstance(row.get("_source_item"), dict) else {}
    if not source_item:
        # 自动分析必须能回连到一条采集候选；不信任无法溯源的模型归属判断。
        return "unrelated"

    blob = _row_blob(row)
    title = _row_title(row)
    names = entity_names(entity, profile)
    parties = related_party_specs(entity, profile)
    related_names = _related_party_names(entity, profile)
    subject = _entity_is_subject(title, blob, names)
    related_event = _named_related_party_event(
        blob, entity_names_=names, parties=parties, related_names=related_names
    )
    exec_event = _is_named_executive_event(blob, profile)
    industry_shock = _is_industry_shock_for_entity(
        blob, names=names, related_names=related_names, profile=profile
    )

    if _is_marketing_copy(blob) and not _has_material_company_event(blob):
        return "unrelated"
    if _is_industry_macro(blob) and not subject and not related_event and not industry_shock:
        return "unrelated"
    if _foreign_company_subject(title, names, related_names) and not related_event and not subject:
        return "unrelated"

    own_event = _entity_own_event(title, names)
    if related_event and not own_event:
        return "contextual"
    if exec_event and not own_event and not subject:
        return "contextual"
    if subject:
        return "direct"

    scoped_key = str(source_item.get("entity_key") or "").strip()
    same_scope = bool(profile and scoped_key == profile.key)
    relation = str(source_item.get("relation") or "unscoped").lower()
    source_type = str(source_item.get("source_type") or "media").lower()
    source_meta = " ".join(
        str(source_item.get(key) or "")
        for key in ("feed", "publisher", "url", "source_domain")
    )
    source_is_related = _mentions_related_party(source_meta, related_names)

    if same_scope and relation == "direct" and source_type in _OFFICIAL_SOURCE_TYPES:
        if _is_marketing_copy(blob) and not _has_material_company_event(blob):
            return "unrelated"
        if source_is_related and not subject:
            return "contextual" if related_event or _has_party_event(blob) or exec_event else "unrelated"
        return "direct"
    if same_scope and relation == "contextual":
        return "contextual" if related_event or industry_shock or exec_event else "unrelated"
    if industry_shock or exec_event:
        return "contextual"
    return "unrelated"


def _confidence(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def source_name_for_row(row: dict[str, Any]) -> str | None:
    source_item = row.get("_source_item") if isinstance(row.get("_source_item"), dict) else {}
    explicit = str(
        row.get("来源名称")
        or source_item.get("publisher")
        or source_item.get("feed")
        or ""
    ).strip()
    if explicit:
        return explicit
    url = str(row.get("来源链接") or source_item.get("url") or "").strip()
    return urlparse(url).netloc or None


def prepare_entity_row(
    entity: TargetEntity,
    row: dict[str, Any],
    profile: EntityProfile | None,
) -> bool:
    """归一并校验一条模型输出；无关条目返回 False。"""
    relevance = classify_entity_relevance(entity, row, profile)
    if relevance == "unrelated":
        return False

    direction = str(row.get("影响方向") or "unknown").strip().lower()
    if direction not in _VALID_DIRECTIONS:
        direction = "unknown"
    impact = str(row.get("信用风险信号") or "none").strip().lower()
    if impact not in _VALID_IMPACTS:
        impact = "none"
    if relevance != "direct" or direction != "negative" or row.get("_degraded"):
        impact = "none"

    importance = str(row.get("资讯重要度") or row.get("风险等级") or "中").strip()
    if importance not in _VALID_IMPORTANCE:
        importance = "中"

    row["关联企业"] = entity.display_name or entity.name
    row["主体相关性"] = relevance
    row["影响方向"] = direction
    row["信用风险信号"] = impact
    row["资讯重要度"] = importance
    row["风险等级"] = _IMPACT_TO_RISK[impact]
    if relevance == "contextual":
        row["风险类别"] = "供应链/关联方监控"
    row["来源名称"] = source_name_for_row(row) or ""
    row["_entity_relevance"] = relevance
    row["_confidence"] = _confidence(row.get("置信度"))
    row["风险类别"] = canonical_risk_category(row.get("风险类别"))
    return True


def prepare_ai_entity_row(row: dict[str, Any]) -> bool:
    """归一 AI 已核验的主体事件，不再以静态名称匹配覆盖模型判断。

    模型仍必须给出可追溯来源、相关性和风险字段；此函数只做字段合法性与
    ``contextual`` 不得冒充主体信用风险的硬约束，不产生任何新闻事实。
    """
    relevance = str(row.get("主体相关性") or "").strip().lower()
    if relevance not in {"direct", "contextual"}:
        return False
    if not str(row.get("标题") or "").strip() or not str(row.get("来源链接") or "").strip():
        return False

    direction = str(row.get("影响方向") or "unknown").strip().lower()
    if direction not in _VALID_DIRECTIONS:
        direction = "unknown"
    impact = str(row.get("信用风险信号") or "none").strip().lower()
    if impact not in _VALID_IMPACTS:
        impact = "none"
    if relevance != "direct" or direction != "negative" or row.get("_degraded"):
        impact = "none"

    importance = str(row.get("资讯重要度") or row.get("风险等级") or "中").strip()
    if importance not in _VALID_IMPORTANCE:
        importance = "中"
    row["主体相关性"] = relevance
    row["影响方向"] = direction
    row["信用风险信号"] = impact
    row["资讯重要度"] = importance
    row["风险等级"] = _IMPACT_TO_RISK[impact]
    if relevance == "contextual":
        row["风险类别"] = "供应链/关联方监控"
    row["来源名称"] = source_name_for_row(row) or ""
    row["_entity_relevance"] = relevance
    row["_confidence"] = _confidence(row.get("置信度"))
    row["风险类别"] = canonical_risk_category(row.get("风险类别"))
    return True


RISK_TAB_LABELS = {
    "judicial": "司法/行政监管",
    "finance": "金融与经营数据",
    "public": "公开舆情",
    "supply": "供应链/关联方监控",
}

_SUPPLY_RE = re.compile(
    r"供应链|关联方|上下游|供应商|客户|合作方|物流|经销|采购",
    re.I,
)
_EXEC_RE = re.compile(
    r"高管|董事长|社長|役員|董事|CEO|CFO|COO|人事|任命|就任|辞任|辞职|退任|解任|更换",
    re.I,
)
_FINANCE_RE = re.compile(
    r"财务|財務|财报|財報|決算|业绩|業績|营收|营業|利润|融資|融资|债务|債務|"
    r"债券|评级|信用|披露|有価証券|分红|配息|减记|减值|亏损",
    re.I,
)
_SHAREHOLDER_RE = re.compile(
    r"股东|株主|持股|持株|增持|减持|股权|株式|大股东|大株主|TOB|收购|要约收购",
    re.I,
)


def classify_risk_tab(category: str | None) -> str:
    text = str(category or "")
    if re.search(r"司法|行政|监管|合规|处罚|诉讼|法律", text):
        return "judicial"
    if re.search(r"金融|经营|财务|信用|披露|财报|评级|決算", text):
        return "finance"
    if re.search(r"供应链|关联|上下游|合作方|物流", text):
        return "supply"
    if re.search(r"舆论|舆情|社交|媒体|品牌|口碑|公开|行业", text):
        return "public"
    return "public"


def canonical_risk_category(category: str | None) -> str:
    text = str(category or "").strip()
    label = RISK_TAB_LABELS.get(classify_risk_tab(text))
    return label or RISK_TAB_LABELS["public"]


def _event_blob(risk: Any) -> str:
    return " ".join(
        str(getattr(risk, key, None) or "")
        for key in (
            "title",
            "summary",
            "impact_analysis",
            "risk_category",
            "related_company",
            "source_name",
            "source_url",
        )
    )


def is_supply_chain_event(risk: Any) -> bool:
    """上下游 / 关联方监控事件。"""
    if classify_risk_tab(getattr(risk, "risk_category", None)) == "supply":
        return True
    return bool(_SUPPLY_RE.search(_event_blob(risk)))


def is_executive_change_event(risk: Any) -> bool:
    return bool(_EXEC_RE.search(_event_blob(risk)))


def is_financial_change_event(risk: Any) -> bool:
    if classify_risk_tab(getattr(risk, "risk_category", None)) == "finance":
        return True
    return bool(_FINANCE_RE.search(_event_blob(risk)))


def is_shareholder_change_event(risk: Any) -> bool:
    return bool(_SHAREHOLDER_RE.search(_event_blob(risk)))


def _subject_blob(risk: Any) -> str:
    """展示主语判定不读 related_company：入库时该字段常被写成目标企业名。"""
    return " ".join(
        str(getattr(risk, key, None) or "")
        for key in ("title", "summary", "impact_analysis", "source_name", "source_url")
    )


def is_monitored_public_event(
    risk: Any,
    *,
    entity: TargetEntity | None = None,
    profile: EntityProfile | None = None,
) -> bool:
    """主体评估「公开信息事件」展示门禁。

    只保留对合作/投资判断有用的公开事件：目标企业、股东/母公司、高管、
    已点名的上下游企业。行业宏观、营销稿、同行新闻不展示。
    contextual 不得仅因含有「供应链」字样留下。
    """
    if (getattr(risk, "provenance", None) or "") == "demo":
        return False
    relevance = str(getattr(risk, "relevance", None) or "unknown").strip().lower()
    if relevance == "unrelated":
        return False

    title = str(getattr(risk, "title", None) or "")
    blob = _subject_blob(risk)
    full_blob = _event_blob(risk)
    names = entity_names(entity, profile) if entity is not None else ()
    parties = related_party_specs(entity, profile)
    related_names = _related_party_names(entity, profile)
    # 旧版 Godiva 采集器曾加入欧盟糖、百货销售等泛行业资料；它们不是已配置的
    # ICCO/COCOBOD 背景，也不属于主体或关联方事件，故不在本面板展示。
    source_host = urlparse(str(getattr(risk, "source_url", None) or "")).netloc.lower()
    if source_host in {"agriculture.ec.europa.eu", "www.depart.or.jp", "depart.or.jp"}:
        return False
    related_event = _named_related_party_event(
        blob,
        entity_names_=names,
        related_names=related_names,
        parties=parties,
        require_target=False,
    )
    exec_event = _is_named_executive_event(blob, profile)
    subject = _entity_is_subject(title, blob, names) if names else False
    industry_shock = _is_industry_shock_for_entity(
        blob, names=names, related_names=related_names, profile=profile
    )
    foreign = bool(names) and _foreign_company_subject(title, names, related_names)

    if _is_marketing_copy(blob) and not _has_material_company_event(blob):
        return False
    if _is_industry_macro(blob) and not related_event and not industry_shock and not subject:
        return False
    if foreign and not related_event and not subject:
        return False

    if _is_official_disclosure(risk, full_blob) and (
        not names
        or subject
        or related_event
        or any(_contains_name_loose(blob, name) for name in names if name)
    ):
        return True
    if industry_shock or exec_event:
        return True
    if related_event:
        return True

    if relevance == "contextual":
        return False

    if names and not subject and foreign:
        return False
    if is_executive_change_event(risk):
        return True
    if is_financial_change_event(risk):
        return True
    if is_shareholder_change_event(risk):
        return True
    if classify_risk_tab(getattr(risk, "risk_category", None)) == "judicial":
        return True
    if _has_material_company_event(blob):
        return True
    return False
