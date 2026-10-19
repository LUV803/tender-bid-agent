"""Chunk, embed and index a parsed tender document into Chroma."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from ..config import Settings, get_settings
from ..models import now_iso
from ..parsing.pipeline import load_manifest, load_sections
from ..storage import hydrate_document, mirror_index
from .chunking import build_chunks, collection_for
from .embeddings import ZhipuEmbedder
from .errors import IngestError
from .store import ChromaStore

Progress = Callable[[str], None] | None
DEFAULT_VERIFY_QUERY = "评标办法 评分标准 资格审查 投标文件格式"


def index_manifest_path(doc_id: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.parsed_dir / doc_id / "index_manifest.json"


def load_index_manifest(
    doc_id: str, settings: Settings | None = None
) -> dict[str, Any] | None:
    settings = settings or get_settings()
    path = index_manifest_path(doc_id, settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _validate_doc_id(doc_id: str) -> None:
    if (
        not doc_id
        or doc_id in {".", ".."}
        or "/" in doc_id
        or "\\" in doc_id
        or Path(doc_id).name != doc_id
    ):
        raise IngestError(f"非法的 doc_id：{doc_id!r}")


def _resolve_markdown(
    manifest: dict[str, Any], doc_dir: Path, settings: Settings
) -> Path:
    value = manifest.get("markdown_path") or ""
    path = Path(value) if value else doc_dir / "content.md"
    if not path.is_absolute():
        path = settings.project_root / path
    if not path.is_file():
        fallback = doc_dir / "content.md"
        if fallback.is_file():
            return fallback
        raise IngestError(f"找不到 Markdown 正文：{path}")
    return path


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _hit_summary(hit: dict[str, Any]) -> dict[str, Any]:
    metadata = hit.get("metadata") or {}
    document = (hit.get("document") or "").replace("\n", " ")
    return {
        "id": hit.get("id"),
        "section_title": metadata.get("section_title", ""),
        "section_path": metadata.get("section_path", ""),
        "page_hint": metadata.get("page_hint", ""),
        "distance": hit.get("distance"),
        "excerpt": document[:180],
    }


def run_ingest(
    doc_id: str,
    *,
    verify: bool = True,
    verify_query: str | None = None,
    progress: Progress = None,
) -> dict[str, Any]:
    """Rebuild all chunks for one document (idempotent per doc_id)."""
    _validate_doc_id(doc_id)
    settings = get_settings()
    settings.ensure_directories()
    hydrate_document(doc_id, settings)
    manifest = load_manifest(doc_id, settings)
    sections, entries, _ = load_sections(doc_id, settings)
    doc_dir = settings.parsed_dir / doc_id
    markdown_path = _resolve_markdown(manifest, doc_dir, settings)
    markdown = markdown_path.read_text(encoding="utf-8")

    notify = progress or (lambda _message: None)
    chunks = build_chunks(manifest, sections, entries, markdown, settings)
    if not chunks:
        raise IngestError("没有生成任何检索分块")

    doc_type = str(manifest.get("doc_type", ""))
    collection_name = collection_for(doc_type)
    warnings: list[str] = []
    if collection_name == "tender_document" and doc_type not in {
        "tender_file",
        "tender_document",
    }:
        warnings.append(f"doc_type={doc_type!r}，默认写入 tender_document")

    notify(
        f"分块：{len(chunks)} 个；目标 {settings.chunk_target} 字，上限 "
        f"{settings.chunk_hard_max} 字，重叠 {settings.chunk_overlap} 字"
    )
    started = time.monotonic()
    embedder = ZhipuEmbedder(settings)
    store = ChromaStore(settings)

    vectors: list[list[float]] = []
    embedding_batch = max(1, settings.embedding_batch_size)
    for start in range(0, len(chunks), embedding_batch):
        batch = chunks[start : start + embedding_batch]
        vectors.extend(embedder.embed_documents([chunk.text for chunk in batch]))
        notify(
            f"embedding {min(start + len(batch), len(chunks))}/{len(chunks)}"
        )

    store.delete_doc_everywhere(doc_id)
    write_batch = max(1, settings.ingest_batch_size)
    for start in range(0, len(chunks), write_batch):
        batch_chunks = chunks[start : start + write_batch]
        batch_vectors = vectors[start : start + write_batch]
        store.upsert(collection_name, batch_chunks, batch_vectors)
        notify(
            f"写入 Chroma {min(start + len(batch_chunks), len(chunks))}/{len(chunks)}"
        )

    stored_chunks = store.doc_chunk_count(collection_name, doc_id)
    if stored_chunks != len(chunks):
        warnings.append(
            f"Chroma 中的分块数 {stored_chunks} 与预期 {len(chunks)} 不一致"
        )

    verification: dict[str, Any] | None = None
    if verify:
        query = (verify_query or DEFAULT_VERIFY_QUERY).strip()
        if query:
            try:
                query_vector = embedder.embed_query(query)
                hits = store.query(
                    collection_name,
                    query_vector,
                    doc_id=doc_id,
                    n_results=5,
                )
                verification = {
                    "ok": bool(hits),
                    "query": query,
                    "hits": [_hit_summary(hit) for hit in hits],
                }
                if not hits:
                    warnings.append("检索自检没有返回结果")
            except IngestError as exc:
                verification = {"ok": False, "query": query, "error": str(exc)}
                warnings.append(f"检索自检失败：{exc}")

    elapsed = round(time.monotonic() - started, 2)
    index_manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "doc_id": doc_id,
        "doc_type": doc_type,
        "collection": collection_name,
        "source_name": manifest.get("source_name", ""),
        "source_sha256": manifest.get("source_sha256", ""),
        "parser": manifest.get("parser", ""),
        "chunks": len(chunks),
        "stored_chunks": stored_chunks,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "embedding_calls": embedder.calls,
        "chunk_target": settings.chunk_target,
        "chunk_hard_max": settings.chunk_hard_max,
        "chunk_overlap": settings.chunk_overlap,
        "created_at": now_iso(),
        "elapsed_seconds": elapsed,
        "verification": verification,
        "warnings": warnings,
    }
    _write_json_atomic(index_manifest_path(doc_id, settings), index_manifest)
    mirror_index(doc_id, settings)
    notify(
        f"完成：{len(chunks)} 个分块写入 {collection_name}；"
        f"embedding 调用 {embedder.calls} 次；耗时 {elapsed}s"
    )
    return index_manifest


def inspect_document(
    doc_id: str,
    *,
    query: str | None = None,
    top_k: int = 5,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return collection stats and, optionally, retrieval hits."""
    _validate_doc_id(doc_id)
    settings = settings or get_settings()
    hydrate_document(doc_id, settings)
    manifest = load_manifest(doc_id, settings)
    index_manifest = load_index_manifest(doc_id, settings) or {}
    collection_name = str(
        index_manifest.get("collection")
        or collection_for(str(manifest.get("doc_type", "")))
    )
    store = ChromaStore(settings)
    count = store.doc_chunk_count(collection_name, doc_id)
    hits: list[dict[str, Any]] = []
    if query:
        embedder = ZhipuEmbedder(settings)
        vector = embedder.embed_query(query)
        hits = store.query(
            collection_name,
            vector,
            doc_id=doc_id,
            n_results=max(1, top_k),
        )
    return {
        "doc_id": doc_id,
        "doc_type": manifest.get("doc_type", ""),
        "source_name": manifest.get("source_name", ""),
        "collection": collection_name,
        "chunks": count,
        "index_manifest": index_manifest,
        "hits": hits,
    }
