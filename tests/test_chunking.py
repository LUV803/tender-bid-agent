"""Tests for chapter-aware retrieval chunking (no network access)."""

from __future__ import annotations

from dataclasses import replace

from bidding.config import get_settings
from bidding.indexing.chunking import build_chunks, collection_for
from bidding.models import Section


def _settings(**overrides: object):
    base = replace(
        get_settings(),
        chunk_target=120,
        chunk_hard_max=160,
        chunk_overlap=0,
        embedding_model="embedding-3",
        embedding_dimensions=2048,
    )
    return replace(base, **overrides)


def _manifest(**overrides: object) -> dict:
    data = {
        "doc_id": "doc-1",
        "doc_type": "tender_file",
        "source_name": "tender.pdf",
        "source_sha256": "abc",
        "parser": "pymupdf",
        "toc_pages": [],
    }
    data.update(overrides)
    return data


def _section(
    *,
    title: str,
    text: str,
    path: list[str] | None = None,
    page_start: int = 1,
) -> Section:
    return Section(
        index=0,
        level=1,
        number="",
        title=title,
        path=path or [],
        page_start=page_start,
        page_end=page_start,
        text=text,
    )


def test_chunks_strip_page_markers_skip_toc_pages_and_map_pages() -> None:
    section = _section(
        title="第一章 招标公告",
        text=(
            "<!-- page: 1 -->\n"
            "第一章 招标公告\n本项目位于密云区，计划工期 90 日历天。\n\n"
            "<!-- page: 2 -->\n"
            "目录\n第一章 招标公告 ........ 1\n第二章 投标人须知 ........ 3\n\n"
            "<!-- page: 3 -->\n"
            "投标人须在 2023 年 11 月 14 日 13:30 前递交投标文件。"
        ),
        page_start=1,
    )
    chunks = build_chunks(
        _manifest(toc_pages=[2]),
        [section],
        [],
        section.text,
        _settings(),
    )
    assert chunks
    assert all("<!--" not in chunk.text for chunk in chunks)
    assert all("目录" not in chunk.text for chunk in chunks)
    assert all(chunk.metadata["chapter"] == "第一章 招标公告" for chunk in chunks)
    assert {chunk.metadata["page_start"] for chunk in chunks} <= {1, 3}
    assert chunks[0].text.startswith("【第一章 招标公告】")
    assert chunks[0].id == "doc-1:00001"
    assert chunks[-1].metadata["chunk_total"] == len(chunks)


def test_chapter_metadata_uses_chapter_from_section_path() -> None:
    section = _section(
        title="2.评审标准",
        path=["第一卷", "第三章 评标办法（综合评估法）"],
        text="<!-- page: 59 -->\n2.1 初步评审标准\n形式评审标准见评标办法前附表。",
        page_start=59,
    )
    chunks = build_chunks(
        _manifest(), [section], [], section.text, _settings()
    )
    assert chunks
    assert chunks[0].metadata["chapter"] == "第三章 评标办法（综合评估法）"
    assert chunks[0].metadata["page_hint"] == "p59"
    assert chunks[0].metadata["section_path"] == (
        "第一卷 > 第三章 评标办法（综合评估法） > 2.评审标准"
    )


def test_long_paragraphs_are_split_below_hard_max() -> None:
    text = "<!-- page: 1 -->\n" + "。".join(
        f"第{index}条要求投标人提供完整的技术方案并加盖公章"
        for index in range(1, 61)
    ) + "。"
    section = _section(title="技术要求", text=text)
    chunks = build_chunks(
        _manifest(),
        [section],
        [],
        text,
        _settings(chunk_target=120, chunk_hard_max=160, chunk_overlap=0),
    )
    assert len(chunks) > 1
    assert all(chunk.metadata["char_count"] <= 160 for chunk in chunks)
    assert all(chunk.metadata["char_count"] >= 20 for chunk in chunks)


def test_build_chunks_falls_back_to_markdown_without_sections() -> None:
    markdown = (
        "<!-- page: 1 -->\n"
        "这是一份没有书签的招标公告，正文需要按全文分块处理。\n\n"
        "<!-- page: 2 -->\n"
        "投标截止时间为 2024 年 1 月 1 日。"
    )
    chunks = build_chunks(_manifest(), [], [], markdown, _settings())
    assert chunks
    assert all(chunk.metadata["chapter"] == "全文" for chunk in chunks)
    assert all(chunk.metadata["section_title"] == "" for chunk in chunks)


def test_collection_for_doc_type() -> None:
    assert collection_for("tender_file") == "tender_document"
    assert collection_for("tender_document") == "tender_document"
    assert collection_for("announcement") == "tender_announcement"
