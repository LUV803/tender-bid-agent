"""Chapter-aware chunking for retrieval.

Chunks target ~450 characters (hard max 700) with 80 characters of overlap.
Page markers are stripped from the embedded text but retained as metadata so
citations can point back to PDF pages.  Table-of-contents pages are skipped.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from typing import Any

from ..config import Settings
from ..models import Chunk, Section, TocEntry, now_iso
from ..parsing.structure import is_chapter_title

PAGE_BLOCK_RE = re.compile(
    r"<!--\s*page:\s*(\d+)\s*-->([\s\S]*?)(?=<!--\s*page:\s*\d+\s*-->|$)"
)
BLANK_SEPARATOR_RE = re.compile(r"\n\s*\n")
SENTENCE_END_RE = re.compile(r"[。；！？]")

COLLECTION_TENDER_DOCUMENT = "tender_document"
COLLECTION_TENDER_ANNOUNCEMENT = "tender_announcement"
SCHEMA_VERSION = "1.0"


def collection_for(doc_type: str) -> str:
    if doc_type == "announcement":
        return COLLECTION_TENDER_ANNOUNCEMENT
    return COLLECTION_TENDER_DOCUMENT


def _page_text(
    text: str, default_page: int, toc_pages: set[int]
) -> tuple[str, list[tuple[int, int]]]:
    """Return (plain text, [(offset, page)]), dropping TOC pages."""
    if not text:
        return "", []
    matches = list(PAGE_BLOCK_RE.finditer(text))
    if not matches:
        body = text.strip()
        return (body, [(0, default_page)]) if body else ("", [])

    pieces: list[str] = []
    page_map: list[tuple[int, int]] = []
    offset = 0

    def add(content: str, page: int) -> None:
        nonlocal offset
        content = content.strip()
        if not content:
            return
        if pieces:
            offset += 2
        page_map.append((offset, page))
        pieces.append(content)
        offset += len(content)

    prefix = text[: matches[0].start()].strip()
    if prefix:
        add(prefix, default_page)
    for match in matches:
        page = int(match.group(1))
        if page in toc_pages:
            continue
        add(match.group(2), page)
    return "\n\n".join(pieces), page_map


def _page_at(page_map: list[tuple[int, int]], offset: int, fallback: int) -> int:
    if not page_map:
        return fallback
    starts = [item[0] for item in page_map]
    index = bisect_right(starts, offset) - 1
    if index < 0:
        return page_map[0][1]
    return page_map[index][1]


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    offset = 0
    for token in re.split(r"(\n\s*\n)", text):
        if token and not BLANK_SEPARATOR_RE.fullmatch(token) and token.strip():
            spans.append((offset, offset + len(token)))
        offset += len(token)
    return spans


def _split_long_span(
    text: str, start: int, end: int, hard_max: int
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > hard_max:
        window_end = cursor + hard_max
        window = text[cursor:window_end]
        cut = -1
        for match in SENTENCE_END_RE.finditer(window):
            if match.end() > 0:
                cut = cursor + match.end()
        if cut <= cursor:
            newline = text.rfind("\n", cursor + hard_max // 2, window_end)
            cut = newline + 1 if newline > cursor else window_end
        spans.append((cursor, cut))
        cursor = cut
    if cursor < end:
        spans.append((cursor, end))
    return spans


def _pack_spans(
    text: str, spans: list[tuple[int, int]], target: int, hard_max: int
) -> list[tuple[int, int]]:
    atomic: list[tuple[int, int]] = []
    for start, end in spans:
        if end - start <= hard_max:
            atomic.append((start, end))
        else:
            atomic.extend(_split_long_span(text, start, end, hard_max))

    if not atomic:
        return []
    packed: list[tuple[int, int]] = []
    current_start, current_end = atomic[0]
    for start, end in atomic[1:]:
        current_len = current_end - current_start
        combined_len = end - current_start
        if (current_len >= target and combined_len > target) or combined_len > hard_max:
            packed.append((current_start, current_end))
            current_start, current_end = start, end
        else:
            current_end = end
    packed.append((current_start, current_end))
    return packed


def _with_overlap(
    text: str, spans: list[tuple[int, int]], overlap: int
) -> list[tuple[int, int]]:
    if overlap <= 0 or len(spans) <= 1:
        return spans
    result = [spans[0]]
    for position, (start, end) in enumerate(spans[1:], start=1):
        previous_start, previous_end = spans[position - 1]
        candidate = previous_end - overlap
        newline = text.find("\n", candidate, previous_end)
        if newline != -1:
            candidate = newline + 1
        else:
            sentence = max(
                text.rfind("。", candidate, previous_end),
                text.rfind("；", candidate, previous_end),
            )
            if sentence != -1:
                candidate = sentence + 1
        candidate = max(candidate, previous_start)
        if candidate < start:
            start = candidate
        if text[start:end].strip():
            result.append((start, end))
    return result


def _strip_span(text: str, start: int, end: int) -> tuple[int, int]:
    raw = text[start:end]
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw) - len(raw.rstrip())
    return start + leading, end - trailing


def _section_chunks(
    *,
    section: Section,
    toc_pages: set[int],
    settings: Settings,
    manifest: dict[str, Any],
    created_at: str,
) -> list[Chunk]:
    body, page_map = _page_text(section.text, section.page_start or 1, toc_pages)
    if len(body.strip()) < 20:
        return []
    spans = _pack_spans(
        body,
        _paragraph_spans(body),
        max(120, settings.chunk_target),
        max(settings.chunk_target, settings.chunk_hard_max),
    )
    spans = _with_overlap(body, spans, settings.chunk_overlap)

    context = " > ".join([*section.path, section.title]) if section.title else " > ".join(section.path)
    if not context:
        context = "正文"
    chapter = next(
        (item for item in section.path if is_chapter_title(item)),
        "",
    )
    if not chapter:
        chapter = (
            section.title
            if is_chapter_title(section.title)
            else (section.path[-1] if section.path else section.title)
        )
    section_path = " > ".join([*section.path, section.title]) if section.title else " > ".join(section.path)
    chunks: list[Chunk] = []
    for start, end in spans:
        start, end = _strip_span(body, start, end)
        if end - start < 20:
            continue
        text_body = body[start:end]
        document = f"【{context}】\n{text_body}"
        page_start = _page_at(page_map, start, section.page_start or 1)
        page_end = _page_at(page_map, max(start, end - 1), page_start)
        content_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
        metadata: dict[str, Any] = {
            "doc_id": str(manifest.get("doc_id", "")),
            "doc_type": str(manifest.get("doc_type", "")),
            "source_name": str(manifest.get("source_name", "")),
            "source_sha256": str(manifest.get("source_sha256", "")),
            "parser": str(manifest.get("parser", "")),
            "section_no": str(section.number or ""),
            "section_title": str(section.title or ""),
            "section_path": section_path,
            "chapter": str(chapter or ""),
            "level": int(section.level or 0),
            "page_start": int(page_start),
            "page_end": int(page_end),
            "page_hint": f"p{page_start}" if page_start == page_end else f"p{page_start}-p{page_end}",
            "char_count": int(len(text_body)),
            "content_sha256": content_hash,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": int(settings.embedding_dimensions),
            "created_at": created_at,
            "schema_version": SCHEMA_VERSION,
        }
        chunks.append(Chunk(id="", text=document, metadata=metadata))
    return chunks


def _markdown_chunks(
    *,
    markdown: str,
    manifest: dict[str, Any],
    toc_pages: set[int],
    settings: Settings,
    created_at: str,
) -> list[Chunk]:
    body, page_map = _page_text(markdown, 1, toc_pages)
    if len(body.strip()) < 20:
        return []
    spans = _pack_spans(
        body,
        _paragraph_spans(body),
        max(120, settings.chunk_target),
        max(settings.chunk_target, settings.chunk_hard_max),
    )
    spans = _with_overlap(body, spans, settings.chunk_overlap)
    chunks: list[Chunk] = []
    for start, end in spans:
        start, end = _strip_span(body, start, end)
        if end - start < 20:
            continue
        text_body = body[start:end]
        document = f"【全文】\n{text_body}"
        page_start = _page_at(page_map, start, 1)
        page_end = _page_at(page_map, max(start, end - 1), page_start)
        content_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
        metadata: dict[str, Any] = {
            "doc_id": str(manifest.get("doc_id", "")),
            "doc_type": str(manifest.get("doc_type", "")),
            "source_name": str(manifest.get("source_name", "")),
            "source_sha256": str(manifest.get("source_sha256", "")),
            "parser": str(manifest.get("parser", "")),
            "section_no": "",
            "section_title": "",
            "section_path": "",
            "chapter": "全文",
            "level": 0,
            "page_start": int(page_start),
            "page_end": int(page_end),
            "page_hint": f"p{page_start}" if page_start == page_end else f"p{page_start}-p{page_end}",
            "char_count": int(len(text_body)),
            "content_sha256": content_hash,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": int(settings.embedding_dimensions),
            "created_at": created_at,
            "schema_version": SCHEMA_VERSION,
        }
        chunks.append(Chunk(id="", text=document, metadata=metadata))
    return chunks


def build_chunks(
    manifest: dict[str, Any],
    sections: list[Section],
    entries: list[TocEntry],
    markdown: str,
    settings: Settings,
) -> list[Chunk]:
    """Build de-duplicated retrieval chunks in document order."""
    del entries  # entries are optional for section-aware documents
    toc_pages = {int(page) for page in (manifest.get("toc_pages") or [])}
    created_at = now_iso()
    collected: list[Chunk] = []

    for section in sections:
        if not section.text.strip():
            continue
        collected.extend(
            _section_chunks(
                section=section,
                toc_pages=toc_pages,
                settings=settings,
                manifest=manifest,
                created_at=created_at,
            )
        )

    if not collected:
        collected = _markdown_chunks(
            markdown=markdown,
            manifest=manifest,
            toc_pages=toc_pages,
            settings=settings,
            created_at=created_at,
        )

    deduped: list[Chunk] = []
    seen: set[str] = set()
    for chunk in collected:
        fingerprint = chunk.metadata.get("content_sha256", "")
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(chunk)

    doc_id = str(manifest.get("doc_id", ""))
    total = len(deduped)
    for index, chunk in enumerate(deduped, start=1):
        chunk.id = f"{doc_id}:{index:05d}"
        chunk.metadata["chunk_index"] = index
        chunk.metadata["chunk_total"] = total
    return deduped
