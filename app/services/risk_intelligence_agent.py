"""风险情报智能体：统一检索规划、结果处理与页面发布前的任务上下文。

本模块不把模型输出当作事实来源。智能体只负责规划检索与编排工具；
网页正文、官方/RSS 信源和既有结构化校验仍是页面内容的唯一证据基础。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import MODULE_CODES, get_settings
from app.services.api_keys import is_placeholder_key
from app.services.deepseek_analyzer import DeepSeekAnalyzer
from app.services.llm_cache import get_cached_items, material_hash, set_cached_items

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntelligenceSearchPlan:
    """一次任务的可审计检索计划。"""

    queries: list[dict[str, Any]]
    provider: str
    cached: bool = False
    fallback_reason: str = ""

    def audit_payload(self) -> dict[str, Any]:
        return {
            "mode": "risk_intelligence_agent",
            "provider": self.provider,
            "cached": self.cached,
            "query_count": len(self.queries),
            "fallback_reason": self.fallback_reason or None,
        }


class RiskIntelligenceAgent:
    """将静态检索配置转为 AI 可规划、可缓存、可回退的任务。"""

    PROMPT_VERSION = "risk-intelligence-agent-plan-v1"

    def __init__(self, db: Session, *, deepseek: DeepSeekAnalyzer | None = None) -> None:
        self.db = db
        self.deepseek = deepseek or DeepSeekAnalyzer()
        self.settings = get_settings()

    def plan_search(
        self,
        *,
        module_code: str,
        report_date: str,
        window_hours: int,
        fallback_queries: list[dict[str, Any]],
    ) -> IntelligenceSearchPlan:
        """返回 Agent 规划后的检索词；失败时稳定回退到受控基准查询。"""
        baseline = [dict(item) for item in fallback_queries if item.get("query")]
        if not baseline:
            return IntelligenceSearchPlan([], "rules", fallback_reason="无可用基准查询")
        if not bool(getattr(self.settings, "intelligence_agent_enabled", True)):
            return IntelligenceSearchPlan(baseline, "rules", fallback_reason="智能体规划已关闭")
        if is_placeholder_key(getattr(self.settings, "deepseek_api_key", None)):
            return IntelligenceSearchPlan(baseline, "rules", fallback_reason="DeepSeek 未配置")

        cache_basis = json.dumps(
            {
                "version": self.PROMPT_VERSION,
                "module": module_code.upper(),
                "date": report_date,
                "hours": int(window_hours),
                "baseline": baseline,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        key = material_hash(cache_basis, module_code=module_code, source="agent-plan")
        cached = get_cached_items(
            self.db,
            material_key=key,
            max_age_hours=int(getattr(self.settings, "intelligence_agent_plan_cache_hours", 12) or 12),
        )
        if cached:
            return IntelligenceSearchPlan(self._merge_with_baseline(cached, baseline), "deepseek", cached=True)

        try:
            rows = self.deepseek.plan_risk_intelligence_queries(
                module_code=module_code,
                module_name=MODULE_CODES.get(module_code.upper(), module_code),
                report_date=report_date,
                window_hours=window_hours,
                baseline_queries=baseline,
            )
            planned = self._merge_with_baseline(rows, baseline)
            set_cached_items(
                self.db,
                material_key=key,
                module_code=module_code,
                source="agent-plan",
                items=planned,
            )
            return IntelligenceSearchPlan(planned, "deepseek")
        except Exception as exc:  # 模型/额度异常不应中断真实信源采集。
            reason = str(exc).replace("\n", " ")[:160]
            logger.warning("风险情报智能体规划失败，使用基准检索：%s", reason)
            return IntelligenceSearchPlan(baseline, "rules", fallback_reason=reason)

    @staticmethod
    def _merge_with_baseline(
        candidate_rows: list[dict[str, Any]], baseline: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """只接受模型对既有任务的措辞优化，元数据与检索边界由系统控制。"""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, base in enumerate(baseline):
            row = candidate_rows[index] if index < len(candidate_rows) and isinstance(candidate_rows[index], dict) else {}
            query = " ".join(str(row.get("query") or base.get("query") or "").split())
            if not query or query in seen:
                query = str(base.get("query") or "").strip()
            if not query or query in seen:
                continue
            seen.add(query)
            out.append({**base, "query": query, "agent_planned": True})
        return out or baseline

