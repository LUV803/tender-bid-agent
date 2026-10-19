"""Zhipu embedding-3 wrapper (2048 dimensions by default)."""

from __future__ import annotations

import time

from ..config import Settings
from .errors import IngestError


class ZhipuEmbedder:
    def __init__(self, settings: Settings) -> None:
        if not settings.embedding_configured:
            raise IngestError("未配置 ZHIPU_API_KEY，无法生成向量")
        from zai import ZhipuAiClient

        self.settings = settings
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self.calls = 0
        self._client = ZhipuAiClient(
            api_key=settings.zhipu_api_key,
            timeout=settings.llm_timeout,
            max_retries=0,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        max_attempts = max(2, self.settings.llm_max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.embeddings.create(
                    model=self.model,
                    input=texts,
                    dimensions=self.dimensions,
                )
                self.calls += 1
                data = list(getattr(response, "data", None) or [])
                if data and hasattr(data[0], "index"):
                    data.sort(key=lambda item: getattr(item, "index", 0) or 0)
                vectors = [
                    [float(value) for value in item.embedding]
                    for item in data
                ]
                if len(vectors) != len(texts):
                    raise IngestError(
                        f"embedding 返回数量不匹配：{len(vectors)} != {len(texts)}"
                    )
                if any(len(vector) != self.dimensions for vector in vectors):
                    raise IngestError(
                        f"embedding 维度不是预期的 {self.dimensions}"
                    )
                return vectors
            except IngestError:
                raise
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                if any(token in message for token in ("401", "403", "api key", "authentication")):
                    raise IngestError(f"智谱 embedding 认证失败：{exc}") from exc
                if attempt >= max_attempts:
                    break
                time.sleep(min(2.0 ** attempt, 15.0))
        raise IngestError(f"智谱 embedding 调用失败：{last_error}") from last_error

    def embed_query(self, query: str) -> list[float]:
        vectors = self.embed_documents([query])
        if not vectors:
            raise IngestError("查询向量为空")
        return vectors[0]
