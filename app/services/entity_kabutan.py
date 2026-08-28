"""从株探财务页抓取通期三表要点。"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.services.entity_catalog import FINANCIAL_STATEMENT_KEYS
from app.services.http_client import get_http_client

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 6 * 3600
_FETCH_TIMEOUT = 12.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
_MAX_ROWS = 8
_EMPTY_CELLS = {"", "-", "－", "—", "‐", "ー"}
_SKIP_PERIOD = re.compile(r"比|過去|修正")
_FULL_YEAR = re.compile(r"予?\s*20\d{2}\.\d{2}$")
_YY_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{2})$")
_PREFIX = re.compile(r"^[IUC連]\s*")

_HEADER_MAP = {
    "income": {
        "決算期": "period",
        "売上高": "revenue",
        "営業益": "operating_profit",
        "経常益": "ordinary_profit",
        "最終益": "net_profit",
        "修正1株益": "eps",
        "修正1株配": "dps",
        "発表日": "released_at",
    },
    "balance": {
        "決算期": "period",
        "1株純資産": "bps",
        "自己資本比率": "equity_ratio",
        "総資産": "total_assets",
        "自己資本": "equity",
        "剰余金": "retained_earnings",
        "有利子負債倍率": "interest_bearing_debt_ratio",
        "発表日": "released_at",
    },
    "cashflow": {
        "決算期": "period",
        "営業益": "operating_profit",
        "フリーCF": "free_cash_flow",
        "営業CF": "operating_cash_flow",
        "投資CF": "investing_cash_flow",
        "財務CF": "financing_cash_flow",
        "現金等残高": "cash_equivalents",
        "現金比率": "cash_ratio",
    },
}

_cache: dict[str, tuple[float, "KabutanFinance"]] = {}


@dataclass(frozen=True)
class KabutanFinance:
    statements: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {key: [] for key in FINANCIAL_STATEMENT_KEYS}
    )
    ok: bool = False
    error: str | None = None
    fetched_at: float | None = None
    source_url: str | None = None

    def as_meta(self) -> dict[str, Any]:
        return {
            "kabutan_ok": self.ok,
            "kabutan_error": self.error,
            "kabutan_fetched_at": self.fetched_at,
            "kabutan_row_count": sum(len(rows) for rows in self.statements.values()),
        }


def kabutan_finance_url_for_code(stock_code: str | None) -> str | None:
    code = (stock_code or "").strip()
    if code.isdigit():
        return f"https://kabutan.jp/stock/finance?code={code}"
    return None


def load_kabutan_statements(stock_code: str | None, *, live: bool = True) -> KabutanFinance:
    url = kabutan_finance_url_for_code(stock_code)
    if not url or not stock_code:
        return KabutanFinance()
    code = stock_code.strip()
    now = time.time()
    cached = _cache.get(code)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    if not live:
        return KabutanFinance(source_url=url)
    html = _fetch_html(url)
    if not html:
        result = KabutanFinance(error="fetch_failed", source_url=url, fetched_at=now)
        _cache[code] = (now, result)
        return result
    parsed = parse_kabutan_finance(html)
    result = KabutanFinance(
        statements=parsed,
        ok=any(parsed.values()),
        error=None if any(parsed.values()) else "parse_empty",
        fetched_at=now,
        source_url=url,
    )
    _cache[code] = (now, result)
    return result


def warm_kabutan_statements(stock_codes: Iterable[str], *, max_workers: int = 4) -> dict[str, int]:
    """预热已上市主体的株探通期三表缓存，失败不阻断 Web 服务启动。"""
    # Modified by DingJiaye: 2026-08-26 — 让侧栏内的上市主体首次打开即可显示已抓取财务数据。
    codes = list(dict.fromkeys(str(code).strip() for code in stock_codes if str(code).strip().isdigit()))
    if not codes:
        return {"requested": 0, "loaded": 0, "failed": 0}
    loaded = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(codes))) as executor:
        futures = {executor.submit(load_kabutan_statements, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                if future.result().ok:
                    loaded += 1
            except Exception as exc:  # pragma: no cover - network guard
                logger.warning("株探财务预热失败 code=%s: %s", code, exc)
    result = {"requested": len(codes), "loaded": loaded, "failed": len(codes) - loaded}
    logger.info("株探财务缓存预热完成: %s", result)
    return result


def parse_kabutan_finance(html: str) -> dict[str, list[dict[str, Any]]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    found: dict[str, list[tuple[int, list[dict[str, Any]]]]] = {
        key: [] for key in FINANCIAL_STATEMENT_KEYS
    }
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [_norm_header(cell) for cell in _row_texts(rows[0])]
        kind = _classify_headers(headers)
        if kind is None:
            continue
        mapped = _HEADER_MAP[kind]
        data_rows = _table_rows(rows[1:], headers, mapped)
        if data_rows:
            found[kind].append((len(data_rows), data_rows))
    out: dict[str, list[dict[str, Any]]] = {key: [] for key in FINANCIAL_STATEMENT_KEYS}
    for key, candidates in found.items():
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        out[key] = candidates[0][1][:_MAX_ROWS]
    return out


def _classify_headers(headers: list[str]) -> str | None:
    h = set(headers)
    if "フリーCF" in h or "営業CF" in h:
        return "cashflow"
    if "自己資本比率" in h and "総資産" in h:
        return "balance"
    if "売上高" in h and "最終益" in h and "発表日" in h:
        if "修正方向" in h or "前年比" in h or "対上期進捗率" in h or "売上営業損益率" in h:
            return None
        return "income"
    return None


def _table_rows(
    rows,
    headers: list[str],
    mapped: dict[str, str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tr in rows:
        cells = _row_texts(tr)
        if not cells or len(cells) < 3:
            continue
        blob = "".join(cells)
        if "株探プレミアム" in blob:
            continue
        record: dict[str, Any] = {}
        for index, header in enumerate(headers):
            key = mapped.get(header)
            if not key or index >= len(cells):
                continue
            record[key] = _cell_value(cells[index], key=key)
        period = str(record.get("period") or "")
        if not period or _SKIP_PERIOD.search(period) or not _is_full_year(period):
            continue
        record["period"] = _display_period(period)
        out.append(record)
    return out


def _row_texts(tr) -> list[str]:
    cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
    return [" ".join(cell.get_text(" ", strip=True).split()) for cell in cells]


def _norm_header(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", value)


def _clean_period(raw: str) -> str:
    value = unicodedata.normalize("NFKC", raw or "")
    value = _PREFIX.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def _is_full_year(period: str) -> bool:
    cleaned = _clean_period(period)
    if "-" in cleaned:
        return False
    return bool(_FULL_YEAR.search(cleaned.replace(" ", "")))


def _display_period(period: str) -> str:
    return format_finance_period(period) or _clean_period(period)


def format_finance_period(value: Any) -> str | None:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text or text in _EMPTY_CELLS:
        return None
    text = _PREFIX.sub("", text)
    text = re.sub(r"^予\s*", "", text).strip()
    match = re.search(r"(20\d{2})[.\-/年](\d{1,2})", text)
    if match:
        return f"{match.group(1)}.{int(match.group(2)):02d}"
    return text or None


def period_sort_key(period: str | None) -> tuple[int, int]:
    text = format_finance_period(period) or ""
    match = re.match(r"^(20\d{2})\.(\d{2})$", text)
    if match:
        return (int(match.group(1)), int(match.group(2)))
    return (0, 0)


def format_finance_release_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text in _EMPTY_CELLS:
        return None
    match = _YY_DATE.match(text) or re.match(
        r"^(?:20)?(\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$",
        text,
    )
    if match:
        return f"{match.group(1)}/{int(match.group(2)):02d}/{int(match.group(3)):02d}"
    return text


def _cell_value(raw: str, *, key: str) -> str | None:
    text = (raw or "").strip()
    if key == "period":
        return text
    if key == "released_at":
        return format_finance_release_date(text)
    if text in _EMPTY_CELLS:
        return None
    return text


def _fetch_html(url: str) -> str:
    try:
        client = get_http_client()
        resp = client.get(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en;q=0.8",
            },
            timeout=_FETCH_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning("株探财务页 HTTP %s: %s", resp.status_code, url)
            return ""
        return resp.text or ""
    except Exception as exc:
        logger.warning("株探财务页抓取失败 %s: %s", url, exc)
        return ""
