"""Chroma HTTP storage with explicit embeddings (no server-side model)."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..models import Chunk
from .chunking import COLLECTION_TENDER_ANNOUNCEMENT, COLLECTION_TENDER_DOCUMENT
from .errors import IngestError


class ChromaStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        try:
            import chromadb

            self.client = chromadb.HttpClient(
                host=settings.chroma_host, port=settings.chroma_port
            )
            self.client.heartbeat()
        except Exception as exc:
            raise IngestError(
                "无法连接 Chroma；请先运行 "
                "'uv run chroma run --path ./chroma_db' "
                f"({settings.chroma_host}:{settings.chroma_port})：{exc}"
            ) from exc

    def collection(self, name: str):
        try:
            return self.client.get_or_create_collection(
                name=name,
                metadata={
                    "hnsw:space": "cosine",
                    "description": "Bidding tender chunks (explicit embeddings)",
                },
            )
        except Exception as exc:
            raise IngestError(f"无法打开 Chroma collection {name}：{exc}") from exc

    def delete_doc_everywhere(self, doc_id: str) -> None:
        for name in (COLLECTION_TENDER_DOCUMENT, COLLECTION_TENDER_ANNOUNCEMENT):
            try:
                collection = self.client.get_collection(name)
            except Exception:
                continue
            try:
                collection.delete(where={"doc_id": doc_id})
            except Exception as exc:
                raise IngestError(
                    f"删除 collection {name} 中旧分块失败：{exc}"
                ) from exc

    def upsert(
        self,
        collection_name: str,
        chunks: list[Chunk],
        vectors: list[list[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise IngestError("分块数量与向量数量不一致")
        if not chunks:
            return
        collection = self.collection(collection_name)
        try:
            collection.upsert(
                ids=[chunk.id for chunk in chunks],
                documents=[chunk.text for chunk in chunks],
                metadatas=[chunk.metadata for chunk in chunks],
                embeddings=vectors,
            )
        except Exception as exc:
            raise IngestError(f"写入 Chroma 失败：{exc}") from exc

    def doc_chunk_count(self, collection_name: str, doc_id: str) -> int:
        collection = self.collection(collection_name)
        try:
            result = collection.get(where={"doc_id": doc_id}, include=[])
            return len(result.get("ids") or [])
        except Exception:
            try:
                result = collection.get(where={"doc_id": doc_id})
                return len(result.get("ids") or [])
            except Exception as exc:
                raise IngestError(f"读取 Chroma 分块失败：{exc}") from exc

    def query(
        self,
        collection_name: str,
        embedding: list[float],
        *,
        doc_id: str | None = None,
        n_results: int = 5,
    ) -> list[dict[str, Any]]:
        collection = self.collection(collection_name)
        arguments: dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results": max(1, n_results),
            "include": ["documents", "metadatas", "distances"],
        }
        if doc_id:
            arguments["where"] = {"doc_id": doc_id}
        try:
            result = collection.query(**arguments)
        except Exception as exc:
            raise IngestError(f"Chroma 查询失败：{exc}") from exc

        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits: list[dict[str, Any]] = []
        for index, item_id in enumerate(ids):
            hits.append(
                {
                    "id": item_id,
                    "document": documents[index] if index < len(documents) else "",
                    "metadata": metadatas[index] if index < len(metadatas) else {},
                    "distance": distances[index] if index < len(distances) else None,
                }
            )
        return hits
