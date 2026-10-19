"""Local PyMuPDF fallback parser.

Extracts the text layer, bookmarks and embedded images.  It intentionally
does not attempt OCR; scanned pages are reported and the caller can retry
with MinerU when an API key is available.
"""

from __future__ import annotations

import time
from pathlib import Path

import pymupdf

from ..models import ImageRecord, ParseResult
from .errors import ParseError
from .structure import (
    assign_pages_to_entries,
    clean_page_text,
    compose_sections,
    detect_toc_pages,
    read_toc,
)

DEGRADED_WARNING = "本地解析仅还原文本层，表格与图纸版面可能不完整"


def _extract_images(
    doc: pymupdf.Document, images_dir: Path, warnings: list[str]
) -> list[ImageRecord]:
    images_dir.mkdir(parents=True, exist_ok=True)
    records: list[ImageRecord] = []
    seen_xrefs: set[int] = set()

    for page_no, page in enumerate(doc, start=1):
        try:
            image_infos = page.get_images(full=True)
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(f"第 {page_no} 页图片枚举失败：{exc}")
            continue

        for info in image_infos:
            try:
                xref = int(info[0])
                width = int(info[2])
                height = int(info[3])
            except (TypeError, ValueError, IndexError):
                continue
            if xref in seen_xrefs:
                continue
            try:
                extracted = doc.extract_image(xref)
                data = extracted.get("image")
                if not data:
                    continue
                ext = str(extracted.get("ext") or "png").lower()
                filename = f"p{page_no:03d}_x{xref}.{ext}"
                (images_dir / filename).write_bytes(data)
                records.append(
                    ImageRecord(
                        file=filename,
                        page=page_no,
                        xref=xref,
                        width=int(extracted.get("width") or width),
                        height=int(extracted.get("height") or height),
                        ext=ext,
                    )
                )
                seen_xrefs.add(xref)
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append(f"第 {page_no} 页图片 xref={info[0]} 提取失败：{exc}")

    records.sort(key=lambda item: (item.page, item.file))
    return records


def parse_local(pdf_path: Path, images_dir: Path) -> ParseResult:
    """Parse a PDF locally using PyMuPDF."""
    started = time.perf_counter()
    warnings: list[str] = []

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise ParseError(f"无法打开 PDF：{exc}") from exc

    try:
        if doc.needs_pass:
            raise ParseError("PDF 已加密，无法解析")
        page_count = doc.page_count
        if page_count <= 0:
            raise ParseError("PDF 页数为 0")

        raw_pages: dict[int, str] = {}
        pages: dict[int, str] = {}
        for page_no, page in enumerate(doc, start=1):
            try:
                raw_text = page.get_text("text", sort=True)
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append(f"第 {page_no} 页文本提取失败：{exc}")
                raw_text = ""
            raw_pages[page_no] = raw_text
            pages[page_no] = clean_page_text(raw_text)

        toc = read_toc(doc, page_count)
        toc_pages = detect_toc_pages(raw_pages)
        assigned, uncovered = assign_pages_to_entries(toc, pages)
        markdown, sections = compose_sections(toc, assigned, uncovered)

        images = _extract_images(doc, images_dir, warnings)
    finally:
        doc.close()

    if not toc:
        warnings.append("PDF 无书签，章节结构无法还原")

    empty_pages = [page_no for page_no, text in pages.items() if not text.strip()]
    if empty_pages:
        sample = "、".join(str(page_no) for page_no in empty_pages[:10])
        suffix = " 等" if len(empty_pages) > 10 else ""
        warnings.append(f"{len(empty_pages)} 页未提取到文本（第 {sample} 页{suffix}），可能为扫描图片页")

    total_chars = sum(len(text) for text in pages.values())
    if total_chars == 0:
        warnings.append("未提取到文本层，若为扫描件请配置 MinerU API Key 后使用云端 OCR")
    warnings.append(DEGRADED_WARNING)

    elapsed = time.perf_counter() - started
    parser_meta = {
        "structure_source": "toc" if toc else "page_order",
        "degraded": True,
        "degradation_reason": "local_text_layer_only",
        "toc_entries": len(toc),
        "toc_pages": toc_pages,
        "text_chars": total_chars,
        "empty_pages": len(empty_pages),
        "image_count": len(images),
        "elapsed_seconds": round(elapsed, 3),
    }
    return ParseResult(
        parser="pymupdf",
        page_count=page_count,
        toc=toc,
        sections=sections,
        markdown=markdown,
        images=images,
        warnings=warnings,
        toc_pages=toc_pages,
        parser_meta=parser_meta,
    )
