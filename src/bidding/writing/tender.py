"""Authoritative tender context used by all writing agents.

Parsed artifacts are the only source of project facts.  Structured extraction
supplies scoring, qualification and format requirements, while the raw section
cache supplies verbatim evidence with page ranges.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..models import Section, TocEntry
from ..parsing import ParseError
from ..parsing.pipeline import load_sections
from ..parsing.structure import normalize_ws, strip_page_markers
from .errors import BidWriteError

TENDER_PLACEHOLDER_PREFIX = "【待核对："

PROJECT_FIELDS: tuple[tuple[str, str], ...] = (
    ("project_name", "项目名称"),
    ("tender_number", "招标编号"),
    ("bid_section", "标段名称"),
    ("project_scale", "建设规模"),
    ("budget_amount_text", "合同估算价"),
    ("bid_deadline", "投标截止时间"),
    ("bid_opening_time", "开标时间"),
    ("bid_opening_place", "开标地点"),
    ("evaluation_method", "评标方法"),
)


def tender_placeholder(label: str) -> str:
    return f"{TENDER_PLACEHOLDER_PREFIX}{label}】"


def _valid_doc_id(doc_id: str) -> None:
    if (
        not doc_id
        or doc_id in {".", ".."}
        or "/" in doc_id
        or "\\" in doc_id
        or Path(doc_id).name != doc_id
    ):
        raise BidWriteError(f"非法的 doc_id：{doc_id!r}")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return str(value).strip()


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = _as_text(
                item.get("requirement")
                or item.get("text")
                or item.get("detail")
                or item.get("description")
            )
        else:
            text = _as_text(item)
        if text:
            result.append(text)
    return result


@dataclass(slots=True)
class TenderContext:
    doc_id: str
    manifest: dict[str, Any]
    extraction: dict[str, Any]
    sections: list[Section]
    toc: list[TocEntry]
    project: dict[str, str]
    evaluation_factors: list[dict[str, Any]]
    preliminary_review: list[dict[str, Any]]
    qualifications: list[dict[str, Any]]
    bid_format_items: list[dict[str, str]]
    technical_requirements: list[dict[str, Any]]
    contract_points: list[dict[str, Any]]
    other_requirements: list[str]
    key_dates: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    _norm_texts: list[str] = field(default_factory=list, repr=False)
    _quote_cache: dict[str, tuple[int, int] | None] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self._norm_texts:
            self._norm_texts = [
                normalize_ws(strip_page_markers(section.text)[0])
                for section in self.sections
            ]

    @property
    def project_name(self) -> str:
        return self.project.get("project_name") or tender_placeholder("项目名称")

    @property
    def source_name(self) -> str:
        return _as_text(self.manifest.get("source_name"))

    @property
    def parsed_at(self) -> str:
        return _as_text(self.manifest.get("parsed_at"))

    @property
    def extraction_partial(self) -> bool:
        meta = self.extraction.get("extraction_meta") or {}
        return bool(meta.get("partial"))

    def locate_quote(self, quote: str) -> tuple[int, int] | None:
        """Return the page range of a grounded excerpt, or ``None``."""
        key = normalize_ws(quote)[:240]
        if not key:
            return None
        if key in self._quote_cache:
            return self._quote_cache[key]
        result: tuple[int, int] | None = None
        for index, normalized in enumerate(self._norm_texts):
            if not normalized:
                continue
            if key in normalized:
                section = self.sections[index]
                result = (section.page_start, section.page_end)
                break
        self._quote_cache[key] = result
        return result

    def find_section(self, keyword: str) -> Section | None:
        for section in self.sections:
            if keyword and keyword in section.title:
                return section
        return None

    def prompt_brief(self, *, max_items: int = 80, max_chars: int | None = None) -> str:
        """Compact, structured rendering of the extracted requirements."""
        lines: list[str] = ["【项目基本信息】"]
        for key, label in PROJECT_FIELDS:
            lines.append(f"- {label}：{self.project.get(key) or tender_placeholder(label)}")

        lines.append("\n【评分因素】")
        for item in self.evaluation_factors[:max_items]:
            score = item.get("max_score")
            score_text = f"{score:g}分" if isinstance(score, (int, float)) else "分值未提取"
            lines.append(
                f"- [{item.get('category') or '其他'}] {item.get('factor') or '未命名'} "
                f"({score_text})：{_as_text(item.get('criteria'))[:600]}"
            )

        lines.append("\n【资格审查/响应性要求】")
        for item in self.qualifications[:max_items]:
            lines.append(
                f"- [{item.get('category') or '其他'}] "
                f"{_as_text(item.get('requirement'))[:600]}"
            )

        lines.append("\n【投标文件格式要求】")
        for item in self.bid_format_items[:max_items]:
            title = _as_text(item.get("title"))
            requirement = _as_text(item.get("requirements"))
            lines.append(f"- {title}：{requirement[:700]}")

        lines.append("\n【技术标准与要求】")
        for item in self.technical_requirements[:max_items]:
            lines.append(
                f"- {_as_text(item.get('topic'))}："
                f"{_as_text(item.get('requirement'))[:700]}"
            )

        lines.append("\n【合同要点（节选）】")
        for item in self.contract_points[:max_items]:
            lines.append(
                f"- {_as_text(item.get('topic'))}："
                f"{_as_text(item.get('detail'))[:500]}"
            )

        lines.append("\n【其他要求】")
        for item in self.other_requirements[:max_items]:
            lines.append(f"- {item[:500]}")

        lines.append("\n【关键时间】")
        for item in self.key_dates[:max_items]:
            lines.append(
                f"- {_as_text(item.get('event'))}：{_as_text(item.get('time'))}"
            )

        lines.append("\n【初步评审】")
        for item in self.preliminary_review[:max_items]:
            lines.append(
                f"- [{item.get('stage') or '其他'}] {_as_text(item.get('item'))}："
                f"{_as_text(item.get('criteria'))[:600]}"
            )
        text = "\n".join(lines)
        if max_chars is not None and len(text) > max_chars:
            marker = "\n……（结构化要求过长，提示词已截断；未显示条款须查招标文件）"
            text = text[: max(0, max_chars - len(marker))] + marker
        return text


def load_tender_context(doc_id: str, settings: Settings | None = None) -> TenderContext:
    settings = settings or get_settings()
    _valid_doc_id(doc_id)
    try:
        sections, toc, manifest = load_sections(doc_id, settings)
    except ParseError as exc:
        raise BidWriteError(str(exc)) from exc
    if not sections:
        raise BidWriteError(f"文档 {doc_id} 没有可用的章节正文，请重新解析")

    extraction_file = settings.parsed_dir / doc_id / "extraction.json"
    if not extraction_file.is_file():
        raise BidWriteError(
            f"文档 {doc_id} 尚未完成结构化抽取：找不到 {extraction_file}。"
            "请先运行 uv run python -m bidding extract <doc_id>"
        )
    try:
        extraction = json.loads(extraction_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BidWriteError(f"读取 extraction.json 失败：{exc}") from exc
    if not isinstance(extraction, dict):
        raise BidWriteError("extraction.json 结构异常")

    project: dict[str, str] = {}
    for key, label in PROJECT_FIELDS:
        value = _as_text(extraction.get(key))
        project[key] = value or tender_placeholder(label)

    bid_format_raw = extraction.get("bid_format")
    bid_format_items: list[dict[str, str]] = []
    if isinstance(bid_format_raw, dict):
        raw_items = bid_format_raw.get("items") or []
    elif isinstance(bid_format_raw, list):
        raw_items = bid_format_raw
    else:
        raw_items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = _as_text(item.get("title"))
        if not title:
            continue
        bid_format_items.append(
            {
                "title": title,
                "requirements": _as_text(item.get("requirements")),
            }
        )

    warnings: list[str] = []
    for warning in manifest.get("warnings") or []:
        warnings.append(f"解析警告：{warning}")
    meta = extraction.get("extraction_meta") or {}
    if meta.get("partial"):
        warnings.append("抽取结果标记为 partial，部分章节未抽取，响应内容可能不完整")
    for failure in meta.get("failed_units") or []:
        if isinstance(failure, dict):
            title = failure.get("title") or failure.get("unit") or "未知单元"
            warnings.append(f"抽取失败单元：{title}")
    ungrounded = extraction.get("ungrounded_fields") or []
    if ungrounded:
        warnings.append(
            f"抽取结果有 {len(ungrounded)} 个字段无法在原文逐字定位，引用前请人工核对"
        )
    if not bid_format_items:
        warnings.append("未抽取到投标文件格式要求，格式章节需人工核对招标文件")
    if not _as_dicts(extraction.get("evaluation_factors")):
        warnings.append("未抽取到评分因素，评分响应索引可能不完整")

    return TenderContext(
        doc_id=doc_id,
        manifest=manifest,
        extraction=extraction,
        sections=sections,
        toc=toc,
        project=project,
        evaluation_factors=_as_dicts(extraction.get("evaluation_factors")),
        preliminary_review=_as_dicts(extraction.get("preliminary_review")),
        qualifications=_as_dicts(extraction.get("qualifications")),
        bid_format_items=bid_format_items,
        technical_requirements=_as_dicts(extraction.get("technical_requirements")),
        contract_points=_as_dicts(extraction.get("contract_points")),
        other_requirements=_as_strings(extraction.get("other_requirements")),
        key_dates=_as_dicts(extraction.get("key_dates")),
        warnings=warnings,
    )
