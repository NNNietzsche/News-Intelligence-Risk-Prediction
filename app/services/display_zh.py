"""页面展示用简体中文：库内保留原文，渲染时翻译并缓存。"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from html import unescape
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.services.deepseek_analyzer import DeepSeekAnalyzer
from app.services.llm_cache import get_cached_items, material_hash, set_cached_items
from app.services.api_keys import is_placeholder_key
from app.config import get_settings
from app.services.gemini_analyzer import gemini_for
from app.services.risk_reasoning import build_risk_reasoning
from app.services.news_risk_tags import assess_news_risk

logger = logging.getLogger(__name__)

_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_DEGRADED_ZH = "结构化分析暂不可用"
_EMPTY_DETAIL_RE = re.compile(
    r"^(?:google news|谷歌新闻|正文未取得|暂无正文|暂无内容|"
    r"content unavailable|access denied|sign in|log in)[\s.!。]*$",
    re.IGNORECASE,
)


@dataclass
class NewsDisplayCard:
    """日报卡片展示对象（不回写数据库）。"""

    id: int
    module_code: str
    title: str
    related_company: Optional[str]
    risk_category: Optional[str]
    category_tag: Optional[str]
    risk_level: str
    summary: str
    impact_analysis: Optional[str]
    source_url: Optional[str]
    source_title: Optional[str]
    published_at: Any
    social_source_label: str = "暂无"
    social_source_url: Optional[str] = None
    risk_reasoning: dict[str, str | bool] | None = None
    overview: str = ""
    risk_tags: tuple[str, ...] = ()


def needs_chinese_display(text: Optional[str]) -> bool:
    """判断文本是否需要译成简体中文展示。"""
    t = (text or "").strip()
    if not t:
        return False
    if _DEGRADED_ZH in t:
        return False
    if _KANA_RE.search(t):
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 18:
        latin = sum(1 for c in letters if "a" <= c.lower() <= "z")
        if latin / len(letters) >= 0.72:
            return True
    # 几乎无汉字且含较多非中文符号数字标题——仍交由后续批处理跳过短串
    han = len(_HAN_RE.findall(t))
    if han == 0 and len(t) >= 12 and any(c.isalpha() for c in t):
        return True
    return False


def format_news_overview(
    *,
    title: str | None,
    summary: str | None,
    source_name: str | None,
    source_url: str | None,
    published_at: Any = None,
    subject: str | None = None,
) -> str:
    """输出抓取并入库的完整详情；仅去除与标题重复的开头。"""
    headline = " ".join(unescape(str(title or "")).split()).strip().rstrip("。.")
    detail = " ".join(unescape(str(summary or "")).split()).strip().rstrip("。.")
    if not detail or headline == detail or detail in headline:
        return ""
    # RSS 摘要常以标题开头再接正文；仅删去重复标题，保留其后的事实细节。
    if headline and detail.startswith(headline):
        detail = detail[len(headline):].lstrip("：:，,。.-—– ")
        if len(detail) < 8:
            return ""
    # 详情可完整展示；原始链接仍供用户核验全文。
    return detail.rstrip("。") + "。"


def has_publishable_content_detail(title: str | None, summary: str | None) -> bool:
    """只有取得了有实质的内容详情才允许生成新闻卡片。"""
    def comparable(value: str | None) -> str:
        text = unescape(str(value or "")).lower()
        # Google News 常在标题末尾拼接媒体名；相似度判断时去除该尾缀。
        text = re.split(r"\s+[\-–—]\s+", text, maxsplit=1)[0]
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)

    title_key = comparable(title)
    summary_key = comparable(summary)
    if not summary_key:
        return False
    if title_key:
        similarity = SequenceMatcher(None, title_key, summary_key).ratio()
        shorter = min(len(title_key), len(summary_key))
        longer = max(len(title_key), len(summary_key))
        containment = shorter / longer if (
            title_key in summary_key or summary_key in title_key
        ) else 0.0
        if similarity >= 0.72 or containment >= 0.72:
            return False

    detail = format_news_overview(
        title=title,
        summary=summary,
        source_name=None,
        source_url=None,
    ).strip()
    if not detail or _EMPTY_DETAIL_RE.fullmatch(detail):
        return False
    # 媒体名、中转页提示和一句残缺 snippet 不视为文章内容。
    compact = re.sub(r"\s+", "", detail)
    return len(compact) >= 40


def _translate_batch(items: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """调用 DeepSeek 批量翻译，返回 id → {title,summary,impact}。"""
    if not items:
        return {}
    settings = get_settings()
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    system = (
        "你是金融资讯翻译。将输入 json 数组中每条新闻的 title/summary/impact "
        "译为准确、简洁的简体中文，专有名词可保留原文。"
        "输出必须是合法 JSON 对象（json），格式："
        '{"items":[{"id":"...","title":"...","summary":"...","impact":"..."}]}。'
        "不要编造事实；impact 若为系统提示语（如结构化分析暂不可用）则原样返回中文提示。"
        "不要输出 markdown，只输出 json。"
    )
    user = f"请翻译以下资讯字段为简体中文：\n{payload}"
    providers = []
    if not is_placeholder_key(getattr(settings, "deepseek_api_key", None)):
        providers.append(DeepSeekAnalyzer())
    if not is_placeholder_key(getattr(settings, "gemini_api_key", None)):
        providers.append(gemini_for("fast"))
    for analyzer in providers:
        try:
            # 两个分析器均提供相同的 JSON 对象接口。
            raw = analyzer._request_chat(system, user)  # noqa: SLF001 — 展示翻译内部接口
            text = raw.strip()
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"^```\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            parsed = json.loads(text)
            translated_rows = parsed.get("items") if isinstance(parsed, dict) else parsed
            if not isinstance(translated_rows, list):
                continue
            out: dict[str, dict[str, str]] = {}
            for row in translated_rows:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or "")
                if not rid:
                    continue
                out[rid] = {
                    "title": str(row.get("title") or "").strip(),
                    "summary": str(row.get("summary") or "").strip(),
                    "impact": str(row.get("impact") or "").strip(),
                }
            return out
        except Exception as exc:
            logger.warning("展示翻译失败，尝试下一可用模型: %s", exc)
    return {}


def translate_fields_to_chinese(
    rows: list[dict[str, str]],
    *,
    db: Session | None = None,
    cache_source: str = "display_zh_live",
) -> dict[str, dict[str, str]]:
    """供主体评估实时标题使用；可选缓存避免每次页面刷新重复调用模型。"""
    out: dict[str, dict[str, str]] = {}
    pending: list[dict[str, str]] = []
    cache_keys: dict[str, str] = {}
    for row in rows:
        if not any(needs_chinese_display(row.get(field)) for field in ("title", "summary", "impact")):
            continue
        rid = str(row.get("id") or "")
        if not rid:
            continue
        key = material_hash(
            f"{row.get('title') or ''}\n{row.get('summary') or ''}\n{row.get('impact') or ''}",
            module_code="ZH",
            source=cache_source,
        )
        cache_keys[rid] = key
        cached = get_cached_items(db, material_key=key, max_age_hours=720) if db else None
        if cached and isinstance(cached, list) and isinstance(cached[0] if cached else None, dict):
            out[rid] = {name: str(cached[0].get(name) or "") for name in ("title", "summary", "impact")}
        else:
            pending.append(row)
    translated = _translate_batch(pending[:16])
    for rid, value in translated.items():
        out[rid] = value
        if db and rid in cache_keys:
            set_cached_items(
                db,
                material_key=cache_keys[rid],
                module_code="ZH",
                source=cache_source,
                items=[value],
            )
    return out


def build_display_cards(
    db: Session,
    entries: list[Any],
    *,
    social_resolver,
) -> list[NewsDisplayCard]:
    """为日报条目生成中文展示卡片；库内原文不变。"""
    pending: list[dict[str, str]] = []
    cache_hits: dict[str, dict[str, str]] = {}

    for e in entries:
        eid = str(e.id)
        title = e.title or ""
        summary = e.summary or ""
        impact = e.impact_analysis or ""
        if not (
            needs_chinese_display(title)
            or needs_chinese_display(summary)
            or needs_chinese_display(impact)
        ):
            continue
        key = material_hash(
            f"{title}\n{summary}\n{impact}",
            module_code="ZH",
            source="display_zh",
        )
        cached = get_cached_items(db, material_key=key, max_age_hours=720)
        if cached and isinstance(cached, list) and cached and isinstance(cached[0], dict):
            cache_hits[eid] = {
                "title": str(cached[0].get("title") or title),
                "summary": str(cached[0].get("summary") or summary),
                "impact": str(cached[0].get("impact") or impact),
            }
            continue
        pending.append(
            {
                "id": eid,
                "title": title[:300],
                # 内容详情使用完整抓取正文；翻译后也不把正文缩短成摘要。
                "summary": summary,
                "impact": impact[:500],
                "_cache_key": key,
            }
        )

    # 批量翻译（限制单次条数，避免首屏超时；其余下次访问继续补齐）
    translated: dict[str, dict[str, str]] = dict(cache_hits)
    max_per_request = 12
    pending = pending[:max_per_request]
    chunk_size = 6
    for i in range(0, len(pending), chunk_size):
        chunk = pending[i : i + chunk_size]
        payload = [
            {k: v for k, v in row.items() if not k.startswith("_")} for row in chunk
        ]
        result = _translate_batch(payload)
        for row in chunk:
            eid = row["id"]
            t = result.get(eid)
            if not t:
                continue
            translated[eid] = {
                "title": t.get("title") or row["title"],
                "summary": t.get("summary") or row["summary"],
                "impact": t.get("impact") or row["impact"],
            }
            set_cached_items(
                db,
                material_key=row["_cache_key"],
                module_code="ZH",
                source="display_zh",
                items=[translated[eid]],
            )

    cards: list[NewsDisplayCard] = []
    for e in entries:
        eid = str(e.id)
        tr = translated.get(eid) or {}
        detail_available = has_publishable_content_detail(e.title, e.summary)
        social = social_resolver(e)
        assessment = assess_news_risk(
            title=tr.get("title") or e.title,
            summary=tr.get("summary") or e.summary,
            impact=tr.get("impact") if tr.get("impact") is not None else e.impact_analysis,
            stored_level=e.risk_level,
        )
        cards.append(
            NewsDisplayCard(
                id=e.id,
                module_code=e.module_code,
                title=tr.get("title") or e.title,
                related_company=e.related_company,
                risk_category=e.risk_category,
                category_tag=getattr(e, "category_tag", None),
                risk_level=assessment.level,
                summary=tr.get("summary") or e.summary,
                impact_analysis=tr.get("impact") if tr.get("impact") is not None else e.impact_analysis,
                source_url=e.source_url,
                source_title=getattr(e, "source_title", None),
                published_at=e.published_at,
                social_source_label=social.get("social_source_label") or "暂无",
                social_source_url=social.get("social_source_url"),
                risk_reasoning=build_risk_reasoning(
                    title=tr.get("title") or e.title,
                    summary=tr.get("summary") or e.summary,
                    impact=tr.get("impact") if tr.get("impact") is not None else e.impact_analysis,
                    risk_level=assessment.level,
                    category=assessment.tags[0] if assessment.tags else e.risk_category,
                ),
                overview=format_news_overview(
                    title=tr.get("title") or e.title,
                    summary=(tr.get("summary") or e.summary) if detail_available else "",
                    source_name=getattr(e, "source_title", None),
                    source_url=e.source_url,
                    published_at=e.published_at,
                    subject=e.related_company,
                ),
                risk_tags=assessment.tags,
            )
        )
    return cards
