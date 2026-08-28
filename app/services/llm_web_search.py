"""秘塔余额不足或直连失败时，用 Gemini 联网检索或 DeepSeek 结构化检索替代搜索。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.config import get_settings, resolve_gemini_model
from app.services.api_keys import is_placeholder_key
from app.services.http_client import get_http_client
from app.services.http_retry import with_retries

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.S)


def search_web_fallback(
    query: str,
    *,
    max_results: int = 10,
    whitelist_domains: Optional[list[str]] = None,
) -> tuple[list[dict[str, Any]], str]:
    """依次尝试 Gemini（Google 搜索接地）、DeepSeek。返回 (条目, 提供者)。"""
    settings = get_settings()
    errors: list[str] = []
    effective = _build_query(query, whitelist_domains)
    limit = max(1, min(int(max_results or 10), 20))

    if not is_placeholder_key(getattr(settings, "gemini_api_key", None)):
        try:
            items = _search_gemini(effective, limit=limit, settings=settings)
            if items:
                logger.info("搜索替代成功: Gemini %s 条 q=%s", len(items), effective[:120])
                return items[:limit], "gemini"
            errors.append("Gemini 未返回可用检索结果")
        except Exception as exc:
            logger.warning("Gemini 搜索替代失败: %s", exc)
            errors.append(f"Gemini: {exc}")

    if not is_placeholder_key(getattr(settings, "deepseek_api_key", None)):
        try:
            items = _search_deepseek(effective, limit=limit, settings=settings)
            if items:
                logger.info("搜索替代成功: DeepSeek %s 条 q=%s", len(items), effective[:120])
                return items[:limit], "deepseek"
            errors.append("DeepSeek 未返回可用检索结果")
        except Exception as exc:
            logger.warning("DeepSeek 搜索替代失败: %s", exc)
            errors.append(f"DeepSeek: {exc}")

    detail = "；".join(errors) if errors else "未配置 Gemini 或 DeepSeek"
    raise RuntimeError(f"搜索替代失败: {detail}")


def _build_query(query: str, whitelist_domains: Optional[list[str]]) -> str:
    wl = [d.strip() for d in (whitelist_domains or []) if d and d.strip()]
    if not wl or len(wl) > 5:
        return query
    site_part = " OR ".join(f"site:{d}" for d in wl)
    return f"{query} ({site_part})"


def _search_gemini(query: str, *, limit: int, settings: Any) -> list[dict[str, Any]]:
    url = f"{str(settings.gemini_api_base_url).rstrip('/')}/v1beta/models/{resolve_gemini_model('search', settings)}:generateContent"
    prompt = (
        "请检索与下列查询相关的近期公开新闻或网页，优先近 24 小时内的结果。\n"
        f"查询：{query}\n\n"
        "只根据真实检索结果作答。不要编造链接。\n"
        "在正文中附一段 JSON，格式为 "
        '{"items":[{"title":"","url":"","snippet":"","published_at":"","source_domain":""}]}\n'
        f"最多 {limit} 条。"
    )
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
    }

    last_error: Exception | None = None
    for tools in ([{"googleSearch": {}}], [{"google_search": {}}]):
        body["tools"] = tools

        def _do() -> dict[str, Any]:
            client = get_http_client()
            resp = client.post(
                url,
                params={"key": settings.gemini_api_key},
                json=body,
                timeout=getattr(settings, "request_timeout_seconds", 60),
            )
            resp.raise_for_status()
            return resp.json()

        try:
            data = with_retries(_do, attempts=2, backoff_seconds=1.2, label="Gemini搜索替代")
            return _parse_gemini_search(data, limit=limit)
        except httpx.HTTPStatusError as exc:
            body_text = (exc.response.text or "")[:300]
            last_error = RuntimeError(
                f"Gemini 搜索失败 (HTTP {exc.response.status_code}): {body_text}"
            )
            if exc.response.status_code == 400:
                continue
            raise last_error from exc
    if last_error:
        raise last_error
    return []


def _parse_gemini_search(data: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    candidates = data.get("candidates") or []
    if not candidates:
        return []
    cand = candidates[0] if isinstance(candidates[0], dict) else {}
    parts = ((cand.get("content") or {}).get("parts") or [])
    text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
    grounding = cand.get("groundingMetadata") or data.get("groundingMetadata") or {}
    chunks = grounding.get("groundingChunks") or []
    supports = grounding.get("groundingSupports") or []

    by_url: dict[str, dict[str, Any]] = {}
    for idx, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            continue
        web = chunk.get("web") or {}
        url = str(web.get("uri") or web.get("url") or "").strip()
        if not _valid_url(url):
            continue
        snippet = _support_text_for_chunk(supports, idx)
        by_url[url] = {
            "title": str(web.get("title") or url),
            "url": url,
            "snippet": snippet,
            "published_at": None,
            "source_domain": _domain_of(url),
        }

    for row in _parse_items_json(text):
        url = str(row.get("url") or "").strip()
        if not _valid_url(url):
            continue
        existing = by_url.get(url) or {
            "title": str(row.get("title") or url),
            "url": url,
            "snippet": "",
            "published_at": None,
            "source_domain": _domain_of(url),
        }
        if row.get("title"):
            existing["title"] = str(row.get("title"))
        if row.get("snippet"):
            existing["snippet"] = str(row.get("snippet"))
        if row.get("published_at"):
            existing["published_at"] = str(row.get("published_at"))
        if row.get("source_domain"):
            existing["source_domain"] = str(row.get("source_domain"))
        by_url[url] = existing

    items = list(by_url.values())
    if not items:
        for row in _parse_items_json(text):
            url = str(row.get("url") or "").strip()
            if url and not _valid_url(url):
                continue
            title = str(row.get("title") or "").strip()
            snippet = str(row.get("snippet") or "").strip()
            if not title and not snippet:
                continue
            items.append(
                {
                    "title": title or url or "检索结果",
                    "url": url,
                    "snippet": snippet,
                    "published_at": str(row.get("published_at") or "") or None,
                    "source_domain": str(row.get("source_domain") or "") or _domain_of(url),
                }
            )
    return items[:limit]


def _support_text_for_chunk(supports: list[Any], chunk_index: int) -> str:
    texts: list[str] = []
    for row in supports:
        if not isinstance(row, dict):
            continue
        indices = row.get("groundingChunkIndices") or []
        if chunk_index not in indices:
            continue
        segment = row.get("segment") or {}
        text = str(segment.get("text") or "").strip()
        if text:
            texts.append(text)
    return " ".join(texts)[:800]


def _search_deepseek(query: str, *, limit: int, settings: Any) -> list[dict[str, Any]]:
    url = f"{str(settings.deepseek_api_base_url).rstrip('/')}/v1/chat/completions"
    system = (
        "你是公开资讯检索助手。只返回合法 JSON 对象，键为 items，值为数组。"
        "每个元素包含 title、url、snippet、published_at、source_domain。"
        "只列出你能给出真实可访问链接的近期公开新闻或官方页面。"
        "禁止编造 URL。没有把握时返回 {\"items\": []}。"
        "全部字段用字符串；published_at 尽量 ISO 或原文日期，未知则空字符串。"
    )
    user = f"查询：{query}\n最多 {limit} 条。"
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "max_tokens": 1600,
    }

    def _do() -> dict[str, Any]:
        client = get_http_client()
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=getattr(settings, "request_timeout_seconds", 60),
        )
        resp.raise_for_status()
        return resp.json()

    try:
        data = with_retries(_do, attempts=2, backoff_seconds=1.2, label="DeepSeek搜索替代")
    except httpx.HTTPStatusError as exc:
        body_text = (exc.response.text or "")[:300]
        raise RuntimeError(f"DeepSeek 搜索失败 (HTTP {exc.response.status_code}): {body_text}") from exc

    content = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    )
    items: list[dict[str, Any]] = []
    for row in _parse_items_json(str(content)):
        url_value = str(row.get("url") or "").strip()
        if not _valid_url(url_value):
            continue
        title = str(row.get("title") or url_value).strip()
        snippet = str(row.get("snippet") or "").strip()
        items.append(
            {
                "title": title,
                "url": url_value,
                "snippet": snippet,
                "published_at": str(row.get("published_at") or "") or None,
                "source_domain": str(row.get("source_domain") or "") or _domain_of(url_value),
            }
        )
    return items[:limit]


def _parse_items_json(text: str) -> list[dict[str, Any]]:
    raw = _JSON_FENCE_RE.sub("", (text or "").strip())
    if not raw:
        return []
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", raw)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, dict):
        rows = parsed.get("items") or parsed.get("results") or parsed.get("webpages") or []
    else:
        rows = parsed
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _valid_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or "." not in host:
        return False
    if host in {"example.com", "www.example.com"}:
        return False
    return True


def _domain_of(url: str) -> str:
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    return host
