"""网页正文提取（优先 trafilatura，失败回退 HTML 清洗）。"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (compatible; RiskIntelBot/1.1; +https://localhost) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def extract_article_text(url: str, timeout: int = 12, max_chars: int = 8000) -> str:
    """抓取 URL 并提取可读正文。"""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    host = urlparse(url).netloc.lower()
    url_l = url.lower()
    # 跳过检索入口页；允许 TDnet PDF
    if any(x in host for x in ("edinet-fsa.go.jp",)):
        return ""
    if "release.tdnet.info" in host and (
        "query=" in url_l or "i_main_" in url_l or "i_list_" in url_l
    ):
        return ""
    if "release.tdnet.info" in host and url_l.endswith(".pdf"):
        return _extract_pdf_text(url, timeout=timeout, max_chars=max_chars)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": _UA, "Accept": "text/html,*/*"})
            if resp.status_code >= 400:
                return ""
            content_type = (resp.headers.get("content-type") or "").lower()
            if "pdf" in content_type or url_l.endswith(".pdf"):
                return _extract_pdf_bytes(resp.content, max_chars=max_chars)
            html = resp.text or ""
            if "html" not in content_type and "<html" not in html[:500].lower():
                return (html or "")[:max_chars].strip()
    except httpx.HTTPError as exc:
        logger.debug("正文抓取失败 %s: %s", url, exc)
        return ""

    text = _extract_with_trafilatura(html, url) or _strip_html(html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def _extract_pdf_text(url: str, *, timeout: int, max_chars: int) -> str:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(
                url,
                headers={"User-Agent": _UA, "Accept": "application/pdf,*/*"},
            )
            if resp.status_code >= 400:
                return ""
            return _extract_pdf_bytes(resp.content, max_chars=max_chars)
    except httpx.HTTPError as exc:
        logger.debug("PDF 抓取失败 %s: %s", url, exc)
        return ""


def _extract_pdf_bytes(data: bytes, *, max_chars: int) -> str:
    if not data:
        return ""
    try:
        from io import BytesIO

        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(BytesIO(data))
        parts: list[str] = []
        for page in reader.pages[:8]:
            parts.append(page.extract_text() or "")
            if sum(len(p) for p in parts) >= max_chars:
                break
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if len(text) > max_chars:
            return text[:max_chars] + "…"
        return text
    except Exception as exc:  # noqa: BLE001
        logger.debug("PDF 解析失败: %s", exc)
        return ""


def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        result = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
        return (result or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("trafilatura 提取失败: %s", exc)
        return None


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def enrich_items_with_body(
    items: list[dict],
    *,
    url_key: str = "url",
    body_key: str = "body",
    max_items: int = 8,
    max_chars: int = 6000,
) -> list[dict]:
    """为前 N 条结果并发补充正文，便于下游 LLM 分析。

    Modified by DingJiaye: 2026-08-27 — 原实现按网页顺序等待，单个慢站会阻塞
    整个智能体任务；改为有限并发并保持原有资讯顺序。正文取不到仍保留标题与原始链接。
    """
    enriched = [dict(row) for row in items]
    targets = [
        (index, str(item.get(url_key) or ""))
        for index, item in enumerate(enriched)
        if index < max_items and str(item.get(url_key) or "")
    ]
    if not targets:
        return enriched

    workers = min(4, len(targets))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="article-body") as pool:
        futures = {
            pool.submit(extract_article_text, url, max_chars=max_chars): index
            for index, url in targets
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                body = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.debug("并发正文抓取失败: %s", exc)
                body = ""
            if body:
                enriched[index][body_key] = body
    return enriched
