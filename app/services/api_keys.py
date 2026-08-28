"""API 密钥校验工具。"""

from __future__ import annotations


def is_placeholder_key(key: str | None) -> bool:
    if not key or not key.strip():
        return True
    normalized = key.strip().lower()
    placeholders = ("your_", "sk-your", "mk-your", "change-me", "placeholder")
    return any(normalized.startswith(p) for p in placeholders)


def validate_mita_key(key: str | None) -> None:
    if is_placeholder_key(key):
        raise RuntimeError("未配置有效的 MITA_API_KEY，请在 .env 中填写秘塔 API 密钥")


def validate_deepseek_key(key: str | None) -> None:
    if is_placeholder_key(key):
        raise RuntimeError("未配置有效的 DEEPSEEK_API_KEY，请在 .env 中填写 DeepSeek API 密钥")


def validate_gemini_key(key: str | None) -> None:
    if is_placeholder_key(key):
        raise RuntimeError("未配置有效的 GEMINI_API_KEY，请在 .env 中填写 Gemini API 密钥")
