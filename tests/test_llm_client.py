"""LLM client retry/fallback tests with an in-memory fake endpoint."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from bidding.config import get_settings
from bidding.extraction.errors import ExtractError
from bidding.extraction.llm import JsonLLMClient


class _FakeCompletions:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        spec = self._responses.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=spec.get("finish_reason", "stop"),
                    message=SimpleNamespace(content=spec.get("content", "")),
                )
            ]
        )


def _client(responses: list[dict[str, Any]], *, retries: int = 1) -> tuple[JsonLLMClient, _FakeCompletions]:
    settings = replace(
        get_settings(),
        llm_provider="zhipu",
        zhipu_api_key="test-key",
        zhipu_chat_model="glm-test",
        llm_max_retries=retries,
    )
    completions = _FakeCompletions(responses)
    client = object.__new__(JsonLLMClient)
    client.settings = settings
    client.provider = "zhipu"
    client.model = settings.llm_model
    client.calls = 0
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return client, completions


def test_json_mode_falls_back_to_plain_text_on_malformed_output() -> None:
    client, completions = _client(
        [
            {"content": "这不是 JSON"},
            {"content": '{"project_name": "测试项目"}'},
        ]
    )

    completion = client.complete_json("抽取项目信息")

    assert completion.data == {"project_name": "测试项目"}
    assert completion.used_json_mode is False
    assert client.calls == 2
    assert completions.requests[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in completions.requests[1]
    assert completions.requests[0]["max_tokens"] <= 8192


def test_truncated_output_is_reported_without_retrying() -> None:
    client, completions = _client(
        [{"content": "半截输出", "finish_reason": "length"}]
    )

    with pytest.raises(ExtractError) as exc_info:
        client.complete_json("抽取项目信息")

    assert exc_info.value.truncated is True
    assert "截断" in str(exc_info.value)
    assert len(completions.requests) == 1


def test_repair_pass_runs_after_two_malformed_replies() -> None:
    client, completions = _client(
        [
            {"content": "不是 JSON"},
            {"content": "仍然不是 JSON"},
            {"content": '{"evaluation_method": "综合评估法"}'},
        ],
        retries=2,
    )

    completion = client.complete_json("抽取评标办法")

    assert completion.data == {"evaluation_method": "综合评估法"}
    assert completion.used_json_mode is False
    assert len(completions.requests) == 3
    repair_prompt = completions.requests[2]["messages"][1]["content"]
    assert "修复为合法 JSON" in repair_prompt
    assert "不是 JSON" in repair_prompt
