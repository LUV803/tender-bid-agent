"""LLM client used by the bid-writing agents.

It shares provider configuration with extraction but keeps its own prompts and
JSON-repair loop.  All transient errors are retried a bounded number of times;
authentication and permission failures are marked fatal so a long writing run
does not fail section by section.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import Settings
from ..extraction.llm import parse_json_object
from .errors import BidWriteError

MAX_JSON_CHARS = 200_000


@dataclass(slots=True)
class LLMCallRecord:
    purpose: str
    kind: str
    attempts: int
    chars: int
    duration_seconds: float
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "kind": self.kind,
            "attempts": self.attempts,
            "chars": self.chars,
            "duration_seconds": round(self.duration_seconds, 2),
            "error": self.error,
        }


class BidLLM(Protocol):
    """Minimal interface implemented by the real client and offline fakes."""

    calls: int

    def complete_text(self, system: str, user: str, *, purpose: str = "") -> str: ...

    def complete_json(self, system: str, user: str, *, purpose: str = "") -> dict[str, Any]: ...


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


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


class OpenAIBidLLM:
    """OpenAI-compatible chat client for DeepSeek and Zhipu."""

    def __init__(self, settings: Settings) -> None:
        provider = settings.llm_provider.lower()
        if provider not in {"deepseek", "zhipu"}:
            raise BidWriteError(
                f"不支持的 LLM_PROVIDER={provider!r}，可选 deepseek 或 zhipu",
                fatal=True,
            )
        if not settings.llm_configured:
            raise BidWriteError(
                f"未配置 {provider} 的 API key，无法生成投标文件", fatal=True
            )
        from openai import OpenAI

        self.settings = settings
        self.provider = provider
        self.model = settings.llm_model
        self.records: list[LLMCallRecord] = []
        self._client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
            max_retries=0,
        )

    @property
    def calls(self) -> int:
        return len(self.records)

    def _max_tokens(self) -> int:
        if self.provider == "zhipu":
            return min(self.settings.llm_max_tokens, 8192)
        return self.settings.llm_max_tokens

    def _retry_backoff(self, attempt: int) -> None:
        time.sleep(min(self.settings.mineru_retry_backoff ** attempt, 15.0))

    def _create(self, messages: list[dict[str, str]], *, json_mode: bool) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self._max_tokens(),
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise BidWriteError("模型响应没有 choices")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        content = _message_text(getattr(choice, "message", None))
        if finish_reason == "length":
            raise BidWriteError(
                "模型输出被 max_tokens 截断，请缩短章节篇幅后重试"
            )
        if not content.strip():
            raise BidWriteError("模型返回空内容")
        return content

    def complete_text(self, system: str, user: str, *, purpose: str = "") -> str:
        started = time.perf_counter()
        max_attempts = max(2, self.settings.llm_max_retries + 1)
        attempts = 0
        last_error: BidWriteError | None = None
        while attempts < max_attempts:
            attempts += 1
            try:
                content = self._create(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    json_mode=False,
                )
                self.records.append(
                    LLMCallRecord(
                        purpose=purpose,
                        kind="text",
                        attempts=attempts,
                        chars=len(content),
                        duration_seconds=time.perf_counter() - started,
                    )
                )
                return _strip_fence(content)
            except BidWriteError as exc:
                last_error = exc
                if exc.fatal:
                    raise
                if attempts >= max_attempts:
                    break
                self._retry_backoff(attempts)
                continue
            except Exception as exc:  # noqa: BLE001 - normalized below
                last_error = BidWriteError(f"LLM 调用失败：{exc}")
                if attempts >= max_attempts:
                    break
                self._retry_backoff(attempts)
        assert last_error is not None
        self.records.append(
            LLMCallRecord(
                purpose=purpose,
                kind="text",
                attempts=attempts,
                chars=0,
                duration_seconds=time.perf_counter() - started,
                error=str(last_error)[:500],
            )
        )
        raise last_error

    def complete_json(self, system: str, user: str, *, purpose: str = "") -> dict[str, Any]:
        started = time.perf_counter()
        max_attempts = max(3, self.settings.llm_max_retries + 1)
        attempts = 0
        mode = "json"
        raw = ""
        last_error: BidWriteError | None = None
        while attempts < max_attempts:
            attempts += 1
            if mode == "repair":
                repair_user = (
                    "把下面这段模型输出修正为一个合法 JSON 对象。只输出 JSON，不要解释。\n"
                    "如果内容被截断，保留能够确定的字段。\n\n"
                    f"{raw[:MAX_JSON_CHARS]}"
                )
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": repair_user},
                ]
                json_mode = False
            else:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
                json_mode = mode == "json"
            try:
                raw = self._create(messages, json_mode=json_mode)
            except BidWriteError as exc:
                last_error = exc
                if exc.fatal:
                    raise
                if json_mode and "请求被拒绝" in str(exc):
                    mode = "plain"
                    continue
                if attempts >= max_attempts:
                    break
                self._retry_backoff(attempts)
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = BidWriteError(f"LLM 调用失败：{exc}")
                if attempts >= max_attempts:
                    break
                self._retry_backoff(attempts)
                continue

            try:
                data = parse_json_object(raw)
            except Exception as exc:  # noqa: BLE001 - malformed JSON is expected
                last_error = BidWriteError(f"模型输出不是合法 JSON：{exc}")
                if mode == "json":
                    mode = "plain"
                elif mode == "plain":
                    mode = "repair"
                else:
                    break
                continue
            self.records.append(
                LLMCallRecord(
                    purpose=purpose,
                    kind="json",
                    attempts=attempts,
                    chars=len(raw),
                    duration_seconds=time.perf_counter() - started,
                )
            )
            return data

        assert last_error is not None
        self.records.append(
            LLMCallRecord(
                purpose=purpose,
                kind="json",
                attempts=attempts,
                chars=len(raw),
                duration_seconds=time.perf_counter() - started,
                error=str(last_error)[:500],
            )
        )
        raise last_error
