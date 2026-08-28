"""皆无评级时，调用 LLM（openai SDK / DeepSeek）生成合理解释。"""

from __future__ import annotations

import logging
from typing import Optional

from intl_ratings.config import IntlRatingsEnv, get_env
from intl_ratings.llm_client import LlmClient
from intl_ratings.logging_utils import RawResponseStore
from intl_ratings.models import EntityMapping

logger = logging.getLogger(__name__)

REASON_SYSTEM = """你是信用分析助手。当发行体在穆迪/标普/惠誉均无独立主体评级（NR）时，用一句中文说明合理原因。
若为离岸 SPV，优先使用格式：该实体为[母公司名称]设立的离岸融资SPV，无独立主体评级，共享母公司信用资质
只输出 JSON：{"reason": "..."}。不要编造具体评级符号。"""


class NoRatingReasonGenerator:
    def __init__(
        self,
        env: Optional[IntlRatingsEnv] = None,
        raw_store: Optional[RawResponseStore] = None,
        timeout: int = 60,
        llm: Optional[LlmClient] = None,
    ) -> None:
        self.env = env or get_env()
        self.raw_store = raw_store
        self.llm = llm or LlmClient(
            env=self.env,
            raw_store=raw_store,
            timeout=timeout,
            temperature=0.1,
        )

    def generate(self, mapping: EntityMapping) -> str:
        if not self.llm.available:
            return self._fallback(mapping)

        user = (
            f"发行体: {mapping.issuer_name}\n"
            f"母公司: {mapping.parent_name or '未知'}\n"
            f"是否离岸SPV: {mapping.is_offshore_spv}\n"
            f"担保人: {mapping.guarantor_name or '无'}\n"
            f"备注: {mapping.notes or '无'}"
        )
        parsed = self.llm.chat_json(
            REASON_SYSTEM,
            user,
            issuer=mapping.issuer_name,
            source="no_rating_reason_llm",
        )
        reason = str(parsed.get("reason") or "").strip()
        return reason or self._fallback(mapping)

    @staticmethod
    def _fallback(mapping: EntityMapping) -> str:
        parent = mapping.parent_name or mapping.guarantor_name
        if mapping.is_offshore_spv and parent:
            return f"该实体为{parent}设立的离岸融资SPV，无独立主体评级，共享母公司信用资质"
        if parent:
            return f"暂未检索到独立公开主体评级，信用资质或与{parent}关联，待官方披露核实"
        return "暂未获公开评级信息，可能未主动邀评或仅存在非公开评级，待补充官方披露"
