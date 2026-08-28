"""将已核验资讯整理为审慎、可追溯的风险推导链。"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any


_CATEGORY_PATHS = {
    "司法/行政监管": "需核对事件是否触发整改、罚款、召回或经营限制；这些要求可能直接增加合规与处置支出，并影响相关产品或地区的正常运营。",
    "金融与经营数据": "应结合已披露金额、期限和业务范围，评估其对收入确认、利润率、融资成本、到期偿债与经营现金流的具体影响。",
    "供应链/关联方监控": "应核对受影响的供应商、原料、地区与合同安排；若无法替代，可能推高采购和物流成本、延迟交付或放大交易对手集中度。",
    "公开舆情": "应先核实报道所指主体、事件范围和官方回应；若事实被确认且持续传播，可能增加客户沟通、品牌维护和管理层处置成本。",
}
_LEVEL_JUDGEMENTS = {
    "极高": "当前信号强度较高，建议优先人工核验事实范围、资金或合规影响及处置进展。",
    "高": "当前信号需要优先复核，重点确认是否已有实质经营、合规或现金流影响。",
    "中": "当前为需关注信号；在缺少进一步证据前，不宜直接推定为重大负面事件。",
    "低": "当前证据有限或影响尚未证实，暂不构成重大负面判断。",
}
_CATEGORY_FOCUS = {
    "司法/行政监管": "关注监管文件、整改期限、处罚金额及受影响产品或地区。",
    "金融与经营数据": "关注后续业绩披露、融资到期安排、利润率与经营现金流变化。",
    "供应链/关联方监控": "关注供货连续性、关键关联方状态、替代来源与成本变化。",
    "公开舆情": "关注来源可靠性、主体是否被准确指向，以及官方回应与后续报道。",
}

# 常规业绩、任命、获奖、合作或一般项目动态不应强行展示“风险提示”。
# 只有材料本身出现明确的不利/不确定性信号，或 AI 给出了独立影响分析时才展示。
_RISK_SIGNAL_TERMS = (
    "风险", "亏损", "下滑", "下降", "暴跌", "跌", "违约", "债务", "再融资",
    "流动性", "处罚", "罚款", "调查", "诉讼", "召回", "整改", "合规",
    "制裁", "出口管制", "停产", "中断", "延迟", "取消", "撤出", "退出",
    "冲突", "紧张", "袭击", "封锁", "危机", "事故", "供应短缺", "成本上升",
    "裁员", "降级", "减记", "减值", "预警", "不确定",
)


def _clean(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit].rstrip("，,；;。.") if text else ""


def _same_message(left: str, right: str) -> bool:
    """避免把概况原样再放进“传导”栏。"""
    if not left or not right:
        return False
    compact_left = "".join(left.casefold().split())
    compact_right = "".join(right.casefold().split())
    if (
        compact_left == compact_right
        or compact_left in compact_right
        or compact_right in compact_left
    ):
        return True
    return SequenceMatcher(None, compact_left, compact_right).ratio() >= 0.72


def _fallback_transmission(category: str, title: str) -> str:
    """当模型没有给出独立影响分析时，按风险类型给出不重复的条件式路径。"""
    text = f"{category} {title}".casefold()
    if any(token in text for token in ("汇率", "美元", "日元", "比索", "货币")):
        return "若汇率波动持续或扩大，可能通过外币结算、进口成本、外币债务重估及套期保值成本传导至相关企业。"
    if any(token in text for token in ("原油", "油价", "天然气", "贵金属", "大宗商品")):
        return "若价格波动持续，可能通过采购成本、库存重估、运输费用或客户需求变化传导至相关企业。"
    if any(token in text for token in ("融资", "债券", "贷款", "再融资", "债务", "利率")):
        return "需核对融资金额、期限、利率、担保和资金用途；这些条件会影响利息负担、到期再融资压力及可动用流动性。"
    if any(token in text for token in ("监管", "处罚", "诉讼", "召回", "整改", "合规")):
        return "需核对主管机关、适用规则、整改期限和受影响产品或业务；处罚、整改或召回范围扩大时，可能增加直接处置成本并限制相关业务运行。"
    if any(token in text for token in ("供应", "原料", "交付", "物流", "供应商", "港口")):
        return "需核对受影响供应商、原料或物流节点及替代方案；若关键环节无法及时切换，可能造成采购成本上升、库存安全边际收窄或交付延迟。"
    if any(token in text for token in ("收购", "投资", "项目", "扩建", "建设", "数据中心")):
        return "需核对投资金额、资金来源、建设节点和预期回报；项目延期、成本超支或融资条件变化，可能增加资本开支压力并推迟收益兑现。"
    if any(token in text for token in ("任命", "辞任", "董事", "高管", "ceo", "cfo")):
        return "需核对职位交接安排、继任者和战略连续性；关键管理层变动若伴随业务调整，可能影响项目决策、客户关系或内部控制稳定性。"
    return _CATEGORY_PATHS.get(
        category,
        "现有材料未披露可量化的金额、范围或持续时间；应围绕事件涉及的业务、资金、合同与合规责任补充核验后再判断实际影响。",
    )


def should_show_risk_tip(
    *, title: str, summary: str, impact: str, category: str, level: str
) -> bool:
    """判断页面是否确有必要展示风险提示，避免中性新闻套用泛化推导。"""
    evidence = f"{title} {summary} {impact}".casefold()
    if any(term.casefold() in evidence for term in _RISK_SIGNAL_TERMS):
        return True
    # 独立的影响分析通常来自模型对正文的具体风险判断；重复标题/摘要的
    # 兜底文本不作为展示依据。
    if impact and not _same_message(impact, title) and not _same_message(impact, summary):
        return True
    return False


def build_risk_reasoning(
    *,
    title: Any,
    summary: Any = None,
    impact: Any = None,
    risk_level: Any = None,
    category: Any = None,
) -> dict[str, str | bool]:
    """只基于已存在的标题/摘要/影响分析补足展示结构，不创造新事实。"""
    category_text = _clean(category, 40) or "公开舆情"
    level = _clean(risk_level, 8) or "低"
    fact = _clean(summary) or f"公开来源披露：{_clean(title, 180) or '该事件'}。"
    summary_text = _clean(summary)
    impact_text = _clean(impact)
    fact = summary_text or f"公开来源披露：{_clean(title, 180) or '该事件'}。"
    title_text = _clean(title, 180)
    # 影响分析只有在与标题、概况都不同，确实补足传导关系时才展示。
    transmission = (
        impact_text
        if impact_text
        and not _same_message(summary_text, impact_text)
        and not _same_message(title_text, impact_text)
        else (
            f"本次披露涉及：{summary_text}。{_fallback_transmission(category_text, title_text)}"
            if summary_text and not _same_message(summary_text, title_text)
            else _fallback_transmission(category_text, title_text)
        )
    )
    return {
        "fact": fact,
        "transmission": transmission,
        "judgement": _LEVEL_JUDGEMENTS.get(level, _LEVEL_JUDGEMENTS["低"]),
        "focus": _CATEGORY_FOCUS.get(
            category_text,
            "关注官方原文、后续披露及是否出现可量化的经营、合规或现金流影响。",
        ),
        "show": should_show_risk_tip(
            title=title_text,
            summary=summary_text,
            impact=impact_text,
            category=category_text,
            level=level,
        ),
    }
