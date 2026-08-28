"""网络请求重试：缓解偶发 DNS / 超时 / 限流。"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    ConnectionError,
    TimeoutError,
    OSError,
)


def is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, _RETRYABLE):
        return True
    msg = str(exc).lower()
    if any(
        marker in msg
        for marker in ("余额不足", "积分不足", "insufficient credit", "insufficient balance")
    ):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 425, 429, 500, 502, 503, 504)
    markers = (
        "getaddrinfo failed",
        "name or service not known",
        "temporary failure",
        "connection reset",
        "timed out",
        "timeout",
        "10060",
        "11001",
        "errno 11001",
        "请求失败",
    )
    return any(m in msg for m in markers)


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    backoff_seconds: float = 1.5,
    label: str = "request",
) -> T:
    """执行 fn，遇可重试网络错误时指数退避重试。"""
    last_exc: Optional[BaseException] = None
    tries = max(1, int(attempts))
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i + 1 >= tries or not is_retryable_error(exc):
                raise
            delay = backoff_seconds * (2**i)
            logger.warning(
                "%s 失败（%d/%d），%.1fs 后重试: %s",
                label,
                i + 1,
                tries,
                delay,
                exc,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
