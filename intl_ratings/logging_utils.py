"""原始报文落盘与错误发行体日志。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _safe_name(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", (text or "").strip())
    return (s or "unknown")[:max_len]


class RawResponseStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, issuer: str, source: str, payload: Any) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fname = f"{_safe_name(issuer)}__{_safe_name(source)}__{ts}.json"
        path = self.root / fname
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "issuer": issuer,
                        "source": source,
                        "saved_at": ts,
                        "payload": payload,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
        except OSError as exc:
            logger.warning("写入 raw response 失败: %s", exc)
        return path


class ErrorIssuerLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, issuer: str, field: str, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] issuer={issuer} field={field} msg={message}\n"
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            logger.warning("写入 error_issuers.log 失败: %s", exc)
        logger.warning("字段异常 %s / %s: %s", issuer, field, message)
