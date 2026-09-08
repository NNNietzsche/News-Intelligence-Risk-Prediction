"""Yahoo Finance 公开市场行情接入。

该模块只读取 Yahoo Finance 的公开行情，并将最新成功结果缓存到本地数据库。
它不替代新闻采集，也不使用或推断任何行内数据。Modified by DingJiaye, 2026-09-08.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
import logging
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.database.models import MarketQuoteSnapshot

logger = logging.getLogger(__name__)


MARKET_INSTRUMENTS: tuple[dict[str, str], ...] = (
    # 主流指数
    {"symbol": "^GSPC", "name": "标普 500", "group": "index", "fred_series": "SP500"},
    {"symbol": "^IXIC", "name": "纳斯达克综合指数", "group": "index", "fred_series": "NASDAQCOM"},
    {"symbol": "^DJI", "name": "道琼斯工业指数", "group": "index", "fred_series": "DJIA"},
    {"symbol": "^N225", "name": "日经 225", "group": "index", "fred_series": "NIKKEI225"},
    # 大宗商品
    {"symbol": "CL=F", "name": "WTI 原油", "group": "commodity", "fred_series": "DCOILWTICO"},
    {"symbol": "BZ=F", "name": "布伦特原油", "group": "commodity", "fred_series": "DCOILBRENTEU"},
    # 核心汇率（采用 FRED 公开日线）；扩展品种可在此清单配置 Yahoo 回退。
    {"symbol": "USDJPY=X", "name": "美元 / 日元", "group": "fx", "fred_series": "DEXJPUS"},
    {"symbol": "USDCNY=X", "name": "美元 / 人民币", "group": "fx", "fred_series": "DEXCHUS"},
)

GROUP_LABELS = {"index": "主流指数", "commodity": "大宗商品", "fx": "主要汇率"}


def yahoo_quote_url(symbol: str) -> str:
    return f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/"


def fred_series_url(series_id: str) -> str:
    return f"https://fred.stlouisfed.org/series/{quote(series_id, safe='')}"


def _fred_history(series_id: str, *, points: int = 30) -> list[tuple[datetime, float]]:
    """读取 FRED 公开 CSV，不需要 API key；只取最近有效观察值。"""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(series_id, safe='')}"
    request = Request(url, headers={"User-Agent": "InfoRisk-MacroMonitor/1.0"})
    with urlopen(request, timeout=20) as response:  # nosec B310 - 固定可信 FRED HTTPS 域名
        body = response.read().decode("utf-8-sig")
    rows = csv.DictReader(StringIO(body))
    observations: list[tuple[datetime, float]] = []
    for row in rows:
        raw_value = (row.get(series_id) or "").strip()
        value = _safe_float(raw_value)
        raw_date = (row.get("observation_date") or "").strip()
        if value is None or not raw_date:
            continue
        try:
            stamp = datetime.strptime(raw_date, "%Y-%m-%d")
        except ValueError:
            continue
        observations.append((stamp, value))
    if not observations:
        raise RuntimeError("FRED 未返回有效观察值")
    return observations[-points:]


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # 排除 NaN


def _row_payload(row: MarketQuoteSnapshot) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.display_name,
        "group": row.quote_group,
        "group_label": GROUP_LABELS.get(row.quote_group, row.quote_group),
        "price": row.price,
        "previous_close": row.previous_close,
        "change": row.change_value,
        "change_pct": row.change_pct,
        "currency": row.currency,
        "as_of": row.as_of.isoformat() if row.as_of else None,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "source_url": row.source_url,
    }


def latest_market_quotes(db: Session, *, max_age_hours: int | None = None) -> list[dict[str, Any]]:
    """按标的提取最新缓存；可限制允许的缓存时效。"""
    rows = (
        db.query(MarketQuoteSnapshot)
        .order_by(MarketQuoteSnapshot.symbol.asc(), MarketQuoteSnapshot.fetched_at.desc(), MarketQuoteSnapshot.id.desc())
        .all()
    )
    threshold = datetime.utcnow() - timedelta(hours=max_age_hours) if max_age_hours else None
    latest: dict[str, MarketQuoteSnapshot] = {}
    for row in rows:
        if row.symbol in latest:
            continue
        if threshold and row.fetched_at < threshold:
            continue
        latest[row.symbol] = row
    order = {item["symbol"]: index for index, item in enumerate(MARKET_INSTRUMENTS)}
    return [_row_payload(row) for row in sorted(latest.values(), key=lambda row: order.get(row.symbol, 999))]


def grouped_latest_market_quotes(db: Session) -> dict[str, list[dict[str, Any]]]:
    groups = {key: [] for key in GROUP_LABELS}
    for item in latest_market_quotes(db):
        groups.setdefault(item["group"], []).append(item)
    return groups


def macro_fx_cross_rates(db: Session) -> dict[str, dict[str, Any]]:
    """由已核验美元兑日元、美元兑人民币日线推导人民币/日元交叉汇率。"""
    latest = {item["symbol"]: item for item in latest_market_quotes(db)}
    jpy = latest.get("USDJPY=X")
    cny = latest.get("USDCNY=X")
    if not jpy or not cny or not jpy.get("price") or not cny.get("price"):
        return {}
    cny_jpy = float(jpy["price"]) / float(cny["price"])
    previous_jpy = jpy.get("previous_close")
    previous_cny = cny.get("previous_close")
    previous_cross = None
    if previous_jpy and previous_cny:
        previous_cross = float(previous_jpy) / float(previous_cny)

    def payload(name: str, price: float) -> dict[str, Any]:
        previous = previous_cross if name == "人民币 / 日元" else (1 / previous_cross if previous_cross else None)
        change = price - previous if previous else None
        return {
            "name": name,
            "price": price,
            "previous_close": previous,
            "change_pct": (change / previous * 100) if change is not None and previous else None,
        }

    return {
        "cny_jpy": payload("人民币 / 日元", cny_jpy),
        "jpy_cny": payload("日元 / 人民币", 1 / cny_jpy),
    }


def market_chart_series(db: Session, *, days: int = 30) -> dict[str, Any]:
    """为宏观面板提供按起点=100归一化的近期开盘走势。"""
    primary_symbols = {
        "index": ("^GSPC", "^IXIC", "^DJI", "^N225"),
        "commodity": ("CL=F", "BZ=F"),
        "fx": ("USDJPY=X", "USDCNY=X"),
    }
    result: dict[str, Any] = {"groups": {key: [] for key in primary_symbols}}
    for group, symbols in primary_symbols.items():
        rows = (
            db.query(MarketQuoteSnapshot)
            .filter(MarketQuoteSnapshot.symbol.in_(symbols))
            .order_by(MarketQuoteSnapshot.symbol.asc(), MarketQuoteSnapshot.as_of.asc())
            .all()
        )
        values_by_symbol: dict[str, list[MarketQuoteSnapshot]] = {}
        for row in rows:
            values_by_symbol.setdefault(row.symbol, []).append(row)
        for symbol in symbols:
            series_rows = values_by_symbol.get(symbol, [])[-days:]
            if len(series_rows) < 2:
                continue
            base = series_rows[0].price
            if not base:
                continue
            result["groups"][group].append({
                "name": series_rows[-1].display_name,
                "points": [
                    {"date": row.as_of.strftime("%m-%d"), "value": round(row.price / base * 100, 2)}
                    for row in series_rows
                ],
            })
    return result


def _save_quote_snapshot(
    db: Session,
    *,
    instrument: dict[str, str],
    price: float,
    previous: float | None,
    as_of: datetime,
    source_url: str,
    fetched_at: datetime,
) -> None:
    existing = (
        db.query(MarketQuoteSnapshot)
        .filter(MarketQuoteSnapshot.symbol == instrument["symbol"], MarketQuoteSnapshot.as_of == as_of)
        .first()
    )
    if existing is None:
        existing = MarketQuoteSnapshot(symbol=instrument["symbol"], as_of=as_of)
        db.add(existing)
    change = price - previous if previous not in (None, 0) else None
    existing.display_name = instrument["name"]
    existing.quote_group = instrument["group"]
    existing.price = price
    existing.previous_close = previous
    existing.change_value = change
    existing.change_pct = (change / previous * 100) if change is not None and previous else None
    existing.currency = "JPY" if instrument["symbol"].endswith("JPY=X") else None
    existing.source_url = source_url
    existing.fetched_at = fetched_at


def refresh_market_quotes(db: Session) -> dict[str, Any]:
    """拉取并缓存公开报价；单标的失败不会清掉上一次成功快照。

    默认清单全部由 FRED 覆盖，避免 Yahoo 的公开端点限流拖慢行情面板。
    后续新增 FRED 未覆盖品种时，才自动使用 yfinance 作为回退。
    """
    updated: list[str] = []
    failed: list[dict[str, str]] = []
    now = datetime.utcnow()
    for instrument in MARKET_INSTRUMENTS:
        symbol = instrument["symbol"]
        try:
            fred_series = instrument.get("fred_series")
            if fred_series:
                observations = _fred_history(fred_series)
                for index, (as_of, price) in enumerate(observations):
                    previous = observations[index - 1][1] if index else None
                    _save_quote_snapshot(
                        db, instrument=instrument, price=price, previous=previous,
                        as_of=as_of, source_url=fred_series_url(fred_series), fetched_at=now,
                    )
                updated.append(symbol)
                continue

            # FRED 未覆盖的标的才回退到 Yahoo Finance；避免一次刷新触发大量 Yahoo 请求。
            try:
                import yfinance as yf
            except ImportError as exc:  # pragma: no cover - requirements 已锁定该依赖
                raise RuntimeError("未安装 yfinance，且该标的无 FRED 公开序列") from exc
            history = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
            if history is None or history.empty or "Close" not in history:
                raise RuntimeError("Yahoo Finance 未返回可用收盘价")
            closes = history["Close"].dropna()
            if closes.empty:
                raise RuntimeError("Yahoo Finance 收盘价为空")
            price = _safe_float(closes.iloc[-1])
            previous = _safe_float(closes.iloc[-2]) if len(closes) >= 2 else None
            if price is None:
                raise RuntimeError("Yahoo Finance 返回无效价格")
            stamp = closes.index[-1]
            as_of = stamp.to_pydatetime().replace(tzinfo=None) if hasattr(stamp, "to_pydatetime") else now
            _save_quote_snapshot(
                db, instrument=instrument, price=price, previous=previous,
                as_of=as_of, source_url=yahoo_quote_url(symbol), fetched_at=now,
            )
            updated.append(symbol)
        except Exception as exc:  # Yahoo 对个别 ticker 的可用性会变动
            logger.warning("Yahoo Finance 行情获取失败 %s: %s", symbol, exc)
            failed.append({"symbol": symbol, "error": str(exc)[:180]})
    db.commit()
    return {
        "updated_count": len(updated),
        "failed_count": len(failed),
        "updated": updated,
        "failed": failed,
        "quotes": latest_market_quotes(db),
        "provider": "FRED public CSV（核心）/ Yahoo Finance（扩展回退）",
        "fetched_at": now.replace(tzinfo=timezone.utc).isoformat(),
    }
