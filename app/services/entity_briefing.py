"""主体评估「最新消息」：近三个月核心风险摘要。

优先使用主体配置的直连 RSS/JSON；无原生 feed 时，用已有 query 生成 Google News RSS。
摘要使用 DeepSeek，失败时回退模板句。采集补缺仍走秘塔等检索来源。
本板块只写近三个月核心风险分析；若无，则写同期情况概括。
具体相关新闻列在公开信息事件中。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.config import RISK_LEVELS, get_settings
from app.database.models import EntityRisk
from app.services.api_keys import is_placeholder_key
from app.services.entity_catalog import (
    DEFAULT_FINANCIAL_SOURCE_LABEL,
    DEFAULT_FINANCIAL_UNIT,
    FINANCIAL_STATEMENT_COLUMNS,
    FINANCIAL_STATEMENT_KEYS,
    FINANCIAL_STATEMENT_TITLES,
    EntityProfile,
    EntitySourceSpec,
)
from app.services.entity_briefing_feed import (
    BriefingHeadline,
    channels_from_sources,
    fetch_briefing_headlines,
    resolve_briefing_channels,
)
from app.services.entity_financial_pdf import (
    financial_payload_from_resolved,
    load_pdf_statements,
    resolve_latest_financial_pdf,
)
from app.services.entity_kabutan import (
    format_finance_period,
    format_finance_release_date,
    kabutan_finance_url_for_code,
    load_kabutan_statements,
    period_sort_key,
)
from app.services.deepseek_analyzer import DeepSeekAnalyzer
from app.services.llm_cache import get_cached_items, material_hash, set_cached_items
from app.services.mita_search import MitaSearchClient
from app.services.risk_reasoning import build_risk_reasoning
from app.services.display_zh import format_news_overview, translate_fields_to_chinese
from app.services.news_quality import is_substantive_news_item
from app.services.news_risk_tags import normalize_display_risk_level

logger = logging.getLogger(__name__)
TOKYO = ZoneInfo("Asia/Tokyo")

_SUMMARY_SYSTEM = (
    "你是银行授信风险分析助手。根据近三个月的核心事件与公开资讯标题，"
    "用中文撰写一份300至500字的‘近三个月汇总报告’。"
    "用2至3个自然段依次说明主要动态、可能的经营或信用传导、整体判断与后续关注点。"
    "只根据输入事实分析，不要编造；不要逐条复述新闻标题，也不要使用项目符号。"
)
_OVERVIEW_SYSTEM = (
    "你是银行授信风险分析助手。近三个月没有重大风险。"
    "根据观察期内的公开资讯标题与一般事件，用中文撰写一份300至500字的‘近三个月汇总报告’。"
    "先说明整体风险态势，再概括主要经营、监管、市场或供应链动态及其潜在传导，并提出后续关注点。"
    "只根据输入事实分析，不要编造；不要逐条复述新闻标题，也不要使用项目符号。"
)
_summary_cache: dict[str, tuple[float, str]] = {}
_SUMMARY_CACHE_TTL = 3600
_SUMMARY_PERSIST_HOURS = 24 * 7


CORE_RISK_LEVELS = {"高", "极高"}
CORE_CREDIT_IMPACTS = {"medium", "high", "critical"}
CORE_IMPORTANCE = {"高", "极高"}
MAX_HIGHLIGHTS = 6
MAX_SEARCH_FALLBACK = 8
_RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}


def news_lookback_days() -> int:
    return max(1, int(getattr(get_settings(), "entity_warning_lookback_days", 90) or 90))


def news_lookback_start(report_date: date, days: int | None = None) -> date:
    window = news_lookback_days() if days is None else max(1, int(days))
    return report_date - timedelta(days=window)


def _in_lookback_window(
    value: date | datetime | None,
    report_date: date,
    days: int,
) -> bool:
    if value is None:
        return False
    day = value.date() if isinstance(value, datetime) else value
    start = news_lookback_start(report_date, days)
    return start <= day <= report_date


def _search_fallback_headlines(
    profile: EntityProfile | None,
    *,
    report_date: date,
) -> list[BriefingHeadline]:
    """直连 RSS 为空时，用秘塔及已配置的检索回退补公开标题。"""
    if profile is None:
        return []
    names = [str(n).strip() for n in profile.all_names if str(n).strip()][:3]
    if not names:
        names = [str(profile.display_name or profile.key or "").strip()]
    names = [n for n in names if n]
    if not names:
        return []
    quoted = " OR ".join(f'"{n}"' if " " in n else n for n in names)
    start = news_lookback_start(report_date)
    end_exclusive = report_date + timedelta(days=1)
    query = (
        f"({quoted}) (news OR 新闻 OR 公告 OR IR OR results OR 融资 OR 处罚 OR 风险) "
        f"after:{start.isoformat()} before:{end_exclusive.isoformat()}"
    )
    items: list[Any] = []
    provider = "mita"
    try:
        response = MitaSearchClient().search(query, max_results=MAX_SEARCH_FALLBACK)
        provider = getattr(response, "provider", None) or "mita"
        items = list(response.items or [])
    except Exception as exc:
        logger.warning("最新消息秘塔补缺失败，改试备用检索: %s", exc)
        try:
            from app.services.llm_web_search import search_web_fallback

            rows, provider = search_web_fallback(query, max_results=MAX_SEARCH_FALLBACK)
            items = rows
        except Exception as exc2:
            logger.warning("最新消息检索补缺失败: %s", exc2)
            return []

    label = f"检索补缺·{provider}"
    out: list[BriefingHeadline] = []
    for item in items[:MAX_SEARCH_FALLBACK]:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("snippet") or "")[:400]
            published = item.get("published_at")
            published_at = str(published).strip() if published else None
        else:
            title = (getattr(item, "title", None) or "").strip()
            url = (getattr(item, "url", None) or "").strip()
            snippet = (getattr(item, "snippet", None) or "")[:400]
            published_at = getattr(item, "published_at", None)
        if not title:
            continue
        if not is_substantive_news_item({"title": title, "snippet": snippet, "url": url}):
            continue
        out.append(
            BriefingHeadline(
                title=title,
                url=url,
                snippet=snippet,
                published_at=published_at,
                feed_label=label,
            )
        )
    if out:
        logger.info("最新消息检索补缺成功: provider=%s items=%s", provider, len(out))
    return out


def _headlines_from_recent_risks(
    risks: Iterable[EntityRisk],
    *,
    report_date: date,
    lookback_days: int,
    limit: int = 6,
) -> list[BriefingHeadline]:
    """外网全空时，用库内近三个月主体事件补分析材料，避免面板长期空白。"""
    from app.services.entity_relevance import is_monitored_public_event

    rows = [
        risk
        for risk in risks
        if (risk.provenance or "") != "demo"
        and str(risk.title or "").strip()
        and _in_lookback_window(risk.report_date, report_date, lookback_days)
        and is_monitored_public_event(risk)
    ]
    rows.sort(
        key=lambda risk: (
            risk.report_date or date.min,
            risk.id or 0,
        ),
        reverse=True,
    )
    out: list[BriefingHeadline] = []
    seen: set[str] = set()
    for risk in rows:
        title = str(getattr(risk, "display_title", None) or risk.title or "").strip()
        key = title.casefold()
        if not title or key in seen:
            continue
        seen.add(key)
        day = risk.report_date.isoformat() if risk.report_date else ""
        label = f"库内事件·{day}" if day else "库内近三个月事件"
        out.append(
            BriefingHeadline(
                title=title,
                url=(risk.source_url or "").strip(),
                snippet=(risk.summary or "")[:400],
                published_at=day or None,
                feed_label=label,
            )
        )
        if len(out) >= limit:
            break
    return out


def _template_overview_recent(*, event_count: int, lookback_note: str) -> str:
    if event_count:
        return (
            f"近三个月暂未见重大负面风险。"
            f"已汇总{lookback_note}公开事件 {event_count} 条，具体新闻见下方公开信息事件。"
        )
    return "近三个月未见重大负面风险，也暂无可用的公开资讯标题。"


def is_core_briefing_event(risk: EntityRisk) -> bool:
    if (risk.provenance or "") == "demo":
        return False
    if (risk.relevance or "") == "contextual":
        return False
    impact = (risk.credit_impact or "none").lower()
    if impact in CORE_CREDIT_IMPACTS:
        return True
    if (risk.risk_level or "") in CORE_RISK_LEVELS:
        return True
    if (risk.news_importance or "") in CORE_IMPORTANCE and impact not in {"", "none"}:
        return True
    return False


def _template_summary(highlights: list[dict[str, Any]]) -> str:
    return (
        f"近三个月监测到 {len(highlights)} 条核心风险事件。"
        "具体相关新闻见下方公开信息事件。"
    )


def _template_overview(*, event_count: int, headline_count: int) -> str:
    if event_count or headline_count:
        return (
            "近三个月未见重大负面风险。"
            f"公开资讯与一般事件共 {event_count + headline_count} 条，具体新闻见下方公开信息事件。"
        )
    return "近三个月未见重大负面风险，也暂无可用的公开资讯标题。"


def summarize_latest_news(
    *,
    entity_name: str,
    report_date: date,
    highlights: list[dict[str, Any]],
    headlines: list[BriefingHeadline],
    fallback: str,
    mode: str = "core",
    overview_titles: list[str] | None = None,
    live: bool = True,
    lookback_start: date | None = None,
    db: Any | None = None,
) -> tuple[str, str | None, str]:
    """返回 (摘要, 生成时间, 来源 deepseek|template)。"""
    # Modified by DingJiaye: 2026-08-26 — 首屏只展示缓存/模板，不能为等待
    # 大模型摘要而阻塞主体索引、日期筛选或页面其他交互。
    if not live:
        return fallback, None, "template"
    settings = get_settings()
    deepseek_ready = not is_placeholder_key(getattr(settings, "deepseek_api_key", None))
    if not deepseek_ready:
        return fallback, None, "template"

    extra = overview_titles or []
    window_start = lookback_start or news_lookback_start(report_date)
    cache_key = "|".join(
        [
            mode,
            entity_name,
            window_start.isoformat(),
            report_date.isoformat(),
            ",".join(item.get("title") or "" for item in highlights),
            ",".join(item.title for item in headlines[:12]),
            ",".join(extra[:12]),
        ]
    )
    now = time.time()
    cached = _summary_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1], datetime.now(TOKYO).isoformat(timespec="seconds"), "deepseek"

    # 进程内缓存会在 Render 重启后清空；把同一份报告写入 SQLite，
    # 让再次登录、刷新页面或重启服务都不需要重复调用 DeepSeek。
    persistent_key = material_hash(
        cache_key,
        module_code="A",
        source="entity_latest_summary:v2",
    )
    if db is not None:
        saved = get_cached_items(
            db,
            material_key=persistent_key,
            max_age_hours=_SUMMARY_PERSIST_HOURS,
        )
        if saved and isinstance(saved[0], dict):
            cached_text = str(saved[0].get("summary") or "").strip()
            if cached_text:
                generated_at = str(saved[0].get("generated_at") or "").strip() or None
                _summary_cache[cache_key] = (now + _SUMMARY_CACHE_TTL, cached_text)
                return cached_text, generated_at, "deepseek"

    event_lines = "\n".join(f"- {item.get('title')}" for item in highlights) or "无"
    news_lines = "\n".join(f"- {item.title}" for item in headlines[:12]) or "无"
    overview_lines = "\n".join(f"- {title}" for title in extra[:12]) or "无"
    window_line = f"观察期: {window_start.isoformat()} 至 {report_date.isoformat()}\n"
    if mode == "overview":
        user_content = (
            f"主体: {entity_name}\n"
            f"{window_line}"
            f"近三个月一般事件:\n{overview_lines}\n"
            f"直连公开资讯标题:\n{news_lines}\n"
        )
        system_prompt = _OVERVIEW_SYSTEM
    else:
        user_content = (
            f"主体: {entity_name}\n"
            f"{window_line}"
            f"近三个月核心事件:\n{event_lines}\n"
            f"直连公开资讯标题:\n{news_lines}\n"
        )
        system_prompt = _SUMMARY_SYSTEM
    try:
        analyzer = DeepSeekAnalyzer(timeout=12)
        analyzer.retry_attempts = 1
        text = (analyzer.generate_text(system_prompt, user_content) or "").strip()
        # 保留模型生成的段落结构，页面以汇总报告而非单句摘要呈现。
        text = "\n\n".join(
            " ".join(line.split()) for line in text.splitlines() if line.strip()
        )
        if not text:
            return fallback, None, "template"
        if len(text) > 600:
            text = text[:600].rstrip("，,。.;； ") + "。"
        generated_at = datetime.now(TOKYO).isoformat(timespec="seconds")
        _summary_cache[cache_key] = (now + _SUMMARY_CACHE_TTL, text)
        if db is not None:
            set_cached_items(
                db,
                material_key=persistent_key,
                module_code="A",
                source="entity_latest_summary:v2",
                items=[{"summary": text, "generated_at": generated_at}],
            )
        return text, generated_at, "deepseek"
    except Exception as exc:
        logger.warning("最新消息 AI 摘要失败: %s", exc)
        return fallback, None, "template"


def build_latest_news(
    *,
    risks: Iterable[EntityRisk],
    report_date: date,
    briefing_sources: Iterable[EntitySourceSpec] = (),
    profile: EntityProfile | None = None,
    live: bool = True,
    recent_risks: Iterable[EntityRisk] | None = None,
    lookback_days: int | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    lookback_days = news_lookback_days() if lookback_days is None else max(1, int(lookback_days))
    window_start = news_lookback_start(report_date, lookback_days)
    highlights: list[dict[str, Any]] = []

    core_events = [
        risk
        for risk in risks
        if is_core_briefing_event(risk)
        and _in_lookback_window(risk.report_date, report_date, lookback_days)
    ]
    core_events.sort(
        key=lambda risk: (
            _RISK_RANK.get(risk.risk_level or "", -1),
            risk.id or 0,
        ),
        reverse=True,
    )
    for risk in core_events:
        if len(highlights) >= MAX_HIGHLIGHTS:
            break
        highlights.append(
            {
                "kind": "risk",
                "title": getattr(risk, "display_title", None) or risk.title,
                "risk_level": risk.risk_level,
            }
        )

    channels = (
        resolve_briefing_channels(profile)
        if profile is not None
        else channels_from_sources(briefing_sources)
    )
    direct_sources = [ch.as_dict() for ch in channels]

    headlines: list[BriefingHeadline] = []
    search_fallback_used = False
    db_fallback_used = False
    if channels:
        try:
            headlines = fetch_briefing_headlines(
                channels,
                report_date=report_date,
                profile=profile,
                live=live,
                lookback_days=lookback_days,
            )
        except Exception as exc:
            logger.warning("最新消息直连采集失败: %s", exc)
    if live and not headlines and profile is not None:
        headlines = _search_fallback_headlines(profile, report_date=report_date)
        search_fallback_used = bool(headlines)

    risk_list = [
        risk
        for risk in risks
        if _in_lookback_window(risk.report_date, report_date, lookback_days)
    ]
    recent_list = list(recent_risks) if recent_risks is not None else risk_list
    if not headlines:
        db_heads = _headlines_from_recent_risks(
            recent_list,
            report_date=report_date,
            lookback_days=lookback_days,
        )
        if db_heads:
            headlines = db_heads
            db_fallback_used = True

    def _headline_tab(item: BriefingHeadline) -> tuple[str, str]:
        label = (item.feed_label or "").casefold()
        if "fda" in label or "监管" in label:
            return "judicial", "司法/行政监管"
        if "sgx" in label or "交易所" in label or "ir" in label or "财务" in label:
            return "finance", "金融与经营数据"
        if any(name in label for name in ("yıldız", "yildiz", "母公司", "icco", "cocobod")):
            return "supply", "供应链/关联方监控"
        return "public", "公开舆情"

    headline_payload = []
    for item in headlines:
        if not is_substantive_news_item({"title": item.title, "snippet": item.snippet, "url": item.url}):
            continue
        tab, category = _headline_tab(item)
        headline_payload.append({
            "title": item.title, "url": item.url, "feed_label": item.feed_label,
            "published_at": item.published_at, "snippet": item.snippet,
            "risk_tab": tab, "risk_category": category,
            # 直连/检索补缺的即时资讯没有数据库风险等级；先以低风险作为
            # 基线，再按标题与正文中可核验的融资、经营等事实重新判定。
            # Modified by DingJiaye: 2026-08-28.
            "risk_level": normalize_display_risk_level(
                title=item.title,
                summary=item.snippet,
                impact="",
                level="低",
            ),
            "risk_reasoning": build_risk_reasoning(
                title=item.title,
                summary=item.snippet,
                risk_level=normalize_display_risk_level(
                    title=item.title,
                    summary=item.snippet,
                    impact="",
                    level="低",
                ),
                category=category,
            ),
        })

    translations = translate_fields_to_chinese([
        {"id": str(index), "title": item["title"], "summary": item["snippet"], "impact": ""}
        for index, item in enumerate(headline_payload)
    ])
    for index, item in enumerate(headline_payload):
        translated = translations.get(str(index), {})
        item["title"] = translated.get("title") or item["title"]
        item["snippet"] = translated.get("summary") or item["snippet"]
        # 翻译后再次判定，确保中文标题中的“贷款展期/销售不佳”等信号
        # 与页面灯号保持一致。
        item["risk_level"] = normalize_display_risk_level(
            title=item["title"],
            summary=item["snippet"],
            impact="",
            level=item.get("risk_level"),
        )
        item["overview"] = format_news_overview(
            title=item["title"],
            summary=item["snippet"],
            source_name=item["feed_label"],
            source_url=item["url"],
            published_at=item["published_at"],
        )
        item["risk_reasoning"] = build_risk_reasoning(
            title=item["title"],
            summary=item["snippet"],
            risk_level=item["risk_level"],
            category=item["risk_category"],
        )

    generated_at = None
    summary_source = "none"
    mode = "core" if highlights else "overview"
    overview_titles = [
        str(getattr(risk, "display_title", None) or risk.title or "").strip()
        for risk in risk_list
        if str(getattr(risk, "display_title", None) or risk.title or "").strip()
        and (risk.provenance or "") != "demo"
    ][:12]
    if not overview_titles and db_fallback_used:
        overview_titles = [item.title for item in headlines[:12]]

    if highlights:
        status = "ready"
        summary = _template_summary(highlights)
        summary_source = "template"
        if profile is not None:
            entity_name = profile.display_name or profile.key
            text, generated_at, summary_source = summarize_latest_news(
                entity_name=entity_name,
                report_date=report_date,
                highlights=highlights,
                headlines=headlines,
                fallback=summary,
                mode="core",
                live=live,
                lookback_start=window_start,
                db=db,
            )
            summary = text
    elif headlines or overview_titles:
        status = "ready"
        if db_fallback_used and not overview_titles:
            summary = _template_overview_recent(
                event_count=len(headlines),
                lookback_note="近三个月",
            )
        elif db_fallback_used:
            summary = _template_overview_recent(
                event_count=len(headlines),
                lookback_note="库内近三个月",
            )
        else:
            summary = _template_overview(
                event_count=len(overview_titles),
                headline_count=len(headlines),
            )
        summary_source = "template"
        entity_name = (profile.display_name or profile.key) if profile is not None else "当前主体"
        text, generated_at, summary_source = summarize_latest_news(
            entity_name=entity_name,
            report_date=report_date,
            highlights=[],
            headlines=headlines,
            fallback=summary,
            mode="overview",
            overview_titles=overview_titles,
            live=live,
            lookback_start=window_start,
            db=db,
        )
        summary = text
    else:
        status = "empty"
        summary = None

    reasoning_title = "；".join(item["title"] for item in highlights[:2]) or (
        headline_payload[0]["title"] if headline_payload else "近三个月公开信息"
    )
    max_level = next(
        (item.get("risk_level") for item in highlights if item.get("risk_level") in _RISK_RANK),
        "低",
    )
    return {
        "status": status,
        "mode": mode,
        "summary": summary,
        "risk_reasoning": build_risk_reasoning(
            title=reasoning_title,
            summary=summary,
            risk_level=max_level,
            category="公开舆情",
        ) if summary else None,
        "highlights": highlights,
        "generated_at": generated_at,
        "summary_source": summary_source,
        "source_count": len(highlights),
        "direct_source_count": len(direct_sources),
        "feed_item_count": len(headlines),
        "headlines": headline_payload,
        "lookback_days": lookback_days,
        "lookback_start": window_start.isoformat(),
        "uses_llm_search": True,
        "search_fallback_used": search_fallback_used,
        "db_fallback_used": db_fallback_used,
        "direct_pending": not direct_sources,
        "direct_sources": direct_sources,
    }


def _display_finance_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("period"):
            item["period"] = format_finance_period(item.get("period")) or item["period"]
        if "released_at" in item:
            item["released_at"] = format_finance_release_date(item.get("released_at"))
        out.append(item)
    out.sort(key=lambda row: period_sort_key(str(row.get("period") or "")), reverse=True)
    return out


def _financial_source_copy(
    profile: EntityProfile | None,
    *,
    kabutan_ok: bool,
    pdf_rows_used: bool,
) -> tuple[str, str]:
    listed = bool(profile and kabutan_finance_url(profile) and not profile.prefer_financial_pdf)
    if kabutan_ok or (listed and not pdf_rows_used):
        return DEFAULT_FINANCIAL_SOURCE_LABEL, DEFAULT_FINANCIAL_UNIT
    label = (
        (profile.financial_source_label if profile else None)
        or ("当期报告 PDF（已核对原文摘录）" if pdf_rows_used else DEFAULT_FINANCIAL_SOURCE_LABEL)
    )
    unit = (profile.financial_unit if profile else None) or DEFAULT_FINANCIAL_UNIT
    return label, unit


def kabutan_finance_url(profile: EntityProfile | None) -> str | None:
    if profile is None:
        return None
    return kabutan_finance_url_for_code(profile.stock_code)


def _financial_source_page(
    profile: EntityProfile | None,
    *,
    latest_pdf_url: str | None = None,
) -> str | None:
    # 非上市主体采用指定的官方 IR 口径时，不能因关联证券代码错误跳转到株探。
    if profile and profile.prefer_financial_pdf and profile.financial_source_page:
        return profile.financial_source_page
    kabutan = kabutan_finance_url(profile)
    if kabutan:
        return kabutan
    if latest_pdf_url:
        return latest_pdf_url
    if profile and profile.financial_source_page:
        return profile.financial_source_page
    if profile:
        for src in profile.financial_sources:
            if src.enabled and src.url:
                return src.url
    return None


def build_financials_panel(profile: EntityProfile | None, *, live: bool = True) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in FINANCIAL_STATEMENT_KEYS}
    if profile:
        for src in profile.financial_sources:
            if src.enabled and src.statement in grouped:
                grouped[src.statement].append(src.as_dict())
    # 配置要求使用官方财报时，证券代码只用于主体识别，不能改变财务口径。
    uses_kabutan = bool(kabutan_finance_url(profile)) and not bool(
        profile and profile.prefer_financial_pdf
    )
    resolved = resolve_latest_financial_pdf(profile, live=live)
    latest = financial_payload_from_resolved(resolved)
    source_page_url = _financial_source_page(
        profile,
        latest_pdf_url=None if uses_kabutan else latest.get("latest_pdf_url"),
    )
    scraped = load_kabutan_statements(
        profile.stock_code if uses_kabutan and profile else None,
        live=live,
    )
    pdf_url = None
    if not scraped.ok:
        candidate = latest.get("latest_pdf_url")
        if candidate and re.search(r"\.pdf(?:$|[?#])", str(candidate), re.I):
            pdf_url = candidate
    pdf_parsed = load_pdf_statements(pdf_url, live=live)
    used_pdf_rows = bool(pdf_parsed.ok) and not scraped.ok
    statements = [
        {
            "key": key,
            "title": FINANCIAL_STATEMENT_TITLES[key],
            "columns": [
                {"key": col_key, "label": label, "align": align}
                for col_key, label, align in FINANCIAL_STATEMENT_COLUMNS[key]
            ],
            "rows": _display_finance_rows(
                scraped.statements.get(key) or pdf_parsed.statements.get(key) or []
            ),
            "sources": grouped[key],
        }
        for key in FINANCIAL_STATEMENT_KEYS
    ]
    has_rows = any(item["rows"] for item in statements)
    has_sources = (
        has_rows
        or any(item["sources"] for item in statements)
        or bool(source_page_url)
        or bool(latest["latest_pdf_url"])
    )
    source_label, unit = _financial_source_copy(
        profile,
        kabutan_ok=bool(scraped.ok),
        pdf_rows_used=used_pdf_rows,
    )
    # 修改记录：2026-08-25 | DingJiaye
    # 非上市主体优先展示官方披露的“本次 vs 上次”可比指标，不将关联集团或 J-REIT
    # 的数据伪装成被监测品牌/集团自己的三张财务报表。
    context_metrics = [
        _with_financial_comparison(item.as_dict())
        for item in (profile.financial_context_metrics if profile else ())
    ]
    chart_metrics = [
        item
        for item in context_metrics
        if item.get("current_numeric") is not None and item.get("previous_numeric") is not None
    ]
    return {
        "status": "linked" if has_sources else "pending",
        "period_label": "通期",
        "unit": unit,
        "source_label": source_label,
        "source_page_url": source_page_url,
        **latest,
        **scraped.as_meta(),
        **pdf_parsed.as_meta(),
        "pdf_rows_used": used_pdf_rows,
        "uses_kabutan_page": uses_kabutan,
        "is_alternative_context": bool(context_metrics),
        "context_metrics": context_metrics,
        "context_notice": (
            profile.financial_context_notice if profile and profile.financial_context_notice else None
        ),
        "comparison_title": (
            profile.financial_comparison_title if profile and profile.financial_comparison_title else None
        ),
        "comparison_summary": (
            profile.financial_comparison_summary if profile and profile.financial_comparison_summary else None
        ),
        "chart_metrics": chart_metrics,
        "statements": statements,
    }


def _with_financial_comparison(metric: dict[str, Any]) -> dict[str, Any]:
    """补齐同口径披露间的变动文本；只对明确提供的数值计算。"""
    current = metric.get("current_numeric")
    previous = metric.get("previous_numeric")
    if current is None or previous is None:
        metric["change_display"] = None
        metric["change_direction"] = None
        return metric
    try:
        delta = float(current) - float(previous)
        pct = (delta / abs(float(previous)) * 100) if float(previous) else None
    except (TypeError, ValueError, ZeroDivisionError):
        metric["change_display"] = None
        metric["change_direction"] = None
        return metric
    sign = "+" if delta > 0 else ""
    if pct is None:
        display = f"较上次 {sign}{delta:,.1f}"
    else:
        display = f"较上次 {sign}{pct:.1f}%"
    metric["change_display"] = display
    metric["change_direction"] = "up" if delta > 0 else "down" if delta < 0 else "flat"
    return metric
