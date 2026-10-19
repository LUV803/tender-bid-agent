"""Tests for TOC-aware PDF structure helpers (no PDF or network access)."""

from __future__ import annotations

from bidding.models import TocEntry
from bidding.parsing.structure import (
    assign_pages_to_entries,
    compose_sections,
    detect_doc_type,
    detect_toc_pages,
    is_toc_page,
    read_toc,
)


class _FakeDoc:
    def __init__(self, toc: list[tuple[int, str, int]], *, fail: bool = False) -> None:
        self._toc = toc
        self._fail = fail

    def get_toc(self, simple: bool = True) -> list[tuple[int, str, int]]:
        if self._fail:
            raise RuntimeError("broken outline")
        return self._toc


def test_read_toc_derives_inclusive_ranges_from_nested_bookmarks() -> None:
    doc = _FakeDoc(
        [
            (1, "第一章 招标公告", 1),
            (2, "1.1 项目概况", 2),
            (2, "1.2 招标范围", 4),
            (1, "第二章 投标人须知", 6),
        ]
    )

    entries = read_toc(doc, 10)

    assert [(entry.level, entry.title, entry.page, entry.page_end) for entry in entries] == [
        (1, "第一章 招标公告", 1, 5),
        (2, "1.1 项目概况", 2, 3),
        (2, "1.2 招标范围", 4, 5),
        (1, "第二章 投标人须知", 6, 10),
    ]


def test_read_toc_clamps_pages_and_tolerates_bad_metadata() -> None:
    entries = read_toc(
        _FakeDoc([(1, "封面", 0), (2, "坏条目", "x"), (1, "附录", 99)]),
        10,
    )

    assert [(entry.title, entry.page, entry.page_end) for entry in entries] == [
        ("封面", 1, 9),
        ("附录", 10, 10),
    ]
    assert read_toc(_FakeDoc([]), 0) == []
    assert read_toc(_FakeDoc([(1, "封面", 1)], fail=True), 10) == []


def test_toc_page_detection_uses_dot_leaders() -> None:
    toc_text = (
        "第一章 招标公告 ........ 1\n"
        "第二章 投标人须知 ....... 3\n"
        "第三章 评标办法 ........ 9"
    )
    body_text = "第一章 招标公告\n本项目位于密云区。"

    assert is_toc_page(toc_text)
    assert not is_toc_page(body_text)
    assert detect_toc_pages({1: body_text, 2: toc_text, 3: ""}) == [2]


def test_assign_pages_prefers_deepest_bookmark_and_reports_uncovered() -> None:
    entries = [
        TocEntry(level=1, title="第一章", page=1, page_end=4),
        TocEntry(level=2, title="1.1 节", page=2, page_end=3),
    ]
    pages = {
        1: "章首页",
        2: "节首页",
        3: "节次页",
        4: "章末页",
        5: "书签外的页面",
    }

    assigned, uncovered = assign_pages_to_entries(entries, pages)

    assert [block for block in assigned[0]] == [
        "<!-- page: 1 -->\n章首页",
        "<!-- page: 4 -->\n章末页",
    ]
    assert ["<!-- page: 2 -->\n节首页", "<!-- page: 3 -->\n节次页"] == assigned[1]
    assert uncovered == ["<!-- page: 5 -->\n书签外的页面"]


def test_compose_sections_builds_headings_and_ancestor_paths() -> None:
    entries = [
        TocEntry(level=1, title="第一章 招标公告", page=1, page_end=2),
        TocEntry(level=2, title="1.1 项目概况", page=2, page_end=2),
    ]
    assigned = {
        0: ["<!-- page: 1 -->\n第一章正文"],
        1: ["<!-- page: 2 -->\n项目概况正文"],
    }

    markdown, sections = compose_sections(
        entries, assigned, ["<!-- page: 0 -->\n前置内容"]
    )

    assert len(sections) == 2
    assert sections[1].path == ["第一章 招标公告"]
    assert sections[1].number == "1.1"
    assert sections[1].text == "<!-- page: 2 -->\n项目概况正文"
    assert "<!-- page: 0 -->\n前置内容" in markdown
    assert "## 第一章 招标公告" in markdown
    assert "### 1.1 项目概况" in markdown


def test_detect_doc_type_heuristics() -> None:
    assert detect_doc_type("评标办法 投标人须知", 2) == "tender_file"
    assert detect_doc_type("招标公告", 1) == "announcement"
    assert detect_doc_type("其他内容", 30) == "tender_file"
    assert detect_doc_type("其他内容", 1) == "unknown"
