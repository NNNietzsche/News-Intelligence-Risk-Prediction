"""共享 httpx 客户端：复用连接，减少握手与 DNS 开销。

Client 的请求接口线程安全；超时可在单次 request 上覆盖。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client: Optional[httpx.Client] = None

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_LIMITS = httpx.Limits(max_connections=40, max_keepalive_connections=20)


def get_http_client() -> httpx.Client:
    """返回进程级共享 Client（惰性创建）。"""
    global _client
    with _lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                timeout=_DEFAULT_TIMEOUT,
                follow_redirects=True,
                limits=_LIMITS,
                headers={"User-Agent": "RiskIntelBot/1.3 (+local; shared-http)"},
            )
            logger.debug("已创建共享 HTTP 客户端")
        return _client


def close_http_client() -> None:
    """进程退出或测试清理时关闭。"""
    global _client
    with _lock:
        if _client is not None and not _client.is_closed:
            try:
                _client.close()
            except Exception as exc:
                logger.debug("关闭共享 HTTP 客户端: %s", exc)
        _client = None
