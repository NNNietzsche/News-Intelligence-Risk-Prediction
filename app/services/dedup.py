"""稳定内容指纹：跨 RSS/秘塔去重，规避发布时间毫秒抖动。"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}

# 标题末尾常见媒体/栏目后缀，去重前剥离
_MEDIA_SUFFIX = re.compile(
    r"[\s\u3000]*[-–—_|｜/／]\s*[^-\u3000–—_|｜/／]{1,24}$"
)
_BRACKET_PREFIX = re.compile(r"^【([^】]{2,80})】\s*")
_NON_WORD = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def normalize_url(url: Optional[str]) -> str:
    if not url:
        return ""
    raw = url.strip()
    try:
        parsed = urlparse(raw)
        query = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        ]
        cleaned = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=re.sub(r"/+$", "", parsed.path or "") or "/",
            query=urlencode(query),
            fragment="",
        )
        return urlunparse(cleaned)
    except Exception:
        return raw.lower()


def normalize_title(title: Optional[str]) -> str:
    text = (title or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text[:240]


def event_title_key(title: Optional[str]) -> str:
    """事件级标题键：去媒体后缀、标点与空白，便于跨社媒判同事件。"""
    text = (title or "").strip()
    if not text:
        return ""
    m = _BRACKET_PREFIX.match(text)
    if m:
        # 【标题】正文… → 优先用括号内标题
        inner = m.group(1).strip()
        rest = text[m.end() :].strip()
        text = inner if len(inner) >= 6 else (inner + " " + rest[:40]).strip()
    for _ in range(3):
        nxt = _MEDIA_SUFFIX.sub("", text).strip()
        if nxt == text:
            break
        text = nxt
    text = text.lower()
    text = _NON_WORD.sub("", text)
    return text[:160]


def titles_similar(
    a: Optional[str],
    b: Optional[str],
    *,
    threshold: float = 0.82,
) -> bool:
    """判断两条标题是否为同一事件（含不同媒体转述）。"""
    ka = event_title_key(a)
    kb = event_title_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if len(shorter) >= 8 and shorter in longer:
        if len(shorter) / max(len(longer), 1) >= 0.72:
            return True
    return SequenceMatcher(None, ka, kb).ratio() >= threshold


def published_day_key(published_at: Optional[str]) -> str:
    """只用到日，避免 feedparser 毫秒差异导致 hash 抖动。"""
    if not published_at:
        return ""
    text = str(published_at).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text[:10]


def content_fingerprint(
    *,
    module_code: str,
    title: Optional[str],
    url: Optional[str] = None,
    published_at: Optional[str] = None,
) -> str:
    url_key = normalize_url(url)
    if url_key:
        seed = f"{module_code.upper()}|url|{url_key}"
    else:
        seed = (
            f"{module_code.upper()}|title|"
            f"{normalize_title(title)}|{published_day_key(published_at)}"
        )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


def _item_quality_score(it: dict[str, Any]) -> tuple:
    body = it.get("body") or it.get("snippet") or it.get("核心摘要") or ""
    title = it.get("title") or it.get("标题") or ""
    domain = (it.get("source_domain") or "").lower()
    prefer = 1 if any(
        x in domain
        for x in (
            "tdnet",
            "edinet",
            "reuters",
            "nikkei",
            "bloomberg",
            "nhk",
            "mitsubishicorp",
            "mitsui.com",
            "itochu",
            "sumitomocorp",
            "marubeni",
            "denso",
            "nyk.com",
        )
    ) else 0
    has_time = 1 if (it.get("published_at") or it.get("发布时间")) else 0
    return (prefer, has_time, len(str(body)), len(str(title)))


def dedupe_by_title_similarity(
    items: list[dict[str, Any]],
    *,
    title_getter: Optional[Callable[[dict[str, Any]], Optional[str]]] = None,
    threshold: float = 0.82,
) -> list[dict[str, Any]]:
    """按标题相似度合并同一事件，保留质量更高的一条。"""
    get_title = title_getter or (
        lambda it: it.get("title") or it.get("标题")
    )
    kept: list[dict[str, Any]] = []
    for it in items:
        title = get_title(it)
        dup_idx: Optional[int] = None
        for i, prev in enumerate(kept):
            if titles_similar(title, get_title(prev), threshold=threshold):
                dup_idx = i
                break
        if dup_idx is None:
            kept.append(it)
            continue
        # 同事件：保留质量更高者
        if _item_quality_score(it) > _item_quality_score(kept[dup_idx]):
            kept[dup_idx] = it
    return kept
