"""新浪财经 7×24 实时快讯采集（JSON API，非 HTML）。

页面：https://finance.sina.com.cn/7x24/?tag=0
接口：https://zhibo.sina.com.cn/api/zhibo/feed
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.services.http_client import get_http_client
from app.services.http_retry import with_retries
from app.services.recency import parse_published_at

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_FEED_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
_REFERER = "https://finance.sina.com.cn/7x24/?tag=0"
_ZHIBO_ID = "152"

# 页面分类 tag_id → 用途
# 0全部 1宏观 2行业 3公司 4数据 5市场 6观点 7央行 8其他 9焦点 10 A股 102 国际
MODULE_TAG_IDS: dict[str, tuple[str, ...]] = {
    "D": ("1", "4", "5", "7", "10"),  # 宏观 / 数据 / 市场 / 央行 / A股
    "B": ("102",),  # 国际
}

_TITLE_BRACKET = re.compile(r"^【([^】]{2,80})】")


@dataclass
class Sina724Hit:
    title: str
    url: str
    snippet: str
    published_at: Optional[str]
    source_domain: str = "finance.sina.com.cn"
    feed_label: str = "新浪财经7x24"
    tag_names: str = ""


class Sina724Collector:
    """抓取近 N 小时新浪 7×24 快讯。"""

    TIMEOUT = 30

    def __init__(
        self,
        *,
        module_tag_ids: Optional[dict[str, tuple[str, ...]]] = None,
        page_size: int = 40,
        retry_attempts: int = 3,
        retry_backoff: float = 1.5,
    ) -> None:
        self.module_tag_ids = module_tag_ids or dict(MODULE_TAG_IDS)
        self.page_size = max(5, min(int(page_size), 100))
        self.retry_attempts = retry_attempts
        self.retry_backoff = retry_backoff

    def collect_for_module(
        self,
        module_code: str,
        *,
        hours: int = 24,
        max_items: int = 36,
    ) -> list[Sina724Hit]:
        code = str(module_code).upper()
        tag_ids = self.module_tag_ids.get(code)
        if not tag_ids:
            return []

        cutoff = datetime.utcnow() - timedelta(hours=max(1, hours))
        hits: list[Sina724Hit] = []
        seen: set[str] = set()

        for tag_id in tag_ids:
            try:
                rows = self._fetch_tag(tag_id)
            except Exception as exc:
                logger.warning("新浪7x24 tag=%s 失败: %s", tag_id, exc)
                continue
            for row in rows:
                hit = self._row_to_hit(row)
                if not hit:
                    continue
                key = hit.url or f"{hit.title}|{hit.published_at}"
                if key in seen:
                    continue
                if hit.published_at:
                    pub = parse_published_at(hit.published_at)
                    if pub is not None and pub.replace(tzinfo=None) < cutoff:
                        continue
                seen.add(key)
                hits.append(hit)
                if len(hits) >= max_items:
                    return hits
        return hits

    def _fetch_tag(self, tag_id: str) -> list[dict]:
        params = {
            "page": "1",
            "page_size": str(self.page_size),
            "zhibo_id": _ZHIBO_ID,
            "tag_id": str(tag_id),
            "dire": "f",
            "dpc": "1",
        }
        headers = {
            "User-Agent": _UA,
            "Referer": _REFERER,
            "Accept": "application/json,text/javascript,*/*;q=0.01",
        }

        def _get() -> list[dict]:
            client = get_http_client()
            resp = client.get(
                _FEED_URL, params=params, headers=headers, timeout=self.TIMEOUT
            )
            resp.raise_for_status()
            data = self._parse_payload(resp.text)
            feed = (
                ((data.get("result") or {}).get("data") or {}).get("feed") or {}
            )
            items = feed.get("list") or []
            return items if isinstance(items, list) else []

        return with_retries(
            _get,
            attempts=self.retry_attempts,
            backoff_seconds=self.retry_backoff,
            label=f"sina724:{tag_id}",
        )

    @staticmethod
    def _parse_payload(text: str) -> dict:
        raw = (text or "").strip()
        if not raw:
            return {}
        # 纯 JSON 直接解析；JSONP 则取括号内
        if not raw.startswith("{"):
            start = raw.find("(")
            end = raw.rfind(")")
            if start >= 0 and end > start:
                raw = raw[start + 1 : end]
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def _row_to_hit(self, row: dict) -> Optional[Sina724Hit]:
        if not isinstance(row, dict):
            return None
        if int(row.get("is_delete") or 0) == 1:
            return None
        rich = str(row.get("rich_text") or "").strip()
        if not rich:
            return None
        title, snippet = self._split_title_body(rich)
        url = self._resolve_url(row)
        if not url:
            # 无详情页时用列表锚点，保证指纹可去重
            nid = row.get("id")
            url = f"https://finance.sina.com.cn/7x24/#{nid}" if nid else ""
        if not url:
            return None
        pub = str(row.get("create_time") or row.get("update_time") or "").strip() or None
        tags = row.get("tag") or []
        tag_names = ",".join(
            str(t.get("name") or "").strip()
            for t in tags
            if isinstance(t, dict) and t.get("name")
        )
        domain = urlparse(url).netloc or "finance.sina.com.cn"
        return Sina724Hit(
            title=title,
            url=url,
            snippet=snippet or f"【新浪财经7x24】{title}",
            published_at=pub,
            source_domain=domain,
            tag_names=tag_names,
        )

    @staticmethod
    def _split_title_body(rich: str) -> tuple[str, str]:
        m = _TITLE_BRACKET.match(rich)
        if m:
            title = m.group(1).strip()
            return title, rich
        # 无【标题】时截取前 48 字作标题
        compact = re.sub(r"\s+", " ", rich).strip()
        if len(compact) <= 48:
            return compact, rich
        return compact[:48].rstrip("，,。；; ") + "…", rich

    @staticmethod
    def _resolve_url(row: dict) -> str:
        doc = str(row.get("docurl") or "").strip()
        if doc.startswith("http"):
            return doc
        ext_raw = row.get("ext")
        if isinstance(ext_raw, str) and ext_raw.strip():
            try:
                ext = json.loads(ext_raw)
                u = str((ext or {}).get("docurl") or "").strip()
                if u.startswith("http"):
                    return u
            except json.JSONDecodeError:
                pass
        elif isinstance(ext_raw, dict):
            u = str(ext_raw.get("docurl") or "").strip()
            if u.startswith("http"):
                return u
        return ""
