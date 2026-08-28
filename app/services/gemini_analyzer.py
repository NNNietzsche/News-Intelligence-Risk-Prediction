"""Gemini 结构化分析 API 封装（主体评估与行业分析共用）。"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from app.config import get_settings, resolve_gemini_model
from app.services.api_keys import validate_gemini_key
from app.services.deepseek_analyzer import (
    DeepSeekAnalyzer,
    LEGACY_INDUSTRY_ANALYSIS_PROMPT_GEMINI,
)
from app.services.http_client import get_http_client
from app.services.http_retry import with_retries

logger = logging.getLogger(__name__)


class GeminiAnalyzer(DeepSeekAnalyzer):
    """与 DeepSeekAnalyzer 同接口，底层改为 Gemini HTTP 调用。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.gemini_api_base_url).rstrip("/")
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.timeout = timeout or settings.request_timeout_seconds
        self.retry_attempts = int(getattr(settings, "network_retry_attempts", 3) or 3)
        self.retry_backoff = float(getattr(settings, "network_retry_backoff_seconds", 1.5) or 1.5)

    def analyze_industry(
        self,
        raw_text: str,
        industry_name: str,
        company_name: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """行业分析：用户侧字段与 DeepSeek 路径一致，仅系统提示按 Gemini 分节。"""
        user_content = (
            f"行业: {industry_name}\n"
            f"企业: {company_name or '未指定'}\n"
            f"附加上下文: {json.dumps(context or {}, ensure_ascii=False)}\n\n"
            f"原始材料:\n{raw_text}"
        )
        return self._chat_json_object(LEGACY_INDUSTRY_ANALYSIS_PROMPT_GEMINI, user_content)

    def generate_text(
        self,
        system_prompt: str,
        user_content: str,
        *,
        max_output_tokens: int = 400,
    ) -> str:
        """纯文本生成；最新消息摘要等场景不要走 JSON 约束。"""
        return self._generate_content(
            system_prompt,
            user_content,
            response_mime_type=None,
            max_output_tokens=max_output_tokens,
        )

    def _request_chat(self, system_prompt: str, user_content: str) -> str:
        return self._generate_content(
            system_prompt,
            user_content,
            response_mime_type="application/json",
        )

    def _generate_content(
        self,
        system_prompt: str,
        user_content: str,
        *,
        response_mime_type: str | None = "application/json",
        max_output_tokens: int | None = None,
    ) -> str:
        validate_gemini_key(self.api_key)
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        generation_config: dict[str, Any] = {"temperature": 0.2}
        if response_mime_type:
            generation_config["responseMimeType"] = response_mime_type
        if max_output_tokens:
            generation_config["maxOutputTokens"] = max_output_tokens
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": generation_config,
        }
        params = {"key": self.api_key}

        def _do_chat() -> dict[str, Any]:
            try:
                client = get_http_client()
                resp = client.post(url, params=params, json=body, timeout=self.timeout)
                resp.raise_for_status()
                try:
                    return resp.json()
                except ValueError as exc:
                    snippet = (resp.text or "")[:500]
                    logger.error("Gemini 返回非 JSON: %s", snippet)
                    raise RuntimeError(f"Gemini 响应解析失败: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                body_text = (exc.response.text or "")[:300]
                if exc.response.status_code in (408, 425, 429, 500, 502, 503, 504):
                    raise
                logger.error("Gemini HTTP %s: %s", exc.response.status_code, body_text)
                raise RuntimeError(
                    f"Gemini 分析失败 (HTTP {exc.response.status_code}): {body_text or exc}"
                ) from exc
            except httpx.HTTPError as exc:
                logger.error("Gemini 请求失败: %s", exc)
                raise RuntimeError(f"Gemini 分析失败: {exc}") from exc

        try:
            data = with_retries(
                _do_chat,
                attempts=self.retry_attempts,
                backoff_seconds=self.retry_backoff,
                label="Gemini",
            )
        except httpx.HTTPStatusError as exc:
            body_text = (exc.response.text or "")[:300]
            raise RuntimeError(
                f"Gemini 分析失败 (HTTP {exc.response.status_code}): {body_text or exc}"
            ) from exc

        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            if block_reason:
                raise RuntimeError(f"Gemini 内容被拦截: {block_reason}")
            raise RuntimeError("Gemini 未返回候选结果")

        parts = (candidates[0].get("content") or {}).get("parts") or []
        text_parts = [str(p.get("text") or "") for p in parts if p.get("text")]
        if not text_parts:
            raise RuntimeError("Gemini 返回空内容")
        return "".join(text_parts)


def gemini_for(task: str = "fast", **kwargs: Any) -> GeminiAnalyzer:
    """按任务选用 Gemini 模型，仍使用同一 GEMINI_API_KEY。"""
    kwargs.setdefault("model", resolve_gemini_model(task))
    return GeminiAnalyzer(**kwargs)
