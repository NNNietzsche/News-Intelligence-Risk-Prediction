"""OpenAI 兼容 SDK 封装（DeepSeek / OpenAI）。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from intl_ratings.config import IntlRatingsEnv, get_env
from intl_ratings.logging_utils import RawResponseStore

logger = logging.getLogger(__name__)


class LlmClient:
    def __init__(
        self,
        env: Optional[IntlRatingsEnv] = None,
        raw_store: Optional[RawResponseStore] = None,
        timeout: int = 60,
        temperature: float = 0.1,
    ) -> None:
        self.env = env or get_env()
        self.raw_store = raw_store
        self.timeout = timeout
        self.temperature = temperature

    @property
    def available(self) -> bool:
        return bool(self.env.deepseek_api_key)

    def chat_json(
        self,
        system_prompt: str,
        user_content: str,
        *,
        issuer: str = "",
        source: str = "llm",
    ) -> dict[str, Any]:
        if not self.available:
            return {}
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("未安装 openai SDK，回退 httpx 直连")
            return self._httpx_json(system_prompt, user_content, issuer=issuer, source=source)

        client = OpenAI(
            api_key=self.env.deepseek_api_key,
            base_url=f"{self.env.deepseek_api_base_url.rstrip('/')}/v1",
            timeout=self.timeout,
        )
        try:
            resp = client.chat.completions.create(
                model=self.env.deepseek_model or "deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=self.temperature,
                response_format={"type": "json_object"},
            )
            raw = resp.model_dump() if hasattr(resp, "model_dump") else resp
            if self.raw_store and issuer:
                self.raw_store.save(issuer, source, raw)
            content = resp.choices[0].message.content or "{}"
            return self._parse_json(content)
        except Exception as exc:
            logger.warning("LLM 调用失败: %s", exc)
            return {}

    def _httpx_json(
        self,
        system_prompt: str,
        user_content: str,
        *,
        issuer: str,
        source: str,
    ) -> dict[str, Any]:
        import httpx

        body = {
            "model": self.env.deepseek_model or "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }
        url = f"{self.env.deepseek_api_base_url.rstrip('/')}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.env.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            if self.raw_store and issuer:
                self.raw_store.save(issuer, source, data)
            content = (
                data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            )
            return self._parse_json(content)
        except Exception as exc:
            logger.warning("LLM httpx 失败: %s", exc)
            return {}

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        text = (content or "").strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
