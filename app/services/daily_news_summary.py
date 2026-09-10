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
    # 每个板块先保留若干事实，避免 B/D 的大量新闻挤掉 C（大型企业）等板块。
    facts = _balanced_facts(rows, modules)
    fallback = _fallback_summary(facts, report_date=report_date, modules=modules)
    if not facts:
        return {**fallback, "source": "template", "entry_count": 0}

    settings = get_settings()
    if is_placeholder_key(getattr(settings, "deepseek_api_key", None)):
        return {**fallback, "source": "template", "entry_count": len(facts)}

    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    # v4：日报改为“判断 + 核心动态”的晨报式结构；旧版长段落缓存不再复用。
    key = material_hash(payload, module_code="DAILY", source=f"daily-summary:{report_date}:v4")
    cached = get_cached_items(db, material_key=key, max_age_hours=24)
    if cached and isinstance(cached[0], dict) and cached[0].get("overview"):
        return {**_normalize_sections(cached[0], modules, facts), "source": "deepseek", "entry_count": len(facts)}
    try:
        result = DeepSeekAnalyzer().summarize_daily_news_sections(facts, report_date)
        normalized = _normalize_sections(result, modules, facts)
        if len(normalized["overview"]) < 40:
            raise ValueError("日报汇总内容过短")
        set_cached_items(
            db,
            material_key=key,
            module_code="DAILY",
            source="daily_news_summary:v4",
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
    risk_count = 0
    watch_count = 0
    for item in facts:
        groups[item["板块"]] = groups.get(item["板块"], 0) + 1
        level = str(item.get("风险等级") or "低")
        if level in {"高", "极高", "风险"}:
            risk_count += 1
        elif level in {"中", "关注"}:
            watch_count += 1
    group_text = "、".join(f"{name} {count} 条" for name, count in groups.items())
    if risk_count:
        signal = f"识别 {risk_count} 条风险事项，应优先核验并跟踪后续影响。"
    elif watch_count:
        signal = f"识别 {watch_count} 条需关注事项，建议持续跟踪。"
    else:
        signal = "暂未识别需升级跟踪的风险事项。"
    return {
        "overview": f"截至 {report_date}，已收录 {len(facts)} 条资讯（{group_text}）。{signal}",
        "sections": sections,
    }


def _fallback_sections(facts: list[dict[str, Any]], modules: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in modules:
        rows = [row for row in facts if row.get("code") == code]
        name = modules.get(code, code)
        if not rows:
            out.append({"code": code, "name": name, "summary": "当日暂无已入库资讯。", "important_points": []})
            continue
        chosen = rows[:3]
        points = [str(row.get("标题") or "") for row in chosen if str(row.get("标题") or "")]
        risk_rows = [row for row in rows if str(row.get("风险等级") or "") in {"高", "极高", "风险"}]
        watch_rows = [row for row in rows if str(row.get("风险等级") or "") in {"中", "关注"}]
        if risk_rows:
            judgement = f"识别 {len(risk_rows)} 条风险事项，优先跟踪相关传导。"
        elif watch_rows:
            judgement = f"识别 {len(watch_rows)} 条需关注事项，持续观察后续披露。"
        else:
            judgement = "当日动态以一般资讯为主，未识别需升级事项。"
        out.append({
            "code": code,
            "name": name,
            "summary": judgement,
            "important_points": points[:3],
        })
    return out


def _normalize_sections(payload: dict[str, Any], modules: dict[str, str], facts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    raw = payload.get("sections") if isinstance(payload, dict) else []
    by_code = {
        str(item.get("code") or "").upper(): item
        for item in raw or []
        if isinstance(item, dict)
    }
    sections: list[dict[str, Any]] = []
    fallback_by_code = {item["code"]: item for item in _fallback_sections(facts or [], modules)}
    for code in modules:
        row = by_code.get(code) or {}
        points = row.get("important_points") or []
        if not isinstance(points, list):
            points = []
        summary = str(row.get("summary") or "").strip()
        # 模型偶尔漏掉某一板块；只要该板块已有事实，就永远不显示“暂无”。
        if not summary or (summary.startswith("当日暂无") and any(item.get("code") == code for item in (facts or []))):
            row = fallback_by_code.get(code, row)
            summary = str(row.get("summary") or "当日暂无已入库资讯。").strip()
            points = row.get("important_points") or []
        sections.append({
            "code": code,
            "name": modules.get(code, code),
            "summary": _compact_text(summary) or "当日暂无已入库资讯。",
            "important_points": [str(point).strip() for point in points if str(point).strip()][:3],
        })
    return {"overview": _compact_text(str(payload.get("overview") or "")), "sections": sections}


def _compact_text(value: str, *, limit: int = 150) -> str:
    """清理模型偶尔输出的冗余空白，避免日报概览变成一整段长说明。"""
    text = " ".join(str(value or "").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    # 优先在句末截断，保证页面呈现的是完整判断而不是半句。
    for marker in ("。", "；"):
        end = text.rfind(marker, 0, limit)
        if end >= max(35, limit // 2):
            return text[: end + 1]
    return text[:limit].rstrip("，、；：") + "。"


def _balanced_facts(rows: list[Any], modules: dict[str, str], *, per_module: int = 6, max_items: int = 24) -> list[dict[str, Any]]:
    """保证每日摘要对每个可见板块都有输入事实。"""
    chosen: list[Any] = []
    seen: set[int] = set()
    for code in modules:
        for item in rows:
            if str(getattr(item, "module_code", "") or "").upper() != code:
                continue
            marker = id(item)
            if marker not in seen:
                chosen.append(item); seen.add(marker)
            if sum(str(getattr(x, "module_code", "") or "").upper() == code for x in chosen) >= per_module:
                break
    for item in rows:
        if len(chosen) >= max_items:
            break
        if id(item) not in seen:
            chosen.append(item); seen.add(id(item))
    return [{
        "code": str(getattr(item, "module_code", "") or "").upper(),
        "板块": modules.get(str(getattr(item, "module_code", "") or "").upper(), "其他"),
        "标题": str(getattr(item, "title", "") or "")[:180],
        "内容": str(getattr(item, "overview", None) or getattr(item, "summary", "") or "")[:500],
        "风险类型": list(getattr(item, "risk_tags", None) or []),
        "风险等级": str(getattr(item, "risk_level", "低") or "低"),
    } for item in chosen]
