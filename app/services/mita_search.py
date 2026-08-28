"""秘塔（Metaso）搜索 API 封装（支持域名白名单/黑名单）。"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings
from app.services.api_keys import validate_mita_key
from app.services.http_client import get_http_client
from app.services.http_retry import is_retryable_error, with_retries

logger = logging.getLogger(__name__)

_SEARCH_TERM_SPLIT_RE = re.compile(r"[\s,，、&/]+")
_MAX_SPLIT_SEARCH_TERMS = 6


def split_search_terms(raw: str) -> list[str]:
    """按空格、逗号、顿号、&、/ 拆成独立检索词，去重并保持顺序。"""
    seen: set[str] = set()
    terms: list[str] = []
    for part in _SEARCH_TERM_SPLIT_RE.split(raw or ""):
        term = part.strip()
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= _MAX_SPLIT_SEARCH_TERMS:
            break
    return terms


_QUOTA_LOCK = threading.Lock()
_MITA_QUOTA_EXHAUSTED = False
_MITA_SKIP_DIRECT = False
_QUOTA_MARKERS = (
    "余额不足",
    "积分不足",
    "insufficient credit",
    "insufficient credits",
    "insufficient balance",
    "no credit",
    "quota exceeded",
)
_QUOTA_ERR_CODES = {3000, "3000"}


class MitaQuotaError(RuntimeError):
    """秘塔账户余额或积分不足。"""


def is_mita_quota_error(payload_or_exc: Any) -> bool:
    if isinstance(payload_or_exc, MitaQuotaError):
        return True
    err_code = None
    text = ""
    if isinstance(payload_or_exc, dict):
        err_code = payload_or_exc.get("errCode")
        text = str(
            payload_or_exc.get("errMsg")
            or payload_or_exc.get("message")
            or payload_or_exc.get("error")
            or ""
        )
    else:
        text = str(payload_or_exc or "")
    if err_code in _QUOTA_ERR_CODES:
        return True
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _QUOTA_MARKERS)


def mita_quota_exhausted() -> bool:
    with _QUOTA_LOCK:
        return _MITA_QUOTA_EXHAUSTED


def mita_direct_skipped() -> bool:
    with _QUOTA_LOCK:
        return _MITA_QUOTA_EXHAUSTED or _MITA_SKIP_DIRECT


def mark_mita_quota_exhausted() -> None:
    global _MITA_QUOTA_EXHAUSTED
    with _QUOTA_LOCK:
        _MITA_QUOTA_EXHAUSTED = True


def mark_mita_skip_direct() -> None:
    """后续查询不再打秘塔直连，改为 Gemini/DeepSeek。"""
    global _MITA_SKIP_DIRECT
    with _QUOTA_LOCK:
        _MITA_SKIP_DIRECT = True


def reset_mita_quota_state() -> None:
    global _MITA_QUOTA_EXHAUSTED, _MITA_SKIP_DIRECT
    with _QUOTA_LOCK:
        _MITA_QUOTA_EXHAUSTED = False
        _MITA_SKIP_DIRECT = False


def is_mita_fallback_worthy(exc: BaseException) -> bool:
    """余额、超时、网络、鉴权失败可改走大模型检索；参数错误不转。"""
    if isinstance(exc, MitaQuotaError) or is_mita_quota_error(exc):
        return True
    msg = str(exc or "")
    lowered = msg.lower()
    if "参数错误" in msg or ("errcode" in lowered and "4001" in lowered):
        return False
    if is_retryable_error(exc):
        return True
    markers = (
        "未配置有效的 mita_api_key",
        "请求失败",
        "http 401",
        "http 403",
        "http 408",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "timeout",
        "timed out",
        "connecterror",
        "connecttimeout",
        "networkerror",
    )
    return any(marker in lowered for marker in markers)


@dataclass
class MitaSearchResultItem:
    title: str
    url: str
    snippet: str
    published_at: Optional[str] = None
    source_domain: Optional[str] = None


@dataclass
class MitaSearchResponse:
    query: str
    items: list[MitaSearchResultItem] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    provider: str = "mita"


def apply_local_domain_filter(
    items: list[MitaSearchResultItem],
    whitelist: Optional[list[str]],
    blacklist: Optional[list[str]],
) -> list[MitaSearchResultItem]:
    wl = {d.lower().strip() for d in (whitelist or []) if d}
    bl = {d.lower().strip() for d in (blacklist or []) if d}

    def domain_of(url: str) -> str:
        host = urlparse(url).netloc or url.split("/", 1)[0]
        return host.lower().removeprefix("www.")

    out: list[MitaSearchResultItem] = []
    for item in items:
        dom = (item.source_domain or domain_of(item.url)).lower()
        if bl and any(dom == b or dom.endswith("." + b) for b in bl):
            continue
        if wl and not any(dom == w or dom.endswith("." + w) for w in wl):
            continue
        out.append(item)
    return out


def finalize_search_items(
    items: list[MitaSearchResultItem],
    whitelist_domains: Optional[list[str]],
    blacklist_domains: Optional[list[str]],
) -> list[MitaSearchResultItem]:
    filtered = apply_local_domain_filter(items, whitelist_domains, blacklist_domains)
    if items and not filtered and whitelist_domains:
        logger.warning(
            "域名白名单过滤后结果为空（原始 %d 条），已降级为仅黑名单过滤",
            len(items),
        )
        filtered = apply_local_domain_filter(items, None, blacklist_domains)
    return filtered


class MitaSearchClient:
    """
    秘塔 AI 搜索客户端。
    官方端点: POST https://metaso.cn/api/v1/search
    文档: https://metaso.cn/search-api/playground
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.mita_api_base_url).rstrip("/")
        self.api_key = api_key or settings.mita_api_key
        self.timeout = timeout or settings.request_timeout_seconds
        self.retry_attempts = int(getattr(settings, "network_retry_attempts", 3) or 3)
        self.retry_backoff = float(getattr(settings, "network_retry_backoff_seconds", 1.5) or 1.5)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _search_url(self) -> str:
        """构造普通网页 Search API 地址。"""
        base = self.base_url.rstrip("/")
        if base.endswith("/search"):
            return base
        return f"{base}/search"

    def _build_query(
        self,
        query: str,
        whitelist_domains: Optional[list[str]] = None,
    ) -> str:
        """秘塔 API 无原生域名参数时，通过 site: 语法收窄检索范围。"""
        wl = [d.strip() for d in (whitelist_domains or []) if d and d.strip()]
        if not wl:
            return query
        if len(wl) <= 5:
            site_part = " OR ".join(f"site:{d}" for d in wl)
            return f"{query} ({site_part})"
        return query

    def search(
        self,
        query: str,
        whitelist_domains: Optional[list[str]] = None,
        blacklist_domains: Optional[list[str]] = None,
        max_results: int = 10,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> MitaSearchResponse:
        # 风险日报的信源必须可追溯。默认不把秘塔检索静默替换成
        # Gemini/DeepSeek 的联网生成，以免来源与采集口径发生漂移。
        allow_llm_fallback = bool(
            getattr(get_settings(), "search_llm_fallback_enabled", False)
        )
        if mita_direct_skipped():
            reason = "余额不足" if mita_quota_exhausted() else "直连不可用"
            if not allow_llm_fallback:
                raise RuntimeError(f"秘塔搜索不可用：{reason}")
            logger.info("秘塔%s，使用 Gemini/DeepSeek 搜索替代", reason)
            return self._search_via_llm(
                query,
                whitelist_domains=whitelist_domains,
                blacklist_domains=blacklist_domains,
                max_results=max_results,
            )
        try:
            return self._search_metaso(
                query,
                whitelist_domains=whitelist_domains,
                blacklist_domains=blacklist_domains,
                max_results=max_results,
                extra_params=extra_params,
            )
        except MitaQuotaError as exc:
            mark_mita_quota_exhausted()
            if not allow_llm_fallback:
                raise
            logger.warning("秘塔余额不足，使用 Gemini/DeepSeek 搜索替代: %s", exc)
            return self._search_via_llm(
                query,
                whitelist_domains=whitelist_domains,
                blacklist_domains=blacklist_domains,
                max_results=max_results,
            )
        except Exception as exc:
            if not is_mita_fallback_worthy(exc):
                raise
            mark_mita_skip_direct()
            if not allow_llm_fallback:
                raise
            logger.warning("秘塔搜索失败，使用 Gemini/DeepSeek 搜索替代: %s", exc)
            return self._search_via_llm(
                query,
                whitelist_domains=whitelist_domains,
                blacklist_domains=blacklist_domains,
                max_results=max_results,
            )

    def _search_metaso(
        self,
        query: str,
        whitelist_domains: Optional[list[str]] = None,
        blacklist_domains: Optional[list[str]] = None,
        max_results: int = 10,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> MitaSearchResponse:
        validate_mita_key(self.api_key)

        effective_query = self._build_query(query, whitelist_domains)
        payload: dict[str, Any] = {
            "q": effective_query,
            "scope": "webpage",
            "size": max(1, min(int(max_results), 50)),
            "includeSummary": True,
            "includeRawContent": False,
            "conciseSnippet": False,
        }
        if extra_params:
            payload.update(extra_params)

        url = self._search_url()
        logger.info("秘塔搜索: q=%s", effective_query[:200])

        def _do_search() -> dict[str, Any] | list[Any]:
            try:
                client = get_http_client()
                response = client.post(
                    url, headers=self._headers(), json=payload, timeout=self.timeout
                )
                body_text = (response.text or "")[:300]
                try:
                    parsed = response.json()
                except ValueError:
                    parsed = None
                if response.status_code == 402 or (
                    isinstance(parsed, dict) and is_mita_quota_error(parsed)
                ) or (
                    response.status_code >= 400 and is_mita_quota_error(body_text)
                ):
                    err = ""
                    if isinstance(parsed, dict):
                        err = str(parsed.get("errMsg") or parsed.get("errCode") or "")
                    raise MitaQuotaError(err or body_text or "余额不足")
                response.raise_for_status()
                if parsed is None:
                    snippet = (response.text or "")[:500]
                    logger.error("秘塔 API 返回非 JSON: %s", snippet)
                    raise RuntimeError("秘塔搜索响应解析失败")
                return parsed
            except MitaQuotaError:
                raise
            except httpx.HTTPStatusError as exc:
                body = (exc.response.text or "")[:300]
                if is_mita_quota_error(body) or exc.response.status_code == 402:
                    raise MitaQuotaError(body or "余额不足") from exc
                # 5xx / 429 可重试
                if exc.response.status_code in (408, 425, 429, 500, 502, 503, 504):
                    raise
                logger.error("秘塔 API HTTP %s: %s", exc.response.status_code, body)
                raise RuntimeError(
                    f"秘塔搜索请求失败 (HTTP {exc.response.status_code}): {body or exc}"
                ) from exc
            except httpx.HTTPError as exc:
                logger.error("秘塔 API 请求失败: %s", exc)
                raise RuntimeError(f"秘塔搜索请求失败: {exc}") from exc

        try:
            data = with_retries(
                _do_search,
                attempts=self.retry_attempts,
                backoff_seconds=self.retry_backoff,
                label="秘塔搜索",
            )
        except MitaQuotaError:
            raise
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "")[:300]
            if is_mita_quota_error(body) or exc.response.status_code == 402:
                raise MitaQuotaError(body or "余额不足") from exc
            raise RuntimeError(
                f"秘塔搜索请求失败 (HTTP {exc.response.status_code}): {body or exc}"
            ) from exc

        if not isinstance(data, (dict, list)):
            logger.warning("秘塔 API 返回未知顶层类型: %s", type(data).__name__)
            data = {}

        # API 业务错误（errCode）也记清楚，便于界面区分「无资讯」与「请求失败」
        if isinstance(data, dict) and data.get("errCode") not in (None, 0, "0"):
            err = data.get("errMsg") or data.get("errCode")
            if is_mita_quota_error(data):
                raise MitaQuotaError(str(err))
            raise RuntimeError(f"秘塔搜索失败: {err}")

        items = self._parse_items(data)[:max_results]
        if not items:
            logger.warning(
                "秘塔搜索未解析到任何结果，响应键: %s",
                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            )

        filtered = self._finalize_items(items, whitelist_domains, blacklist_domains)
        return MitaSearchResponse(
            query=effective_query,
            items=filtered,
            raw=data if isinstance(data, dict) else {"items": data},
            provider="mita",
        )

    def _search_via_llm(
        self,
        query: str,
        whitelist_domains: Optional[list[str]] = None,
        blacklist_domains: Optional[list[str]] = None,
        max_results: int = 10,
    ) -> MitaSearchResponse:
        from app.services.llm_web_search import search_web_fallback

        effective_query = self._build_query(query, whitelist_domains)
        rows, provider = search_web_fallback(
            query,
            max_results=max_results,
            whitelist_domains=whitelist_domains,
        )
        items = [
            MitaSearchResultItem(
                title=str(row.get("title") or "无标题"),
                url=str(row.get("url") or ""),
                snippet=str(row.get("snippet") or ""),
                published_at=str(row.get("published_at")) if row.get("published_at") else None,
                source_domain=str(row.get("source_domain")) if row.get("source_domain") else None,
            )
            for row in rows
        ]
        filtered = self._finalize_items(items, whitelist_domains, blacklist_domains)
        logger.info(
            "搜索替代完成: provider=%s query=%s items=%s",
            provider,
            effective_query[:120],
            len(filtered),
        )
        return MitaSearchResponse(
            query=effective_query,
            items=filtered,
            raw={"provider": provider, "fallback": True, "query": effective_query},
            provider=provider,
        )

    def _finalize_items(
        self,
        items: list[MitaSearchResultItem],
        whitelist_domains: Optional[list[str]],
        blacklist_domains: Optional[list[str]],
    ) -> list[MitaSearchResultItem]:
        return finalize_search_items(items, whitelist_domains, blacklist_domains)

    def _apply_local_domain_filter(
        self,
        items: list[MitaSearchResultItem],
        whitelist: Optional[list[str]],
        blacklist: Optional[list[str]],
    ) -> list[MitaSearchResultItem]:
        return apply_local_domain_filter(items, whitelist, blacklist)

    def _parse_items(self, data: dict[str, Any]) -> list[MitaSearchResultItem]:
        candidates: list[Any] = []
        answer_text = ""
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            payload_data = data.get("data")
            if isinstance(payload_data, dict):
                answer_text = str(payload_data.get("text") or "")
            for key in ("webpages", "results", "items", "data", "list", "records"):
                block = data.get(key)
                if isinstance(block, list):
                    candidates = block
                    break
                if isinstance(block, dict):
                    for sub in ("references", "webpages", "items", "list", "results"):
                        inner = block.get(sub)
                        if isinstance(inner, list):
                            candidates = inner
                            break
                if candidates:
                    break

        parsed: list[MitaSearchResultItem] = []
        for row in candidates:
            if not isinstance(row, dict):
                continue
            url = str(
                row.get("url")
                or row.get("link")
                or row.get("href")
                or row.get("sourceUrl")
                or ((row.get("file_meta") or {}).get("url") if isinstance(row.get("file_meta"), dict) else "")
                or ""
            )
            if url.startswith("/"):
                url = urljoin("https://metaso.cn", url)
            snippet = str(
                row.get("snippet")
                or row.get("summary")
                or row.get("content")
                or row.get("abstract")
                or row.get("desc")
                or answer_text
                or ""
            )
            title = str(row.get("title") or row.get("name") or row.get("siteName") or url or "无标题")
            published = (
                row.get("published_at")
                or row.get("date")
                or row.get("publishTime")
                or row.get("publishDate")
            )
            domain = row.get("domain") or row.get("source_domain") or row.get("siteName")
            if not domain and url:
                domain = urlparse(url).netloc
            parsed.append(
                MitaSearchResultItem(
                    title=title,
                    url=url,
                    snippet=snippet,
                    published_at=str(published) if published else None,
                    source_domain=str(domain) if domain else None,
                )
            )
        if not parsed and answer_text:
            parsed.append(
                MitaSearchResultItem(
                    title="秘塔网络搜索摘要",
                    url="",
                    snippet=answer_text,
                    source_domain="metaso.cn",
                )
            )
        return parsed

    def search_to_json_text(self, response: MitaSearchResponse) -> str:
        return json.dumps(
            [
                {
                    "title": i.title,
                    "url": i.url,
                    "snippet": i.snippet,
                    "published_at": i.published_at,
                    "source_domain": i.source_domain,
                }
                for i in response.items
            ],
            ensure_ascii=False,
        )
