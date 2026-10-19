"""DeepSeek JSON completion with mode fallback and transient-error retries."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from .errors import ExtractError
from .prompts import SYSTEM_PROMPT, build_repair_prompt

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


@dataclass(slots=True)
class JsonCompletion:
    data: dict[str, Any]
    used_json_mode: bool


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse a JSON object from possibly chatty model output."""
    text = (content or "").strip()
    if not text:
        raise ExtractError("模型返回空内容")
    fence = _CODE_FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise ExtractError("模型输出中没有 JSON 对象")
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise ExtractError(f"模型输出不是合法 JSON：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise ExtractError("模型输出的 JSON 不是对象")
    return data


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


class JsonLLMClient:
    """Small wrapper that keeps retry/fallback behavior in one place.

    Both providers expose an OpenAI-compatible chat endpoint.  Zhipu is used
    here as a chat fallback because the configured DeepSeek key may be invalid;
    embeddings still go through zai-sdk.
    """

    def __init__(self, settings: Settings) -> None:
        provider = settings.llm_provider.lower()
        if provider not in {"deepseek", "zhipu"}:
            raise ExtractError(
                f"不支持的 LLM_PROVIDER={provider!r}，可选 deepseek 或 zhipu"
            )
        if not settings.llm_configured:
            raise ExtractError(
                f"未配置 {provider} 的 API key，无法进行结构化抽取"
            )
        from openai import OpenAI

        self.settings = settings
        self.provider = provider
        self.model = settings.llm_model
        self.calls = 0
        self._client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
            max_retries=0,
        )

    def _create(self, messages: list[dict[str, str]], *, json_mode: bool) -> str:
        max_tokens = self.settings.llm_max_tokens
        if self.provider == "zhipu":
            # GLM chat models currently cap output well below the DeepSeek
            # default; the schema per unit does not need more than this.
            max_tokens = min(max_tokens, 8192)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(**kwargs)
        self.calls += 1
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ExtractError("模型响应没有 choices")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        content = _message_text(getattr(choice, "message", None))
        if finish_reason == "length":
            raise ExtractError("模型输出被 max_tokens 截断", truncated=True)
        if not content.strip():
            raise ExtractError("模型返回空内容")
        return content

    def complete_json(self, user_prompt: str) -> JsonCompletion:
        """Run one unit, falling back from JSON mode to plain/repair output."""
        max_attempts = max(2, self.settings.llm_max_retries + 1)
        mode = "json"
        attempts = 0
        last_error: ExtractError | None = None
        raw_content = ""

        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            PermissionDeniedError,
            RateLimitError,
        )

        while attempts < max_attempts:
            attempts += 1
            if mode == "repair":
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_repair_prompt(raw_content)},
                ]
                use_json_mode = False
            else:
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ]
                use_json_mode = mode == "json"

            try:
                raw_content = self._create(messages, json_mode=use_json_mode)
            except ExtractError:
                raise
            except (AuthenticationError, PermissionDeniedError) as exc:
                raise ExtractError(f"LLM 认证/权限失败：{exc}", fatal=True) from exc
            except BadRequestError as exc:
                last_error = ExtractError(f"LLM 请求被拒绝：{exc}")
                if use_json_mode:
                    mode = "plain"
                    continue
                raise ExtractError(str(last_error), fatal=True) from exc
            except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
                last_error = ExtractError(f"LLM 网络/限流错误：{exc}")
                if attempts >= max_attempts:
                    raise last_error from exc
                time.sleep(min(self.settings.mineru_retry_backoff ** attempts, 15.0))
                continue
            except APIStatusError as exc:
                last_error = ExtractError(f"LLM 服务错误：{exc.status_code} {exc}")
                if exc.status_code in {400, 401, 403, 404, 422}:
                    raise ExtractError(str(last_error), fatal=True) from exc
                if exc.status_code >= 500 and attempts < max_attempts:
                    time.sleep(min(self.settings.mineru_retry_backoff ** attempts, 15.0))
                    continue
                raise last_error from exc

            try:
                data = parse_json_object(raw_content)
                return JsonCompletion(data=data, used_json_mode=use_json_mode)
            except ExtractError as exc:
                last_error = exc
                if use_json_mode:
                    mode = "plain"
                elif mode == "plain":
                    mode = "repair"
                else:
                    raise

        raise last_error or ExtractError("LLM 抽取失败")


# Backwards-compatible alias for earlier imports.
DeepSeekJsonClient = JsonLLMClient
