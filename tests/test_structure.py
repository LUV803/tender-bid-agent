"""Tests for deterministic parsing helpers (no network access)."""

from __future__ import annotations

from bidding.parsing.structure import (
    is_chapter_title,
    normalize_ws,
    section_number,
    split_page_ranges,
    strip_page_markers,
)


def test_split_page_ranges_210_pages_into_two_balanced_shards() -> None:
    assert split_page_ranges(210, 200) == [(0, 104), (105, 209)]


def test_split_page_ranges_handles_short_and_empty_documents() -> None:
    assert split_page_ranges(0, 200) == []
    assert split_page_ranges(120, 200) == [(0, 119)]
    assert split_page_ranges(400, 200) == [(0, 199), (200, 399)]


def test_split_page_ranges_is_balanced() -> None:
    ranges = split_page_ranges(1000, 300)
    sizes = [end - start + 1 for start, end in ranges]
    assert sum(sizes) == 1000
    assert max(sizes) - min(sizes) <= 1
    assert all(start <= end for start, end in ranges)


def test_page_markers_are_stripped_and_collected_in_order() -> None:
    text = "<!-- page: 3 -->\n第一段\n<!-- page: 7 -->\n第二段"
    body, pages = strip_page_markers(text)
    assert pages == [3, 7]
    assert "<!--" not in body
    assert "第一段" in body and "第二段" in body


def test_chapter_and_section_detection() -> None:
    assert is_chapter_title("第三章 评标办法（综合评估法）")
    assert not is_chapter_title("第一卷")
    assert not is_chapter_title("3. 评标程序")
    assert section_number("第三章 评标办法") == "第三章"
    assert section_number("3.2 投标报价") == "3.2"
    assert section_number("（一）资格审查") == "（一）"


def test_normalize_ws_removes_whitespace_and_table_pipes() -> None:
    assert normalize_ws("投标 人 | 须 知") == "投标人须知"
