"""精确判定公式（与业务口径一致）。"""

from __future__ import annotations

from typing import Optional

from intl_ratings.models import NEED_REVIEW, NO, NO_PUBLIC_TRADE, YES


def judge_loss_from_net_income(net_income: Optional[float]) -> str:
    """归母净利润：<0 → 是；>=0 → 否；缺失 → 需人工复核。"""
    if net_income is None:
        return NEED_REVIEW
    return YES if float(net_income) < 0 else NO


def calc_month_change_pct(price_now: float, price_30d_ago: float) -> Optional[float]:
    """
    月环比涨跌幅百分比：
    ((当前收盘价 - 30日前收盘价) / 30日前收盘价) * 100
    下跌为负值。
    """
    if price_30d_ago is None or price_now is None:
        return None
    base = float(price_30d_ago)
    if base == 0:
        return None
    return (float(price_now) - base) / base * 100.0


def judge_bond_price_drop(
    change_pct: Optional[float],
    *,
    threshold_pct: float = -5.0,
    no_public_label: str = NO_PUBLIC_TRADE,
) -> str:
    """
    若 change_pct <= -5% → 是；> -5% → 否；无数据 → 无公开交易数据。
    threshold_pct 默认 -5.0（与「跌幅超过 5%」一致）。
    """
    if change_pct is None:
        return no_public_label
    return YES if float(change_pct) <= float(threshold_pct) else NO
