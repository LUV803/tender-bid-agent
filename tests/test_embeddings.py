"""Zhipu embedder tests with an in-memory response object."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from bidding.config import get_settings
from bidding.indexing.embeddings import ZhipuEmbedder
from bidding.indexing.errors import IngestError


def _embedder(dimensions: int = 2) -> tuple[ZhipuEmbedder, list[dict[str, Any]]]:
    settings = replace(
        get_settings(),
        zhipu_api_key="test-key",
        embedding_model="embedding-3",
        embedding_dimensions=dimensions,
        llm_max_retries=0,
    )
    embedder = object.__new__(ZhipuEmbedder)
    embedder.settings = settings
    embedder.model = settings.embedding_model
    embedder.dimensions = dimensions
    embedder.calls = 0
    requests: list[dict[str, Any]] = []
    return embedder, requests


def _response(items: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(data=items)


def test_embed_documents_sorts_by_index_and_counts_calls() -> None:
    embedder, requests = _embedder()

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        return _response(
            [
                SimpleNamespace(index=1, embedding=[0.3, 0.4]),
                SimpleNamespace(index=0, embedding=[0.1, 0.2]),
            ]
        )

    embedder._client = SimpleNamespace(
        embeddings=SimpleNamespace(create=fake_create)
    )

    assert embedder.embed_documents(["第一段", "第二段"]) == [
        [0.1, 0.2],
        [0.3, 0.4],
    ]
    assert embedder.calls == 1
    assert requests[0]["input"] == ["第一段", "第二段"]
    assert requests[0]["dimensions"] == 2
    assert embedder.embed_documents([]) == []


def test_embed_documents_validates_count_and_dimensions() -> None:
    embedder, _ = _embedder()

    def mismatch_count(**kwargs: Any) -> SimpleNamespace:
        return _response([SimpleNamespace(index=0, embedding=[0.1, 0.2])])

    embedder._client = SimpleNamespace(
        embeddings=SimpleNamespace(create=mismatch_count)
    )
    with pytest.raises(IngestError, match="数量不匹配"):
        embedder.embed_documents(["第一段", "第二段"])

    def mismatch_dimension(**kwargs: Any) -> SimpleNamespace:
        return _response([SimpleNamespace(index=0, embedding=[0.1])])

    embedder._client = SimpleNamespace(
        embeddings=SimpleNamespace(create=mismatch_dimension)
    )
    with pytest.raises(IngestError, match="维度"):
        embedder.embed_documents(["第一段"])
