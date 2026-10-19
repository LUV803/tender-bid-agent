"""Map-reduce structured extraction for a parsed tender document."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from ..config import Settings, get_settings
from ..models import Section, TocEntry, now_iso
from ..parsing.pipeline import load_sections
from ..parsing.structure import (
    chapter_indices,
    split_by_blank_lines,
    strip_page_markers,
    subtree_range,
    subtree_text,
)
from .errors import ExtractError
from .llm import JsonLLMClient
from .prompts import PROMPT_VERSION, build_user_prompt, route_focus
from .schema import (
    empty_extraction,
    ground_extraction,
    infer_evaluation_method,
    merge_extractions,
    normalize_extraction,
)

SCHEMA_VERSION = "1.0"
PAGE_BLOCK_RE = re.compile(
    r"<!--\s*page:\s*(\d+)\s*-->([\s\S]*?)(?=<!--\s*page:\s*\d+\s*-->|$)"
)
PAGE_MARKER_RE = re.compile(r"<!--\s*page:\s*\d+\s*-->")
Progress = Callable[[str], None] | None


@dataclass(slots=True)
class ExtractionUnit:
    index: int
    title: str
    path: list[str]
    text: str
    focus: str

    @property
    def breadcrumb(self) -> str:
        return " > ".join([*self.path, self.title]) if self.title else " > ".join(self.path)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def extraction_path(doc_id: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.parsed_dir / doc_id / "extraction.json"


def _validate_doc_id(doc_id: str) -> None:
    if (
        not doc_id
        or doc_id in {".", ".."}
        or "/" in doc_id
        or "\\" in doc_id
        or Path(doc_id).name != doc_id
    ):
        raise ExtractError(f"非法的 doc_id：{doc_id!r}")


def _without_toc_pages(text: str, toc_pages: list[int]) -> str:
    if not text or not toc_pages:
        return text
    blocked = {int(page) for page in toc_pages}

    def replace_block(match: re.Match[str]) -> str:
        return "" if int(match.group(1)) in blocked else match.group(0)

    return PAGE_BLOCK_RE.sub(replace_block, text)


def _visible_text(text: str) -> str:
    body, _ = strip_page_markers(text)
    return body.strip()


def _has_content(text: str) -> bool:
    return len(_visible_text(text)) >= 20


def _hard_split(text: str, limit: int) -> list[str]:
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        pieces.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining.strip():
        pieces.append(remaining)
    return pieces


def _pack_text(text: str, limit: int) -> list[str]:
    stripped = text.strip("\n")
    if not stripped:
        return []
    if len(stripped) <= limit:
        return [stripped]
    chunks: list[str] = []
    current = ""
    for paragraph in split_by_blank_lines(stripped):
        paragraph = paragraph.strip("\n")
        if not paragraph:
            continue
        if len(current) + len(paragraph) + 2 <= limit:
            current = f"{current}\n\n{paragraph}" if current else paragraph
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
        else:
            chunks.extend(_hard_split(paragraph, limit))
    if current.strip():
        chunks.append(current)
    return [chunk for chunk in chunks if _has_content(chunk)]


def _titled_chunks(
    text: str, title: str, path: list[str], limit: int
) -> list[tuple[str, list[str], str]]:
    chunks = _pack_text(text, limit)
    if len(chunks) <= 1:
        return [(title, path, chunk) for chunk in chunks]
    return [
        (f"{title}（{index}/{len(chunks)}）", path, chunk)
        for index, chunk in enumerate(chunks, start=1)
    ]


def _preamble_pieces(
    sections: list[Section], first_chapter: int, toc_pages: list[int], limit: int
) -> list[tuple[str, list[str], str]]:
    parts: list[str] = []
    for section in sections[:first_chapter]:
        body = _without_toc_pages(section.text, toc_pages)
        if _has_content(body):
            parts.append(f"【{section.title}】\n{body}" if section.title else body)
    if not parts:
        return []
    return _titled_chunks(
        "\n\n".join(parts), "封面与前置信息", [], limit
    )


def _entry_pieces(
    sections: list[Section],
    entries: list[TocEntry],
    index: int,
    toc_pages: list[int],
    limit: int,
    depth: int = 0,
) -> list[tuple[str, list[str], str]]:
    section = sections[index]
    full = _without_toc_pages(subtree_text(sections, entries, index), toc_pages)
    if not _has_content(full):
        return []
    if len(full) <= limit or depth >= 12:
        return _titled_chunks(full, section.title, section.path, limit)

    start, end = subtree_range(entries, index)
    deeper_levels = [
        entries[cursor].level
        for cursor in range(start + 1, end)
        if entries[cursor].level > section.level
    ]
    children: list[int] = []
    if deeper_levels:
        child_level = min(deeper_levels)
        children = [
            cursor
            for cursor in range(start + 1, end)
            if entries[cursor].level == child_level
        ]

    own = _without_toc_pages(section.text, toc_pages)
    pieces: list[tuple[str, list[str], str]] = []
    if _has_content(own):
        pieces.extend(_titled_chunks(own, section.title, section.path, limit))
    if children:
        for child in children:
            pieces.extend(
                _entry_pieces(sections, entries, child, toc_pages, limit, depth + 1)
            )
    elif not pieces:
        pieces.extend(_titled_chunks(full, section.title, section.path, limit))
    return pieces


def build_units(
    manifest: dict[str, Any],
    sections: list[Section],
    entries: list[TocEntry],
    markdown: str,
    settings: Settings,
) -> list[ExtractionUnit]:
    toc_pages = [int(page) for page in (manifest.get("toc_pages") or [])]
    limit = max(2000, settings.extract_max_chars)
    pieces: list[tuple[str, list[str], str]] = []
    chapters = chapter_indices(entries)

    if chapters and len(sections) >= len(entries):
        if chapters[0] > 0:
            pieces.extend(
                _preamble_pieces(sections, chapters[0], toc_pages, limit)
            )
        for chapter in chapters:
            section = sections[chapter]
            focus = route_focus(
                " > ".join([*section.path, section.title])
            )
            if focus == "contract":
                # Generic conditions are boilerplate: pack by size instead of
                # paying for one LLM call per clause.  Project-specific terms
                # still reach the model inside these chunks.
                full = _without_toc_pages(
                    subtree_text(sections, entries, chapter), toc_pages
                )
                pieces.extend(
                    _titled_chunks(full, section.title, section.path, limit)
                )
            else:
                pieces.extend(
                    _entry_pieces(sections, entries, chapter, toc_pages, limit)
                )

    if not pieces:
        pieces = _titled_chunks(
            _without_toc_pages(markdown, toc_pages), "全文", [], limit
        )

    units: list[ExtractionUnit] = []
    seen: set[str] = set()
    for title, path, text in pieces:
        if not _has_content(text):
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        focus = route_focus(" > ".join([*path, title]))
        units.append(
            ExtractionUnit(
                index=len(units) + 1,
                title=title or "未命名章节",
                path=list(path),
                text=text,
                focus=focus,
            )
        )
    return units


def _nearest_position(text: str, token: str, midpoint: int, radius: int) -> int:
    """Return the occurrence of ``token`` closest to ``midpoint``, or -1."""
    start = max(0, midpoint - radius)
    end = min(len(text), midpoint + radius)
    best = -1
    position = text.find(token, start)
    while position != -1 and position <= end:
        if best < 0 or abs(position - midpoint) < abs(best - midpoint):
            best = position
        position = text.find(token, position + 1)
    return best


def _nearest_page_marker(text: str, midpoint: int, radius: int) -> int:
    start = max(0, midpoint - radius)
    end = min(len(text), midpoint + radius)
    best = -1
    for match in PAGE_MARKER_RE.finditer(text, start, end):
        if best < 0 or abs(match.start() - midpoint) < abs(best - midpoint):
            best = match.start()
    return best


def _split_for_bisect(text: str) -> tuple[str, str] | None:
    midpoint = len(text) // 2
    # Page boundaries keep table rows intact; fall back to the closest
    # paragraph or sentence break when no marker is near the midpoint.
    cut = _nearest_page_marker(text, midpoint, 600)
    if cut <= 0:
        for token in ("\n\n", "\n", "。", "；", "！", "？"):
            position = _nearest_position(text, token, midpoint, 1200)
            if position > 0:
                cut = position + len(token)
                break
    if cut <= 0:
        cut = midpoint
    overlap = min(200, len(text) // 10)
    left = text[: cut + overlap].strip("\n")
    right = text[max(0, cut - overlap) :].strip("\n")
    if not _has_content(left) or not _has_content(right) or left == right:
        return None
    return left, right


class _ExtractionRunner:
    def __init__(
        self,
        settings: Settings,
        client: JsonLLMClient,
        progress: Progress = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.progress = progress
        self.warnings: list[str] = []

    def notify(self, message: str) -> None:
        if self.progress:
            self.progress(message)

    def extract_text(
        self, unit: ExtractionUnit, depth: int = 0
    ) -> tuple[dict[str, Any], bool]:
        prompt = build_user_prompt(
            title=unit.title,
            path=unit.path,
            text=unit.text,
            focus=unit.focus,
        )
        try:
            completion = self.client.complete_json(prompt)
            return normalize_extraction(completion.data), completion.used_json_mode
        except ExtractError as exc:
            if exc.fatal:
                raise
            # Retry truncated or malformed output on smaller halves.  Large
            # scoring tables often produce invalid JSON even when the reply
            # was not cut off, so bisection is not limited to truncation.
            if (
                (exc.truncated or len(unit.text) >= self.settings.extract_bisect_min_chars)
                and depth < self.settings.extract_bisect_max_depth
            ):
                halves = _split_for_bisect(unit.text)
                if halves:
                    reason = "输出截断" if exc.truncated else "输出异常"
                    self.notify(
                        f"  {reason}，二分重试（depth={depth + 1}）：{unit.title}"
                    )
                    left_text, right_text = halves
                    left = replace(unit, title=f"{unit.title}（上）", text=left_text)
                    right = replace(unit, title=f"{unit.title}（下）", text=right_text)
                    left_data, left_json = self.extract_text(left, depth + 1)
                    right_data, right_json = self.extract_text(right, depth + 1)
                    merged = merge_extractions(left_data, right_data)
                    return merged, left_json and right_json
            raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _cache_tag(manifest: dict[str, Any], settings: Settings) -> str:
    source_hash = str(manifest.get("source_sha256") or "")[:16]
    return (
        f"{PROMPT_VERSION}|{manifest.get('parser', '')}|{settings.llm_provider}|"
        f"{settings.llm_model}|{settings.extract_max_chars}|"
        f"{settings.extract_bisect_max_depth}|{source_hash}"
    )


def _prepare_cache(
    doc_dir: Path, tag: str, *, force: bool
) -> Path:
    parts_dir = doc_dir / "extraction_parts"
    if force and parts_dir.exists():
        shutil.rmtree(parts_dir)
    parts_dir.mkdir(parents=True, exist_ok=True)
    tag_file = parts_dir / "cache_tag.txt"
    current = ""
    try:
        current = tag_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if current != tag:
        for path in parts_dir.glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass
        tag_file.write_text(tag, encoding="utf-8")
    return parts_dir


def _cache_file(parts_dir: Path, unit: ExtractionUnit) -> Path:
    digest = unit.fingerprint()[:12]
    return parts_dir / f"{unit.index:03d}_{digest}.json"


def _load_cached_unit(
    parts_dir: Path, unit: ExtractionUnit
) -> tuple[dict[str, Any], bool] | None:
    digest = unit.fingerprint()
    for path in sorted(parts_dir.glob(f"{unit.index:03d}_*.json")):
        data = _read_json(path)
        if not isinstance(data, dict) or data.get("fingerprint") != digest:
            continue
        result = data.get("result")
        if isinstance(result, dict):
            return normalize_extraction(result), bool(data.get("used_json_mode", False))
    return None


def _store_cached_unit(
    parts_dir: Path, unit: ExtractionUnit, result: dict[str, Any], used_json: bool
) -> None:
    target = _cache_file(parts_dir, unit)
    _write_json_atomic(
        target,
        {
            "index": unit.index,
            "title": unit.title,
            "focus": unit.focus,
            "fingerprint": unit.fingerprint(),
            "used_json_mode": used_json,
            "result": result,
            "cached_at": now_iso(),
        },
    )
    for path in parts_dir.glob(f"{unit.index:03d}_*.json"):
        if path != target:
            try:
                path.unlink()
            except OSError:
                pass


def _resolve_markdown(manifest: dict[str, Any], doc_dir: Path, settings: Settings) -> Path:
    value = manifest.get("markdown_path") or ""
    path = Path(value) if value else doc_dir / "content.md"
    if not path.is_absolute():
        path = settings.project_root / path
    if not path.is_file():
        fallback = doc_dir / "content.md"
        if fallback.is_file():
            return fallback
        raise ExtractError(f"找不到 Markdown 正文：{path}")
    return path


def _cached_document(
    path: Path, cache_tag: str, parser: str
) -> dict[str, Any] | None:
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    meta = data.get("extraction_meta")
    if not isinstance(meta, dict):
        return None
    if meta.get("cache_tag") != cache_tag:
        return None
    if meta.get("parser") != parser:
        return None
    if meta.get("partial"):
        return None
    return data


def run_extract(
    doc_id: str,
    *,
    force: bool = False,
    unit_limit: int | None = None,
    progress: Progress = None,
) -> dict[str, Any]:
    """Extract structured tender facts from a parsed document.

    Results are cached per unit (keyed by content hash) so an interrupted run
    can resume without paying for completed units.
    """
    _validate_doc_id(doc_id)
    settings = get_settings()
    settings.ensure_directories()
    sections, entries, manifest = load_sections(doc_id, settings)
    doc_dir = settings.parsed_dir / doc_id
    markdown_path = _resolve_markdown(manifest, doc_dir, settings)
    markdown = markdown_path.read_text(encoding="utf-8")
    cache_tag = _cache_tag(manifest, settings)
    output_path = extraction_path(doc_id, settings)

    notify = progress or (lambda _message: None)

    if not force and unit_limit is None:
        cached = _cached_document(output_path, cache_tag, str(manifest.get("parser", "")))
        if cached is not None:
            notify(f"命中整份抽取缓存：{output_path}")
            return cached

    units = build_units(manifest, sections, entries, markdown, settings)
    if not units:
        raise ExtractError("没有可用于抽取的章节内容")
    partial = unit_limit is not None
    if unit_limit is not None:
        if unit_limit <= 0:
            raise ExtractError("--limit 必须大于 0")
        units = units[:unit_limit]
        notify(f"调试模式：仅处理前 {len(units)} 个单元，结果标记为 partial")

    parts_dir = _prepare_cache(doc_dir, cache_tag, force=force)
    client = JsonLLMClient(settings)
    runner = _ExtractionRunner(settings, client, progress)

    notify(
        f"抽取单元：{len(units)} 个；模型 {settings.llm_provider}/{settings.llm_model}；"
        f"提示词 {PROMPT_VERSION}"
    )
    result = empty_extraction()
    warnings: list[str] = []
    failed_units: list[dict[str, str]] = []
    cached_units = 0
    processed = 0
    all_json_mode = True
    started = time.monotonic()

    for position, unit in enumerate(units, start=1):
        cached_unit = None if force else _load_cached_unit(parts_dir, unit)
        if cached_unit is not None:
            data, used_json = cached_unit
            cached_units += 1
            processed += 1
            all_json_mode = all_json_mode and used_json
            merge_extractions(result, data)
            notify(f"[{position}/{len(units)}] 缓存 {unit.title}")
            continue

        notify(
            f"[{position}/{len(units)}] 抽取 {unit.title} "
            f"({unit.focus}, {len(unit.text)} 字)"
        )
        try:
            data, used_json = runner.extract_text(unit)
        except ExtractError as exc:
            if exc.fatal:
                raise ExtractError(
                    f"抽取中止（{unit.breadcrumb}）：{exc}"
                ) from exc
            failed_units.append({"title": unit.breadcrumb, "error": str(exc)})
            warning = f"单元抽取失败：{unit.breadcrumb}：{exc}"
            warnings.append(warning)
            notify(f"  失败：{exc}")
            continue

        processed += 1
        all_json_mode = all_json_mode and used_json
        merge_extractions(result, data)
        _store_cached_unit(parts_dir, unit, data, used_json)

    if failed_units and processed == 0:
        raise ExtractError("所有抽取单元均失败：" + failed_units[0]["error"])

    result["evaluation_method"] = infer_evaluation_method(
        [entry.title for entry in entries],
        result.get("evaluation_method", ""),
    )
    ungrounded = ground_extraction(result, markdown)
    elapsed = round(time.monotonic() - started, 2)
    partial = partial or bool(failed_units)
    if partial:
        warnings.append(
            "抽取结果不完整（调试限制或存在失败单元），请检查 failed_units"
        )

    extraction: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "doc_id": doc_id,
        "doc_type": manifest.get("doc_type", ""),
        "source_name": manifest.get("source_name", ""),
        "source_sha256": manifest.get("source_sha256", ""),
        "parser": manifest.get("parser", ""),
        **result,
        "ungrounded_fields": ungrounded,
        "extraction_meta": {
            "model": settings.llm_model,
            "provider": settings.llm_provider,
            "prompt_version": PROMPT_VERSION,
            "cache_tag": cache_tag,
            "parser": manifest.get("parser", ""),
            "source_sha256": manifest.get("source_sha256", ""),
            "generated_at": now_iso(),
            "elapsed_seconds": elapsed,
            "unit_count": len(units),
            "processed_units": processed,
            "cached_units": cached_units,
            "calls": client.calls,
            "failed_units": failed_units,
            "json_mode_used": all_json_mode,
            "partial": partial,
            "ungrounded_count": len(ungrounded),
            "warnings": [*warnings, *runner.warnings],
        },
    }
    _write_json_atomic(output_path, extraction)
    notify(f"写入 {output_path}")
    return extraction
