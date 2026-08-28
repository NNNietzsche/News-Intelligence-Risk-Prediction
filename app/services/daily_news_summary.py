"""新闻汇总页日报摘要：基于已入库条目生成并缓存，不额外抓取外网。"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from app.config import get_settings
from app.services.api_keys import is_placeholder_key
from app.services.deepseek_analyzer import DeepSeekAnalyzer
from app.services.llm_cache import get_cached_items, material_hash, set_cached_items

logger = logging.getLogger(__name__)


def build_daily_news_summary(
    entries: Iterable[Any], *, report_date: str, modules: dict[str, str], db: Any
) -> dict[str, Any]:
    """返回 AI 汇总；无密钥/失败时仍提供可追溯的规则化汇总。"""
    rows = list(entries)
    facts = [
        {
            "code": str(getattr(item, "module_code", "") or ""),
            "板块": modules.get(str(getattr(item, "module_code", "") or ""), "其他"),
            "标题": str(getattr(item, "title", "") or "")[:180],
            "内容": str(getattr(item, "overview", None) or getattr(item, "summary", "") or "")[:500],
            "风险类型": list(getattr(item, "risk_tags", None) or []),
            "风险等级": str(getattr(item, "risk_level", "低") or "低"),
        }
        for item in rows[:18]
    ]
    fallback = _fallback_summary(facts, report_date=report_date, modules=modules)
    if not facts:
        return {**fallback, "source": "template", "entry_count": 0}

    settings = get_settings()
    if is_placeholder_key(getattr(settings, "deepseek_api_key", None)):
        return {**fallback, "source": "template", "entry_count": len(facts)}

    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    key = material_hash(payload, module_code="DAILY", source=f"daily-summary:{report_date}:v2")
    cached = get_cached_items(db, material_key=key, max_age_hours=24)
    if cached and isinstance(cached[0], dict) and cached[0].get("overview"):
        return {**_normalize_sections(cached[0], modules), "source": "deepseek", "entry_count": len(facts)}
    try:
        result = DeepSeekAnalyzer().summarize_daily_news_sections(facts, report_date)
        normalized = _normalize_sections(result, modules)
        if len(normalized["overview"]) < 40:
            raise ValueError("日报汇总内容过短")
        set_cached_items(
            db,
            material_key=key,
            module_code="DAILY",
            source="daily_news_summary:v2",
            items=[normalized],
        )
        return {**normalized, "source": "deepseek", "entry_count": len(facts)}
    except Exception as exc:
        logger.warning("新闻日报 AI 汇总失败，使用规则化摘要：%s", exc)
        return {**fallback, "source": "template", "entry_count": len(facts)}


def _fallback_summary(
    facts: list[dict[str, Any]], *, report_date: str, modules: dict[str, str]
) -> dict[str, Any]:
    sections = _fallback_sections(facts, modules)
    if not facts:
        return {
            "overview": f"截至 {report_date}，当日暂无已入库新闻，待刷新资讯后生成汇总报告。",
            "sections": sections,
        }
    groups: dict[str, int] = {}
    high = 0
    tags: list[str] = []
    for item in facts:
        groups[item["板块"]] = groups.get(item["板块"], 0) + 1
        high += 1 if item["风险等级"] in {"高", "极高"} else 0
        tags.extend(item["风险类型"])
    group_text = "、".join(f"{name} {count} 条" for name, count in groups.items())
    tag_text = "、".join(dict.fromkeys(tags)) or "未形成明确风险标签"
    signal = "存在高等级信号，应优先核验原始来源与后续公告。" if high else "暂未出现高等级信号，仍应关注后续披露与市场变化。"
    return {
        "overview": f"截至 {report_date}，本次汇总已收录 {len(facts)} 条资讯（{group_text}）。重点涉及{tag_text}。{signal}",
        "sections": sections,
    }


def _fallback_sections(facts: list[dict[str, Any]], modules: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in ("B", "C", "D"):
        rows = [row for row in facts if row.get("code") == code]
        name = modules.get(code, code)
        if not rows:
            out.append({"code": code, "name": name, "summary": "当日暂无已入库资讯。", "important_points": []})
            continue
        chosen = rows[:2]
        detail = "；".join(str(row.get("内容") or row.get("标题") or "")[:140] for row in chosen)
        points = [str(row.get("标题") or "") for row in chosen if str(row.get("标题") or "")]
        out.append({
            "code": code,
            "name": name,
            "summary": f"本板块收录 {len(rows)} 条资讯。{detail}",
            "important_points": points[:3],
        })
    return out


def _normalize_sections(payload: dict[str, Any], modules: dict[str, str]) -> dict[str, Any]:
    raw = payload.get("sections") if isinstance(payload, dict) else []
    by_code = {
        str(item.get("code") or "").upper(): item
        for item in raw or []
        if isinstance(item, dict)
    }
    sections: list[dict[str, Any]] = []
    for code in ("B", "C", "D"):
        row = by_code.get(code) or {}
        points = row.get("important_points") or []
        if not isinstance(points, list):
            points = []
        sections.append({
            "code": code,
            "name": modules.get(code, code),
            "summary": str(row.get("summary") or "当日暂无已入库资讯。").strip(),
            "important_points": [str(point).strip() for point in points if str(point).strip()][:3],
        })
    return {"overview": str(payload.get("overview") or "").strip(), "sections": sections}
