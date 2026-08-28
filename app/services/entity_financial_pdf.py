"""从财务一览页发现最新有价证券报告书 PDF，避免写死当期文件。"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from app.services.entity_catalog import EntityProfile
from app.services.entity_kabutan import format_finance_period
from app.services.http_client import get_http_client

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 6 * 3600
_FETCH_TIMEOUT = 12.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

_HREF_RE = re.compile(r"""(?:href|data-href|data-url)\s*=\s*["']([^"']+)["']""", re.I)
_YEAR_RE = re.compile(r"20\d{2}")
_PDF_RE = re.compile(
    r"\.pdf(?:$|[?#])|yuho_pdf|/yuho/|fstatement/.+\.pdf|security_report/.+\.(?:pdf|zip)",
    re.I,
)
_POSITIVE_RE = re.compile(
    r"有価証券報告書|有价证券报告|yuho|yuka|security.?report|fstatement|年報|通期|annual.?report",
    re.I,
)
_NEGATIVE_RE = re.compile(
    r"訂正|臨時|半期|四半期|内部統制|議決権|株主総会|integrated.?report|sustainability",
    re.I,
)

_cache: dict[str, tuple[float, "LatestFinancialPdf | None"]] = {}


@dataclass(frozen=True)
class PdfCandidate:
    url: str
    title: str


@dataclass(frozen=True)
class LatestFinancialPdf:
    url: str
    title: str
    source: str  # discovered | fallback
    list_url: str | None = None


def resolve_latest_financial_pdf(
    profile: EntityProfile | None,
    *,
    live: bool = True,
) -> LatestFinancialPdf | None:
    if profile is None:
        return None
    list_url = (profile.financial_source_page or "").strip() or None
    fallback = _fallback_pdf(profile)
    if list_url and re.search(r"\.pdf(?:$|[?#])", list_url, re.I):
        return LatestFinancialPdf(
            url=list_url.split("#")[0],
            title=(profile.financial_source_label or "当期报告 PDF"),
            source="fallback",
            list_url=list_url,
        )
    if not list_url:
        return fallback
    discovered = _discover_cached(list_url, hint=profile.financial_pdf_hint, live=live)
    if discovered:
        return discovered
    return fallback


def extract_pdf_candidates(html: str, base_url: str) -> list[PdfCandidate]:
    from bs4 import BeautifulSoup

    found: dict[str, PdfCandidate] = {}
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("a"):
        href = str(tag.get("href") or "").strip()
        title = " ".join(tag.get_text(" ", strip=True).split())
        _remember(found, href, title, base_url)
        parent_text = " ".join((tag.parent.get_text(" ", strip=True) if tag.parent else "").split())
        if parent_text and href:
            _remember(found, href, parent_text[:180], base_url)

    for match in _HREF_RE.finditer(html or ""):
        _remember(found, match.group(1), "", base_url)
    return list(found.values())


def pick_latest_pdf(
    candidates: list[PdfCandidate],
    *,
    hint: str | None = None,
) -> PdfCandidate | None:
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda item: _score(item, hint=hint),
        reverse=True,
    )
    best = ranked[0]
    if _score(best, hint=hint) <= 0:
        return None
    return best


def _discover_cached(
    list_url: str,
    *,
    hint: str | None,
    live: bool = True,
) -> LatestFinancialPdf | None:
    now = time.time()
    cache_key = f"{list_url}|{hint or ''}"
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    if not live:
        return None
    result = _discover(list_url, hint=hint)
    _cache[cache_key] = (now, result)
    return result


def _discover(list_url: str, *, hint: str | None) -> LatestFinancialPdf | None:
    html = _fetch_html(list_url)
    if not html:
        return None
    picked = pick_latest_pdf(extract_pdf_candidates(html, list_url), hint=hint)
    if not picked:
        return None
    return LatestFinancialPdf(
        url=picked.url,
        title=picked.title or "当期有价证券报告书",
        source="discovered",
        list_url=list_url,
    )


def _fetch_html(url: str) -> str:
    try:
        client = get_http_client()
        resp = client.get(
            url,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
            timeout=_FETCH_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning("财务一览页 HTTP %s: %s", resp.status_code, url)
            return ""
        return resp.text or ""
    except Exception as exc:
        logger.warning("财务一览页抓取失败 %s: %s", url, exc)
        return ""


def _remember(found: dict[str, PdfCandidate], href: str, title: str, base_url: str) -> None:
    url = _normalize_pdf_url(href, base_url)
    if not url:
        return
    current = found.get(url)
    if current is None or (title and len(title) > len(current.title)):
        found[url] = PdfCandidate(url=url, title=title)


def _normalize_pdf_url(href: str, base_url: str) -> str | None:
    raw = (href or "").strip()
    if not raw or raw.startswith(("#", "javascript:", "mailto:")):
        return None
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    blob = absolute.lower()
    if not _PDF_RE.search(blob):
        return None
    return absolute.split("#")[0]


def _score(item: PdfCandidate, *, hint: str | None) -> int:
    blob = f"{item.title} {item.url}"
    score = 1
    if _POSITIVE_RE.search(blob):
        score += 40
    if _NEGATIVE_RE.search(blob):
        score -= 45
    years = [int(year) for year in _YEAR_RE.findall(blob)]
    if years:
        score += max(years) - 2000
    needle = (hint or "").strip()
    if needle and needle.lower() in blob.lower():
        score += 35
    return score


def _fallback_pdf(profile: EntityProfile) -> LatestFinancialPdf | None:
    for src in profile.financial_sources:
        if src.enabled and src.url:
            return LatestFinancialPdf(
                url=src.url,
                title=src.label or "当期有价证券报告书",
                source="fallback",
                list_url=profile.financial_source_page,
            )
    return None


def financial_payload_from_resolved(
    resolved: LatestFinancialPdf | None,
) -> dict[str, Any]:
    if resolved is None:
        return {
            "latest_pdf_url": None,
            "latest_pdf_title": None,
            "latest_pdf_source": None,
        }
    return {
        "latest_pdf_url": resolved.url,
        "latest_pdf_title": resolved.title,
        "latest_pdf_source": resolved.source,
    }


_PDF_CACHE_TTL_SECONDS = 6 * 3600
_PDF_MAX_BYTES = 25 * 1024 * 1024
_PDF_MAX_CHARS = 18000
_PDF_FETCH_TIMEOUT = 20.0
_PDF_KEYWORD_RE = re.compile(
    r"損益|貸借|キャッシュ|売上高|営業利益|営業益|総資産|自己資本|フリーCF|営業CF|"
    r"revenue|operating income|total assets|cash flow|净利润|营业收入",
    re.I,
)
_DIGIT_RE = re.compile(r"\d")
_pdf_statement_cache: dict[str, tuple[float, "PdfFinance"]] = {}

_PDF_EXTRACT_PROMPT = (
    "你从有价证券报告书或年度报告原文中提取通期合并报表要点。"
    "只返回 JSON 对象，键为 income、balance、cashflow，值为对象数组。"
    "income 字段：period,revenue,operating_profit,ordinary_profit,net_profit,eps,dps,released_at。"
    "balance 字段：period,bps,equity_ratio,total_assets,equity,retained_earnings,"
    "interest_bearing_debt_ratio,released_at。"
    "cashflow 字段：period,operating_profit,free_cash_flow,operating_cash_flow,"
    "investing_cash_flow,financing_cash_flow,cash_equivalents,cash_ratio。"
    "period 一律写成 YYYY.MM，例如 2026.03。"
    "数字必须原文照抄（可保留千分位），找不到的字段省略。"
    "只保留年度通期，不要半年度或季度。每表按报告期从新到旧，最多 8 行。不要编造。"
)


@dataclass(frozen=True)
class PdfFinance:
    statements: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {key: [] for key in ("income", "balance", "cashflow")}
    )
    ok: bool = False
    error: str | None = None

    def as_meta(self) -> dict[str, Any]:
        return {
            "pdf_parse_ok": self.ok,
            "pdf_parse_error": self.error,
            "pdf_row_count": sum(len(rows) for rows in self.statements.values()),
        }


def load_pdf_statements(pdf_url: str | None, *, live: bool = True) -> PdfFinance:
    url = (pdf_url or "").strip()
    if not url:
        return PdfFinance()
    now = time.time()
    cached = _pdf_statement_cache.get(url)
    if cached and now - cached[0] < _PDF_CACHE_TTL_SECONDS:
        return cached[1]
    if not live:
        return PdfFinance()
    result = _parse_pdf_url(url)
    _pdf_statement_cache[url] = (now, result)
    return result


def verify_statement_rows(
    statements: dict[str, list[dict[str, Any]]],
    source_text: str,
) -> dict[str, list[dict[str, Any]]]:
    """只保留能在原文中找到的数字，避免模型编造。"""
    compact_source = re.sub(r"[,\s]", "", source_text or "")
    out: dict[str, list[dict[str, Any]]] = {}
    for key, rows in (statements or {}).items():
        kept_rows: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            cleaned: dict[str, Any] = {}
            for field, value in row.items():
                text = str(value).strip() if value is not None else ""
                if not text:
                    continue
                if field == "period":
                    cleaned[field] = format_finance_period(text) or text
                    continue
                if not _DIGIT_RE.search(text):
                    cleaned[field] = text
                    continue
                if _number_in_text(text, source_text, compact_source):
                    cleaned[field] = text
            if cleaned.get("period") and len(cleaned) > 1:
                kept_rows.append(cleaned)
        out[key] = kept_rows[:8]
    return out


def _number_in_text(value: str, source_text: str, compact_source: str) -> bool:
    raw = (value or "").strip()
    if raw in source_text:
        return True
    compact = re.sub(r"[,\s]", "", raw)
    return bool(compact) and compact in compact_source


def _parse_pdf_url(url: str) -> PdfFinance:
    data = _fetch_pdf_bytes(url)
    if not data:
        return PdfFinance(error="fetch_failed")
    text = extract_statement_text(data)
    if not text.strip():
        return PdfFinance(error="empty_text")
    parsed = _extract_statements_with_gemini(text)
    verified = verify_statement_rows(parsed, text)
    ok = any(verified.values())
    return PdfFinance(
        statements={
            "income": verified.get("income") or [],
            "balance": verified.get("balance") or [],
            "cashflow": verified.get("cashflow") or [],
        },
        ok=ok,
        error=None if ok else "parse_empty",
    )


def extract_statement_text(data: bytes) -> str:
    from io import BytesIO

    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("缺少 pypdf，无法解析财务 PDF")
        return ""
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        logger.warning("财务 PDF 打开失败: %s", exc)
        return ""
    preferred: list[str] = []
    rest: list[str] = []
    for page in reader.pages:
        page_text = (page.extract_text() or "").strip()
        if not page_text:
            continue
        if _PDF_KEYWORD_RE.search(page_text):
            preferred.append(page_text)
        else:
            rest.append(page_text)
    blob = "\n\n".join(preferred + rest)
    return blob[:_PDF_MAX_CHARS]


def _fetch_pdf_bytes(url: str) -> bytes:
    try:
        client = get_http_client()
        resp = client.get(
            url,
            headers={"User-Agent": _UA, "Accept": "application/pdf,application/octet-stream,*/*"},
            timeout=_PDF_FETCH_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning("财务 PDF HTTP %s: %s", resp.status_code, url)
            return b""
        content_type = (resp.headers.get("content-type") or "").lower()
        data = resp.content or b""
        if "html" in content_type and not data.startswith(b"%PDF"):
            return b""
        if len(data) > _PDF_MAX_BYTES:
            logger.warning("财务 PDF 超过大小上限: %s", url)
            return b""
        return data
    except Exception as exc:
        logger.warning("财务 PDF 抓取失败 %s: %s", url, exc)
        return b""


def _extract_statements_with_gemini(text: str) -> dict[str, list[dict[str, Any]]]:
    empty = {"income": [], "balance": [], "cashflow": []}
    try:
        from app.services.api_keys import is_placeholder_key
        from app.services.gemini_analyzer import gemini_for
        from app.config import get_settings

        if is_placeholder_key(getattr(get_settings(), "gemini_api_key", None)):
            return empty
        analyzer = gemini_for("finance", timeout=20)
        analyzer.retry_attempts = 1
        parsed = analyzer._chat_json_object(_PDF_EXTRACT_PROMPT, text[:_PDF_MAX_CHARS])
    except Exception as exc:
        logger.warning("财务 PDF Gemini 抽取失败: %s", exc)
        return empty
    out = dict(empty)
    if not isinstance(parsed, dict):
        return out
    for key in out:
        rows = parsed.get(key)
        if isinstance(rows, list):
            out[key] = [row for row in rows if isinstance(row, dict)]
    return out
