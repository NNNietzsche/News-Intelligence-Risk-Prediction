"""新闻汇总的银行风险标签与审慎等级规则。"""

from __future__ import annotations

import re
from dataclasses import dataclass

RISK_TYPES = (
    "信贷风险",
    "市场风险",
    "流动性与资产负债",
    "合规与反洗钱",
    "国别与地缘",
    "操作与网络安全",
    "治理与信息披露",
)

_KEYWORDS = {
    "信贷风险": ("违约", "重组", "评级", "展望", "坏账", "减值", "核销", "担保失效", "偿债", "现金流", "盈利预警"),
    "市场风险": ("利率", "汇率", "股指", "债价", "油价", "原油", "黄金", "贵金属", "大宗商品", "日元", "美元"),
    "流动性与资产负债": ("融资冻结", "流动性", "挤兑", "存款流失", "利差", "再融资", "债发", "期限错配", "融资成本"),
    "合规与反洗钱": ("制裁", "罚单", "罚款", "吊销", "许可", "反洗钱", "aml", "kyc", "出口管制", "监管整改", "制裁名单"),
    "国别与地缘": ("战争", "冲突", "航道", "港口", "管道", "能源供应中断", "资本管制", "国有化", "政权"),
    "操作与网络安全": ("网络攻击", "黑客", "系统故障", "支付清算", "结算中断", "服务中断", "营运停摆", "数据泄露"),
    "治理与信息披露": ("会计更正", "造假", "重大遗漏", "信息披露", "审计", "高管辞任", "董事辞任", "财务更正"),
}

_PRIORITY = ("合规与反洗钱", "信贷风险", "流动性与资产负债", "操作与网络安全", "治理与信息披露", "国别与地缘", "市场风险")
_POSITIVE_OR_ROUTINE = ("合作", "活动", "营销", "获奖", "任命", "推出", "增长", "上调", "利润增长", "例行声明")
_RUMOUR = ("传闻", "据悉", "消息人士", "未经证实", "或将", "可能", "拟")
_EXTREME = ("违约已", "已违约", "重组已", "已重组", "停牌", "挤兑", "融资冻结", "制裁生效", "吊销许可", "航道关闭", "支付清算中断")
_HIGH = ("盈利预警", "展望负面", "下调展望", "制裁宣布", "军事升级", "航道紧张", "大幅下跌", "重大系统故障")
_EXPLICIT_LOW_IMPACT = (
    "影响极低", "直接影响极低", "不构成直接风险", "不构成实质影响",
    "不构成重大负面", "通常不构成直接风险", "对银行风险影响极低",
)
_REFINANCE_WATCH_TERMS = (
    "贷款展期", "贷款延期", "债务展期", "债务延期", "再融资", "重组",
    "杠杆收购贷款", "银团贷款", "super senior loan",
)
_HIGH_REFINANCE_TERMS = (
    "杠杆收购贷款", "lbo loan", "leveraged buyout loan", "super senior loan",
    "超级优先贷款", "债务重组", "贷款", "融资", "收购融资",
)


@dataclass(frozen=True)
class NewsRiskAssessment:
    tags: tuple[str, ...]
    level: str


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.casefold() in text for term in terms)


def normalize_display_risk_level(
    *, title: str | None, summary: str | None, impact: str | None, level: str | None
) -> str:
    """对页面灯号做最终一致性校验，不让文字结论与等级相互矛盾。

    当 AI 的影响分析明确写明“影响极低”或“不构成直接风险”时，历史性、
    背景性事件不应继续以黄色/红色灯号提醒。已发生的高等级事实仍优先保留。
    Modified by DingJiaye: 2026-08-28.
    """
    normalized = str(level or "低").strip()
    if normalized not in {"低", "中", "高", "极高"}:
        normalized = "低"
    impact_text = str(impact or "").casefold()
    factual_text = " ".join(str(value or "") for value in (title, summary)).casefold()
    if _contains(impact_text, _EXPLICIT_LOW_IMPACT) and not _contains(factual_text, _EXTREME + _HIGH):
        return "低"
    # 贷款到期展期、杠杆收购贷款延期与再融资安排本身就是需要跟踪的
    # 流动性/资本结构信号；旧采集记录即使未写入等级，也不应静默显示为普通。
    # Modified by DingJiaye: 2026-08-28.
    english_refinance_signal = "loan" in factual_text and any(
        token in factual_text for token in ("extend", "extension", "maturity", "lbo")
    )
    # 贷款或收购融资的展期往往同时涉及到期偿债、重组和新增资金优先级，
    # 对银行属于可执行的融资结构风险，直接亮红灯。
    # Modified by DingJiaye: 2026-08-28.
    if _contains(factual_text, _HIGH_REFINANCE_TERMS) and (
        _contains(factual_text, ("展期", "延期", "延长", "到期", "extend", "extension", "maturity"))
    ):
        return "高"
    if _contains(factual_text, ("销售不佳", "销售下滑", "销量下滑")) and _contains(
        factual_text, ("成本飙升", "成本上升", "原材料成本")
    ):
        return "高"
    if normalized == "低" and (_contains(factual_text, _REFINANCE_WATCH_TERMS) or english_refinance_signal):
        return "中"
    return normalized


def assess_news_risk(*, title: str | None, summary: str | None, impact: str | None, stored_level: str | None = None) -> NewsRiskAssessment:
    """仅按已抓取内容打标签；主类型 1 个、辅类型最多 1 个。"""
    body = " ".join(str(value or "") for value in (title, summary, impact)).casefold()
    scores = {kind: sum(1 for term in terms if term.casefold() in body) for kind, terms in _KEYWORDS.items()}
    matched = [kind for kind, score in scores.items() if score]
    matched.sort(key=lambda kind: (-scores[kind], _PRIORITY.index(kind)))
    tags = tuple(matched[:2])

    # 无风险事实的普通资讯只标为低；不为凑标签硬贴类型。
    if not tags:
        return NewsRiskAssessment(tags=(), level="低")

    content_missing = not (summary or "").strip() or "正文未取得" in (summary or "")
    if _contains(body, _EXTREME) and not _contains(body, _RUMOUR):
        level = "极高"
    elif _contains(body, _HIGH) or ("制裁" in body and _contains(body, ("宣布", "公布", "实施"))):
        level = "高"
    elif _contains(body, _RUMOUR):
        level = "中"
    elif any(scores[kind] >= 1 for kind in tags):
        level = "中"
    else:
        level = "低"

    # 标题或正文缺失不能超过中；传闻最高高（当前无正文时已被上限进一步收窄）。
    if content_missing and level in {"高", "极高"}:
        level = "中"
    if _contains(body, _RUMOUR) and level == "极高":
        level = "高"
    # “宣布/发布”本身不是中性信号：例如“宣布制裁”仍须按合规风险处理。
    if _contains(body, _POSITIVE_OR_ROUTINE) and not _contains(body, _HIGH + _EXTREME):
        level = "低"
    return NewsRiskAssessment(
        tags=tags,
        level=normalize_display_risk_level(
            title=title,
            summary=summary,
            impact=impact,
            level=level,
        ),
    )
