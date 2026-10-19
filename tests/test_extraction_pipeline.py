"""Tests for extraction unit construction and truncation handling."""

from __future__ import annotations

from dataclasses import replace

from bidding.config import get_settings
from bidding.extraction.errors import ExtractError
from bidding.extraction.llm import JsonCompletion
from bidding.extraction.pipeline import (
    ExtractionUnit,
    _ExtractionRunner,
    _split_for_bisect,
    _without_toc_pages,
    build_units,
)
from bidding.models import TocEntry
from bidding.parsing.structure import compose_sections


def _settings():
    return replace(get_settings(), extract_max_chars=2000)


def _filler(topic: str, paragraphs: int) -> str:
    return "\n\n".join(
        f"{topic}第{index}条：投标人应当按照招标文件要求提交完整的实施方案、"
        f"人员配置、进度计划和质量保证措施，并加盖单位公章。"
        for index in range(1, paragraphs + 1)
    )


def _synthetic_document():
    entries = [
        TocEntry(level=1, title="第一章 招标公告", page=1, page_end=1),
        TocEntry(level=1, title="第二章 投标人须知", page=2, page_end=2),
        TocEntry(
            level=1,
            title="第三章 评标办法（综合评估法）",
            page=3,
            page_end=4,
        ),
        TocEntry(level=2, title="1.评标方法", page=4, page_end=4),
        TocEntry(level=1, title="第四章 合同条款及格式", page=5, page_end=6),
    ]
    assigned = {
        0: ["<!-- page: 1 -->\n第一章 招标公告\n本项目最高投标限价为 333 万元。"],
        1: ["<!-- page: 2 -->\n第二章 投标人须知\n投标人须具备相应施工资质。"],
        2: [
            "<!-- page: 3 -->\n第三章 评标办法（综合评估法）\n"
            "本项目采用综合评估法进行评标。"
        ],
        3: [
            "<!-- page: 4 -->\n1.评标方法\n\n"
            + _filler("评分标准", 80)
        ],
        4: [
            "<!-- page: 5 -->\n第四章 合同条款及格式\n\n"
            + _filler("合同条款", 80)
        ],
    }
    markdown, sections = compose_sections(entries, assigned, [])
    manifest = {
        "doc_id": "doc-1",
        "doc_type": "tender_file",
        "toc_pages": [],
        "source_sha256": "abc",
        "parser": "pymupdf",
    }
    return manifest, sections, entries, markdown


def test_build_units_routes_focus_and_compresses_contract_chapters() -> None:
    manifest, sections, entries, markdown = _synthetic_document()
    units = build_units(manifest, sections, entries, markdown, _settings())
    assert units
    assert [unit.index for unit in units] == list(range(1, len(units) + 1))

    by_title = {unit.title: unit for unit in units}
    assert by_title["第一章 招标公告"].focus == "general"
    assert by_title["第二章 投标人须知"].focus == "qualification"

    scoring_units = [
        unit for unit in units if unit.title.startswith("1.评标方法（")
    ]
    assert len(scoring_units) >= 2
    assert scoring_units[0].title.endswith(f"（1/{len(scoring_units)}）")
    assert all(unit.focus == "scoring" for unit in scoring_units)

    contract_units = [
        unit for unit in units if unit.title.startswith("第四章 合同条款及格式")
    ]
    assert len(contract_units) >= 3
    assert all(unit.focus == "contract" for unit in contract_units)
    assert [unit.title for unit in contract_units] == [
        f"第四章 合同条款及格式（{index}/{len(contract_units)}）"
        for index in range(1, len(contract_units) + 1)
    ]


def test_build_units_drops_toc_pages_and_honours_limit() -> None:
    manifest, sections, entries, markdown = _synthetic_document()
    manifest["toc_pages"] = [1]
    units = build_units(manifest, sections, entries, markdown, _settings())
    assert all("最高投标限价" not in unit.text for unit in units)
    assert all("<!-- page: 1 -->" not in unit.text for unit in units)
    limit = _settings().extract_max_chars
    assert all(len(unit.text) <= limit for unit in units)


def test_without_toc_pages_removes_only_matching_page_blocks() -> None:
    text = (
        "<!-- page: 1 -->\n正文一\n"
        "<!-- page: 2 -->\n目录内容\n"
        "<!-- page: 3 -->\n正文三"
    )
    result = _without_toc_pages(text, [2])
    assert "目录内容" not in result
    assert "正文一" in result and "正文三" in result
    assert "<!-- page: 1 -->" in result
    assert _without_toc_pages(text, []) == text


def test_runner_bisects_large_units_after_malformed_json() -> None:
    settings = replace(
        _settings(),
        extract_bisect_min_chars=1200,
        extract_bisect_max_depth=2,
    )
    unit = ExtractionUnit(
        index=1,
        title="附件五：评标表格",
        path=["第三章 评标办法"],
        text=_filler("评分标准", 80),
        focus="scoring",
    )

    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete_json(self, prompt: str) -> JsonCompletion:
            self.calls += 1
            if self.calls == 1:
                raise ExtractError("模型输出不是合法 JSON：Expecting ',' delimiter")
            return JsonCompletion(
                data={"evaluation_method": "综合评估法"},
                used_json_mode=True,
            )

    client = FlakyClient()
    runner = _ExtractionRunner(settings, client)  # type: ignore[arg-type]
    result, used_json = runner.extract_text(unit)
    assert client.calls == 3
    assert used_json is True
    assert result["evaluation_method"] == "综合评估法"


def test_split_for_bisect_returns_overlapping_halves() -> None:
    unit = ExtractionUnit(
        index=1,
        title="长章节",
        path=[],
        text=_filler("技术条款", 60),
        focus="technical",
    )
    halves = _split_for_bisect(unit.text)
    assert halves is not None
    left, right = halves
    assert left and right
    assert left != right
    assert len(left) < len(unit.text) and len(right) < len(unit.text)
    assert _split_for_bisect("太短") is None


def test_split_for_bisect_prefers_page_boundary_near_midpoint() -> None:
    text = (
        "甲" * 1800
        + "\n<!-- page: 2 -->\n"
        + "乙" * 280
        + "\n\nSENTINEL"
        + "丙" * 1000
    )
    halves = _split_for_bisect(text)
    assert halves is not None
    left, right = halves
    # The cut lands at the page marker before the nearby paragraph break, so
    # the sentinel after that break stays out of the left half.
    assert "<!-- page: 2 -->" in left
    assert "SENTINEL" not in left
    assert "SENTINEL" in right
