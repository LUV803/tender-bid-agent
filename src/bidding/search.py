"""Hybrid retrieval over parsed tender documents.

Three deterministic layers are always available:

* section-level lexical search over ``raw/sections.json`` / ``content.md``;
* structured lookup over ``extraction.json`` (scoring factors, qualification
  requirements, review criteria, dates, contract/technical requirements and
  the bid-format checklist);
* a table index built from page headings such as ``表10：投标报价得分计算表``.

When the document has been ingested into Chroma and an embedding key is
configured, vector hits are fused on top of the lexical ranking.  Vector
failures never block an answer: the response records a warning and returns
the deterministic results.  Every hit carries a page or table citation so the
reader can verify it against the tender PDF.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .indexing.chunking import (
    COLLECTION_TENDER_DOCUMENT,
    PAGE_BLOCK_RE,
)
from .indexing.embeddings import ZhipuEmbedder
from .indexing.errors import IngestError
from .indexing.store import ChromaStore
from .parsing.structure import PAGE_MARKER_RE, sanitize_doc_id

logger = logging.getLogger(__name__)

MAX_QUERY_CHARS = 200
MAX_LIMIT = 30
MATCH_NOISE_RE = re.compile(r"[\s|｜]+")
CJK_RUN_RE = re.compile(r"[一-鿿]+")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*|\d+(?:\.\d+)?%?")
TABLE_HEADING_RE = re.compile(r"表\s*(\d+)\s*[:：]\s*([^\n]{1,80})")
TABLE_LABEL_RE = re.compile(r"表\s*(\d+)")
TABLE_LEADER_RE = re.compile(r"[.．·…]{2,}.*$")
CHAPTER_START_RE = re.compile(r"(?m)^\s*#{0,6}\s*第[一二三四五六七八九十百]+[章卷节]")
MAX_TABLE_BODY_PAGES = 12

# Query phrases that express intent rather than content.  They are removed
# before building the "core" phrase used for title matching.
INTENT_PHRASES = (
    "怎么计算",
    "如何计算",
    "怎么算",
    "怎么写",
    "如何编写",
    "怎样编写",
    "有哪些",
    "包括哪些",
    "需要什么材料",
    "需要哪些材料",
    "需要什么",
    "需要哪些",
    "是什么",
    "怎么",
    "如何",
    "怎样",
    "请问",
)
STOP_TERMS = {
    "怎么",
    "么算",
    "么写",
    "如何",
    "什么",
    "需要",
    "哪些",
    "多少",
    "是否",
    "一下",
    "请问",
    "可以",
    "应该",
}

# Triggers -> aliases.  Aliases are added with a lower weight so the original
# wording stays dominant.
SYNONYM_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("报价", "价格", "费用"), ("投标报价", "报价", "评标价", "评标基准价", "偏差率")),
    (("评分", "得分", "分值", "打分", "怎么算"), ("得分", "评分", "分值", "计分", "打分")),
    (
        ("资格", "资质", "审查", "材料", "条件"),
        ("资格", "资质", "资格审查", "资格条件", "证明材料"),
    ),
    (
        ("施工组织设计", "技术标", "施组", "施工方案", "怎么写"),
        ("施工组织设计", "技术标", "施工组织", "施工方案"),
    ),
    (("工期", "进度"), ("工期", "计划工期", "进度计划", "完工")),
    (("质量", "合格"), ("质量", "质量标准", "质量管理")),
    (("保证金",), ("投标保证金", "保证金")),
    (("项目经理", "负责人", "建造师"), ("项目经理", "项目负责人", "建造师")),
    (("安全", "文明施工"), ("安全生产", "安全管理", "安全文明施工")),
)

# kind -> multiplier when the query expresses the corresponding intent.
INTENT_RULES: tuple[
    tuple[tuple[str, ...], dict[str, float]], ...
] = (
    (
        ("评分", "得分", "分值", "打分", "怎么算", "评标", "评审"),
        {"factor": 1.3, "review": 1.08, "table": 1.06, "fragment": 1.0},
    ),
    (
        ("资格", "资质", "审查", "材料", "条件"),
        {"qualification": 1.32, "review": 1.1, "format": 1.05},
    ),
    (
        ("施工组织设计", "技术标", "施组", "施工方案", "怎么写", "编制"),
        {"technical": 1.28, "factor": 1.12, "format": 1.16, "section": 1.04},
    ),
    (
        ("工期", "进度", "完工", "竣工"),
        {"date": 1.08, "contract": 1.16, "technical": 1.06},
    ),
    (
        ("合同", "付款", "支付", "质保金", "质量保证金", "违约"),
        {"contract": 1.32},
    ),
    (("时间", "截止", "开标", "日期"), {"date": 1.35}),
    (("格式", "文件组成", "材料", "表格"), {"format": 1.1, "table": 1.04}),
)

GROUP_BASE: dict[str, float] = {
    "factor": 1.02,
    "qualification": 1.02,
    "review": 1.0,
    "technical": 1.0,
    "format": 0.99,
    "contract": 0.98,
    "date": 0.97,
    "other": 0.9,
    "section": 1.0,
    "table": 1.04,
    "fragment": 0.92,
}

KIND_LABELS: dict[str, str] = {
    "table": "表格",
    "section": "章节",
    "factor": "评分因素",
    "review": "评审标准",
    "qualification": "资格要求",
    "date": "关键时间",
    "contract": "合同条款",
    "technical": "技术要求",
    "format": "投标文件格式",
    "other": "其他要求",
    "fragment": "原文片段",
}

GROUP_LIMITS: dict[str, int] = {
    "table": 4,
    "factor": 4,
    "review": 3,
    "qualification": 5,
    "date": 3,
    "contract": 3,
    "technical": 3,
    "format": 3,
    "other": 2,
    "section": 12,
    "fragment": 8,
}


class SearchError(RuntimeError):
    """Raised when search cannot read the requested document."""


@dataclass(slots=True)
class PageIndex:
    """Normalized document text with page offsets and raw page bodies."""

    text: str = ""
    offsets: list[int] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    raw_pages: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_markdown(cls, markdown: str) -> "PageIndex":
        index = cls()
        matches = list(PAGE_BLOCK_RE.finditer(markdown or ""))
        if not matches:
            compact = _norm(markdown or "")
            if compact:
                index.offsets = [0]
                index.pages = [1]
                index.text = compact
                index.raw_pages = {1: markdown or ""}
            return index
        chunks: list[str] = []
        cursor = 0
        for match in matches:
            page = int(match.group(1))
            raw = match.group(2)
            index.raw_pages.setdefault(page, raw)
            compact = _norm(raw)
            if not compact:
                continue
            index.offsets.append(cursor)
            index.pages.append(page)
            chunks.append(compact)
            cursor += len(compact)
        index.text = "".join(chunks)
        return index

    @classmethod
    def from_sections(cls, sections: Iterable[dict[str, Any]]) -> "PageIndex":
        index = cls()
        chunks: list[str] = []
        cursor = 0
        for section in sections:
            text = str(section.get("text") or "")
            for match in PAGE_BLOCK_RE.finditer(text):
                page = int(match.group(1))
                raw = match.group(2)
                if page in index.raw_pages:
                    continue
                index.raw_pages[page] = raw
                compact = _norm(raw)
                if not compact:
                    continue
                index.offsets.append(cursor)
                index.pages.append(page)
                chunks.append(compact)
                cursor += len(compact)
        index.text = "".join(chunks)
        return index

    def page_for(self, compact_offset: int) -> int | None:
        if not self.offsets:
            return None
        position = 0
        for start, page in zip(self.offsets, self.pages):
            if start > compact_offset:
                break
            position = page
        return position

    def locate(self, quote: str) -> int | None:
        """Return the page containing ``quote`` using whitespace-free matching."""
        needle = _norm(quote)
        if len(needle) < 4 or not self.text:
            return None
        position = self.text.find(needle)
        if position < 0:
            # LLM quotes occasionally add or omit a short prefix.  Try leading
            # windows first, then a middle window, before giving up.
            for size in (48, 32, 24, 16, 10, 8):
                if len(needle) >= size:
                    position = self.text.find(needle[:size])
                    if position >= 0:
                        break
            else:
                for start in range(0, max(1, len(needle) - 12), 16):
                    position = self.text.find(needle[start : start + 16])
                    if position >= 0:
                        break
        if position < 0:
            return None
        return self.page_for(position)

    def page_for_terms(self, raw_text: str, terms: Iterable[str]) -> int | None:
        pages = _page_map(raw_text)
        if not pages:
            return None
        for term in terms:
            if len(term) < 2:
                continue
            for page, compact in pages:
                if term in compact:
                    return page
        return None


@dataclass(slots=True)
class SearchDocument:
    doc_id: str
    manifest: dict[str, Any]
    extraction: dict[str, Any]
    sections: list[dict[str, Any]]
    page_index: PageIndex
    index_manifest: dict[str, Any] | None
    source_name: str
    project_name: str
    toc_pages: set[int]


@dataclass(slots=True)
class _SectionEntry:
    title: str
    display_path: str
    chapter: str
    raw: str
    plain: str
    norm: str
    heading_norm: str
    page_start: int
    page_end: int


@dataclass(slots=True)
class _TableEntry:
    label: str
    number: int
    title: str
    page: int
    body: str
    heading_norm: str
    body_norm: str


@dataclass(slots=True)
class _StructuredEntry:
    kind: str
    title: str
    body: str
    norm: str
    heading_norm: str
    page: int | None
    page_start: int | None
    page_end: int | None
    meta: dict[str, Any]


def _norm(value: str) -> str:
    """Whitespace/pipe-free lowercase text used for matching."""
    return MATCH_NOISE_RE.sub("", value or "").lower()


def _clean_query(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip())


def _core_query(query: str) -> str:
    text = _clean_query(query)
    for phrase in INTENT_PHRASES:
        text = text.replace(phrase, "")
    text = re.sub(r"[?？!！。，,；;：:\s]+", "", text)
    return text


def _query_terms(query: str) -> tuple[dict[str, float], str]:
    core = _core_query(query)
    terms: dict[str, float] = {}

    def add(term: str, weight: float) -> None:
        term = term.strip().lower()
        if len(term) < 2 or term in STOP_TERMS:
            return
        terms[term] = max(terms.get(term, 0.0), weight)

    for token in TOKEN_RE.findall(query):
        add(token, 1.0)

    for run in CJK_RUN_RE.findall(core or query):
        add(run, 1.0)
        for position in range(len(run) - 1):
            add(run[position : position + 2], 0.32)

    for triggers, aliases in SYNONYM_GROUPS:
        if not any(trigger in query for trigger in triggers):
            continue
        for alias in aliases:
            add(alias, 0.48)

    for label in TABLE_LABEL_RE.findall(query):
        add(f"表{int(label)}", 1.8)

    if core:
        terms[core] = terms.get(core, 0.0) + 1.2
    return terms, core


def _strong_terms(terms: dict[str, float]) -> list[str]:
    return [
        term
        for term, _weight in sorted(terms.items(), key=lambda item: item[1], reverse=True)
        if len(term) >= 2
    ]


def _idf_map(texts: list[str], terms: dict[str, float]) -> dict[str, float]:
    total = max(len(texts), 1)
    frequencies = {
        term: sum(1 for text in texts if term in text) for term in terms
    }
    return {
        term: math.log(1 + (total - count + 0.5) / (count + 0.5))
        for term, count in frequencies.items()
    }


def _score_text(
    norm: str,
    heading_norm: str,
    terms: dict[str, float],
    idf: dict[str, float],
    *,
    title_weight: float = 4.0,
) -> tuple[float, list[str]]:
    score = 0.0
    matched: list[tuple[str, float]] = []
    for term, weight in terms.items():
        body_hits = norm.count(term)
        heading_hits = heading_norm.count(term)
        if not body_hits and not heading_hits:
            continue
        local = (
            weight
            * idf.get(term, 1.0)
            * (math.log1p(min(body_hits, 16)) + title_weight * heading_hits)
        )
        score += local
        matched.append((term, local))
    matched.sort(key=lambda item: item[1], reverse=True)
    return score, [term for term, _value in matched]


def _coverage(
    norm: str, heading_norm: str, terms: dict[str, float]
) -> float:
    """Weighted share of the query matched by this entry.

    Per-group normalization alone would rank the best entry of an unrelated
    group as high as the best entry of the relevant group.  Coverage keeps
    entries that only hit one generic token (for example 投标 in a contract
    clause) below entries matching the whole query concept.
    """
    total = sum(terms.values())
    if total <= 0:
        return 0.0
    matched = sum(
        weight
        for term, weight in terms.items()
        if term in norm or term in heading_norm
    )
    return matched / total


def _intent_boost(kind: str, query: str) -> float:
    boost = 1.0
    for triggers, weights in INTENT_RULES:
        if any(trigger in query for trigger in triggers):
            boost *= weights.get(kind, 1.0)
    return min(boost, 2.1)


def _page_map(raw_text: str) -> list[tuple[int, str]]:
    matches = list(PAGE_BLOCK_RE.finditer(raw_text or ""))
    if not matches:
        compact = _norm(raw_text)
        return [(1, compact)] if compact else []
    pages: list[tuple[int, str]] = []
    for match in matches:
        compact = _norm(match.group(2))
        if compact:
            pages.append((int(match.group(1)), compact))
    return pages


def _plain_text(raw_text: str, limit: int | None = None) -> str:
    text = PAGE_MARKER_RE.sub(" ", raw_text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _compact_offsets(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    for position, char in enumerate(text):
        if char.isspace() or char in "|｜":
            continue
        chars.append(char.lower())
        offsets.append(position)
    return "".join(chars), offsets


def _make_snippet(
    raw_text: str,
    terms: Iterable[str],
    *,
    width: int = 240,
    fallback: str = "",
) -> str:
    visible = re.sub(r"\s+", " ", (raw_text or "").strip())
    if not visible:
        return fallback
    compact, offsets = _compact_offsets(visible)
    for term in terms:
        needle = _norm(term)
        if len(needle) < 2:
            continue
        position = compact.find(needle)
        if position < 0:
            continue
        raw_position = offsets[max(0, min(position, len(offsets) - 1))]
        start = max(0, raw_position - 70)
        end = min(len(visible), raw_position + width)
        snippet = visible[start:end].strip()
        if start > 0:
            snippet = f"…{snippet}"
        if end < len(visible):
            snippet = f"{snippet}…"
        return snippet
    return visible[:width].rstrip() + ("…" if len(visible) > width else "")


def _page_hint(start: int | None, end: int | None) -> str:
    if not start:
        return ""
    if not end or end == start:
        return f"p{start}"
    return f"p{start}-p{end}"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    if isinstance(value, list):
        return "；".join(part for part in (_as_text(item) for item in value) if part)
    return ""


def load_document(settings: Settings, doc_id: str) -> SearchDocument:
    """Read parse artifacts for one canonical doc_id without leaving upload/."""
    if not doc_id or sanitize_doc_id(doc_id) != doc_id:
        raise SearchError("doc_id 不安全或为空")
    root = settings.parsed_dir.resolve()
    parsed_dir = (root / doc_id).resolve()
    if parsed_dir.parent != root or not parsed_dir.is_dir():
        raise SearchError("文档尚未解析或不存在")
    manifest = _read_json(parsed_dir / "manifest.json")
    if manifest is None:
        raise SearchError("文档尚未解析或不存在")
    extraction = _read_json(parsed_dir / "extraction.json") or {}
    index_manifest = _read_json(parsed_dir / "index_manifest.json")
    source_name = str(manifest.get("source_name") or "")
    if not source_name:
        source_name = str(manifest.get("source_path") or "").rsplit("/", 1)[-1]
    project_name = str(extraction.get("project_name") or "")
    toc_pages = {int(page) for page in _as_list(manifest.get("toc_pages")) if str(page).isdigit()}

    sections_raw = _read_json(parsed_dir / "raw" / "sections.json") or {}
    sections = [item for item in _as_list(sections_raw.get("sections")) if isinstance(item, dict)]

    content = ""
    content_path = parsed_dir / "content.md"
    try:
        if content_path.is_file():
            content = content_path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    page_index = PageIndex.from_markdown(content)
    if not page_index.text:
        page_index = PageIndex.from_sections(sections)
    if not sections and page_index.raw_pages:
        sections = []
        for page in sorted(page_index.raw_pages):
            raw = page_index.raw_pages[page]
            plain = _plain_text(raw)
            if len(plain) < 20:
                continue
            first_line = next(
                (line.strip(" #") for line in plain.split("\n") if line.strip(" #")),
                f"第 {page} 页",
            )
            sections.append(
                {
                    "title": first_line[:40],
                    "path": ["全文"],
                    "page_start": page,
                    "page_end": page,
                    "text": f"<!-- page: {page} -->\n{raw}",
                }
            )
    return SearchDocument(
        doc_id=doc_id,
        manifest=manifest,
        extraction=extraction,
        sections=sections,
        page_index=page_index,
        index_manifest=index_manifest,
        source_name=source_name,
        project_name=project_name,
        toc_pages=toc_pages,
    )


def _section_entries(document: SearchDocument) -> list[_SectionEntry]:
    entries: list[_SectionEntry] = []
    for section in document.sections:
        raw = str(section.get("text") or "")
        plain = _plain_text(raw)
        if len(plain) < 20:
            continue
        title = str(section.get("title") or "").strip() or "(无标题)"
        path = [str(item) for item in _as_list(section.get("path")) if item]
        display_path = " > ".join([*path, title])
        chapter = path[0] if path else title
        entries.append(
            _SectionEntry(
                title=title,
                display_path=display_path,
                chapter=chapter,
                raw=raw,
                plain=plain,
                norm=_norm(plain),
                heading_norm=_norm(display_path),
                page_start=int(section.get("page_start") or 1),
                page_end=int(section.get("page_end") or section.get("page_start") or 1),
            )
        )
    return entries


def _clean_table_title(value: str) -> str:
    title = TABLE_LEADER_RE.sub("", value or "")
    title = re.sub(r"\s+", "", title).strip(" .．·…:：-—")
    return title[:60]


def _table_entries(document: SearchDocument) -> list[_TableEntry]:
    occurrences: list[dict[str, Any]] = []
    for page in sorted(document.page_index.raw_pages):
        if page in document.toc_pages:
            continue
        raw = document.page_index.raw_pages[page]
        for match in TABLE_HEADING_RE.finditer(raw):
            title = _clean_table_title(match.group(2))
            if len(title) < 2:
                continue
            occurrences.append(
                {
                    "number": int(match.group(1)),
                    "title": title,
                    "page": page,
                    "position": match.start(),
                    "tail": len(raw[match.end() :].strip()),
                }
            )
    by_number: dict[int, list[dict[str, Any]]] = {}
    for occurrence in occurrences:
        by_number.setdefault(occurrence["number"], []).append(occurrence)

    # Parsed sections know exactly which pages each table occupies.  Prefer
    # that range over heuristic page lookahead (tables in this corpus can span
    # ten pages and contain no markdown pipe characters).
    section_ranges: dict[int, list[tuple[int, int]]] = {}
    for section in document.sections:
        title = str(section.get("title") or "")
        label = TABLE_LABEL_RE.search(title)
        if not label:
            continue
        start = int(section.get("page_start") or 0)
        end = int(section.get("page_end") or start)
        if start and end >= start:
            section_ranges.setdefault(int(label.group(1)), []).append((start, end))

    entries: list[_TableEntry] = []
    for number, group in by_number.items():
        primary = max(group, key=lambda item: (item["tail"], -item["page"]))
        titles = Counter(item["title"] for item in group)
        title = max(titles, key=lambda item: (titles[item], len(item)))
        start_key = (primary["page"], primary["position"])
        later = [
            item
            for item in occurrences
            if (item["page"], item["position"]) > start_key
            and item["number"] != number
        ]
        boundary = min((item["page"], item["position"]) for item in later) if later else None
        covering = [
            end
            for start, end in section_ranges.get(number, [])
            if start <= primary["page"] <= end
        ]
        body_end = max(covering) if covering else (
            primary["page"] + MAX_TABLE_BODY_PAGES - 1
        )
        body_pages: list[int] = []
        for page in sorted(document.page_index.raw_pages):
            if not primary["page"] <= page <= body_end:
                continue
            if page in document.toc_pages:
                continue
            if boundary is not None and page > boundary[0]:
                break
            raw_page = document.page_index.raw_pages[page]
            # The last table in a document has no following table heading as a
            # boundary.  A new chapter/section heading still marks its end.
            if (
                not covering
                and page != primary["page"]
                and CHAPTER_START_RE.search(raw_page)
            ):
                break
            body_pages.append(page)
        body = "\n".join(
            document.page_index.raw_pages[page][:4000] for page in body_pages
        )
        heading = f"表{number} {title}"
        entries.append(
            _TableEntry(
                label=f"表{number}",
                number=number,
                title=title,
                page=primary["page"],
                body=body,
                heading_norm=_norm(heading),
                body_norm=_norm(body),
            )
        )
    entries.sort(key=lambda item: item.number)
    return entries


def _structured_entries(document: SearchDocument) -> list[_StructuredEntry]:
    extraction = document.extraction
    page_index = document.page_index
    entries: list[_StructuredEntry] = []

    def add(
        kind: str,
        title: str,
        body: str,
        *,
        quote: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        title = re.sub(r"\s+", " ", title or "").strip()
        body = re.sub(r"\s+", " ", body or "").strip()
        if not title and not body:
            return
        if not title:
            title = body[:40]
        if not body:
            body = quote
        page = page_index.locate(quote) if quote else None
        merged_meta = dict(meta or {})
        if quote:
            merged_meta.setdefault("source_quote", quote)
        entries.append(
            _StructuredEntry(
                kind=kind,
                title=title,
                body=body,
                norm=_norm(f"{title} {body}"),
                heading_norm=_norm(title),
                page=page,
                page_start=page,
                page_end=page,
                meta=merged_meta,
            )
        )

    for item in _as_list(extraction.get("evaluation_factors")):
        if not isinstance(item, dict):
            continue
        factor = _as_text(item.get("factor"))
        if not factor:
            continue
        score = item.get("max_score")
        score_text = _as_text(score)
        title = f"{factor}（{score_text} 分）" if score_text else factor
        body = " ".join(
            part
            for part in (
                _as_text(item.get("category")),
                _as_text(item.get("criteria")),
            )
            if part
        )
        add(
            "factor",
            title,
            body,
            quote=_as_text(item.get("source_quote")),
            meta={"category": _as_text(item.get("category")), "max_score": score},
        )

    for item in _as_list(extraction.get("preliminary_review")):
        if not isinstance(item, dict):
            continue
        stage = _as_text(item.get("stage"))
        name = _as_text(item.get("item"))
        if not name:
            continue
        add(
            "review",
            f"{stage} · {name}" if stage else name,
            _as_text(item.get("criteria")),
            quote=_as_text(item.get("source_quote")),
            meta={"stage": stage, "category": "初步评审"},
        )

    for item in _as_list(extraction.get("qualifications")):
        if not isinstance(item, dict):
            continue
        requirement = _as_text(item.get("requirement"))
        if not requirement:
            continue
        category = _as_text(item.get("category"))
        title = f"{category}：{requirement[:42]}" if category else requirement[:48]
        body = " ".join(
            part
            for part in (
                requirement,
                _as_text(item.get("thresholds")),
            )
            if part
        )
        add(
            "qualification",
            title,
            body,
            quote=_as_text(item.get("source_quote")),
            meta={
                "category": category,
                "thresholds": _as_list(item.get("thresholds")),
            },
        )

    for item in _as_list(extraction.get("key_dates")):
        if not isinstance(item, dict):
            continue
        event = _as_text(item.get("event"))
        if not event:
            continue
        add(
            "date",
            event,
            _as_text(item.get("time")),
            quote=_as_text(item.get("source_quote")),
            meta={"time": _as_text(item.get("time"))},
        )

    for item in _as_list(extraction.get("contract_points")):
        if not isinstance(item, dict):
            continue
        topic = _as_text(item.get("topic"))
        if not topic:
            continue
        add(
            "contract",
            topic,
            _as_text(item.get("detail")),
            quote=_as_text(item.get("source_quote")),
        )

    for item in _as_list(extraction.get("technical_requirements")):
        if not isinstance(item, dict):
            continue
        topic = _as_text(item.get("topic"))
        if not topic:
            continue
        add(
            "technical",
            topic,
            _as_text(item.get("requirement")),
            quote=_as_text(item.get("source_quote")),
        )

    for item in _as_list((extraction.get("bid_format") or {}).get("items")):
        if not isinstance(item, dict):
            continue
        title = _as_text(item.get("title"))
        requirement = _as_text(item.get("requirements"))
        if title or requirement:
            add("format", title, f"{title} {requirement}".strip())

    for item in _as_list(extraction.get("other_requirements")):
        text = _as_text(item)
        if len(text) >= 6:
            add("other", text[:48], text)

    # Extraction units overlap at section boundaries, so the same requirement
    # can be recorded twice with slightly different wording.  Keep the longest
    # copy per kind instead of showing near-duplicates side by side.  Titles
    # must match too: boilerplate suffixes would otherwise merge unrelated
    # requirements that happen to share a sentence.
    deduped: list[_StructuredEntry] = []
    for entry in entries:
        duplicate = next(
            (
                index
                for index, existing in enumerate(deduped)
                if existing.kind == entry.kind
                and _near_duplicate(
                    existing.norm, entry.norm, existing.title, entry.title
                )
            ),
            None,
        )
        if duplicate is None:
            deduped.append(entry)
        elif len(entry.norm) > len(deduped[duplicate].norm):
            deduped[duplicate] = entry
    return deduped


def _gram_containment(left: str, right: str) -> float:
    left_grams = {left[index : index + 2] for index in range(len(left) - 1)}
    right_grams = {right[index : index + 2] for index in range(len(right) - 1)}
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / min(len(left_grams), len(right_grams))


def _near_duplicate(
    left: str,
    right: str,
    left_title: str,
    right_title: str,
    *,
    body_threshold: float = 0.88,
    title_threshold: float = 0.7,
) -> bool:
    """Containment check used to collapse overlapping extraction records."""
    if min(len(left), len(right)) < 40:
        return False
    if _gram_containment(left_title, right_title) < title_threshold:
        return False
    return _gram_containment(left, right) >= body_threshold


def _result(
    *,
    kind: str,
    title: str,
    snippet: str,
    score: float,
    page_start: int | None = None,
    page_end: int | None = None,
    section_path: str = "",
    source: str = "lexical",
    matched_terms: list[str] | None = None,
    table_label: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "title": title,
        "snippet": snippet,
        "score": round(score, 4),
        "page_start": page_start,
        "page_end": page_end,
        "page_hint": _page_hint(page_start, page_end),
        "section_path": section_path,
        "source": source,
        "matched_terms": (matched_terms or [])[:6],
    }
    if table_label:
        result["table_label"] = table_label
    if meta:
        result.update(meta)
    return result


def _rank_group(
    scored: list[tuple[float, list[str], float, Any]],
    kind: str,
    query: str,
) -> list[tuple[float, list[str], Any]]:
    if not scored:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[0][0]
    if top <= 0:
        return []
    boosted: list[tuple[float, list[str], Any]] = []
    for score, matched, coverage, entry in scored:
        relative = score / top
        if relative < 0.16 or coverage < 0.15:
            continue
        adjusted = relative * (0.2 + 0.8 * coverage)
        boosted.append(
            (adjusted * _intent_boost(kind, query), matched, entry)
        )
    boosted.sort(key=lambda item: item[0], reverse=True)
    return boosted[: GROUP_LIMITS.get(kind, 3)]


def _vector_fragments(
    settings: Settings,
    document: SearchDocument,
    query: str,
    terms: dict[str, float],
    section_idf: dict[str, float],
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    if document.index_manifest is None:
        return [], "该文档尚未写入向量库，本次使用关键词与结构化检索"
    if not settings.embedding_configured:
        return [], "未配置 ZHIPU_API_KEY，本次使用关键词与结构化检索"
    collection = str(
        (document.index_manifest or {}).get("collection")
        or COLLECTION_TENDER_DOCUMENT
    )
    try:
        vector = ZhipuEmbedder(settings).embed_query(query)
        hits = ChromaStore(settings).query(
            collection,
            vector,
            doc_id=document.doc_id,
            n_results=max(8, limit * 2),
        )
    except IngestError as exc:
        message = re.sub(r"\s+", " ", str(exc)).strip()
        return [], f"向量检索不可用，已回退到关键词与结构化检索：{message[:180]}"
    except Exception as exc:  # defensive: never fail the request
        logger.warning("向量检索失败：%s", exc)
        return [], "向量检索不可用，已回退到关键词与结构化检索"
    if not hits:
        return [], "向量库中没有本文档的分块，本次使用关键词与结构化检索"

    documents = [str(hit.get("document") or "") for hit in hits]
    idf = _idf_map([_norm(text) for text in documents], terms)
    scored: list[tuple[float, list[str], dict[str, Any]]] = []
    for hit in hits:
        metadata = hit.get("metadata") or {}
        text = str(hit.get("document") or "")
        title = str(metadata.get("section_title") or "")
        heading = " > ".join(
            part
            for part in (str(metadata.get("section_path") or ""), title)
            if part
        )
        lexical, matched = _score_text(
            _norm(text), _norm(heading), terms, idf, title_weight=3.0
        )
        distance = hit.get("distance")
        similarity = (
            max(0.0, min(1.0, 1.0 - float(distance)))
            if isinstance(distance, (int, float))
            else 0.0
        )
        scored.append((lexical, matched, {**hit, "similarity": similarity}))

    top_lexical = max((item[0] for item in scored), default=0.0)
    fragments: list[dict[str, Any]] = []
    for lexical, matched, hit in scored:
        relative = lexical / top_lexical if top_lexical > 0 else 0.0
        similarity = hit["similarity"]
        combined = 0.55 * relative + 0.45 * similarity
        if combined < 0.2:
            continue
        metadata = hit.get("metadata") or {}
        raw_text = str(hit.get("document") or "")
        raw_text = re.sub(r"^【[^】]*】\s*", "", raw_text)
        start = metadata.get("page_start")
        end = metadata.get("page_end")
        fragments.append(
            _result(
                kind="fragment",
                title=str(metadata.get("section_title") or "原文片段"),
                snippet=_make_snippet(raw_text, matched, width=260),
                score=combined,
                page_start=int(start) if isinstance(start, (int, float)) else None,
                page_end=int(end) if isinstance(end, (int, float)) else None,
                section_path=str(metadata.get("section_path") or ""),
                source="vector",
                matched_terms=matched,
                meta={
                    "similarity": round(similarity, 4),
                    "chunk_id": str(hit.get("id") or ""),
                },
            )
        )
    fragments.sort(key=lambda item: item["score"], reverse=True)
    return fragments[: GROUP_LIMITS["fragment"]], None


def search_document(
    settings: Settings,
    doc_id: str,
    query: str,
    *,
    limit: int = 10,
    mode: str = "auto",
) -> dict[str, Any]:
    """Search one parsed tender document and return cited, ranked hits."""
    started = time.perf_counter()
    query = _clean_query(query)
    if not query:
        raise SearchError("检索词不能为空")
    if len(query) > MAX_QUERY_CHARS:
        raise SearchError(f"检索词不能超过 {MAX_QUERY_CHARS} 个字符")
    limit = max(1, min(int(limit), MAX_LIMIT))
    mode = (mode or "auto").strip().lower()
    if mode not in {"auto", "lexical", "hybrid"}:
        raise SearchError("mode 必须是 auto、lexical 或 hybrid")

    document = load_document(settings, doc_id)
    terms, core = _query_terms(query)
    strong_terms = _strong_terms(terms)
    if not terms:
        raise SearchError("检索词至少需要包含一个有效关键词")

    sections = _section_entries(document)
    section_idf = _idf_map([entry.norm for entry in sections], terms)

    section_scored: list[tuple[float, list[str], float, _SectionEntry]] = []
    for entry in sections:
        score, matched = _score_text(
            entry.norm, entry.heading_norm, terms, section_idf, title_weight=5.0
        )
        if score > 0:
            section_scored.append(
                (
                    score,
                    matched,
                    _coverage(entry.norm, entry.heading_norm, terms),
                    entry,
                )
            )
    ranked_sections = _rank_group(section_scored, "section", query)

    tables = _table_entries(document)
    table_idf = _idf_map([f"{item.body_norm}{item.heading_norm}" for item in tables], terms)
    table_scored: list[tuple[float, list[str], float, _TableEntry]] = []
    for entry in tables:
        score, matched = _score_text(
            entry.body_norm,
            entry.heading_norm,
            terms,
            table_idf,
            title_weight=7.0,
        )
        if f"表{entry.number}" in terms:
            score = score * 1.35 + 0.5
        if score > 0:
            table_scored.append(
                (
                    score,
                    matched,
                    _coverage(entry.body_norm, entry.heading_norm, terms),
                    entry,
                )
            )
    ranked_tables = _rank_group(table_scored, "table", query)

    structured = _structured_entries(document)
    by_kind: dict[str, list[_StructuredEntry]] = {}
    for entry in structured:
        by_kind.setdefault(entry.kind, []).append(entry)
    ranked_structured: list[tuple[float, list[str], _StructuredEntry]] = []
    for kind, entries in by_kind.items():
        idf = _idf_map([entry.norm for entry in entries], terms)
        scored: list[tuple[float, list[str], float, _StructuredEntry]] = []
        for entry in entries:
            score, matched = _score_text(
                entry.norm, entry.heading_norm, terms, idf, title_weight=4.0
            )
            if score > 0:
                scored.append(
                    (
                        score,
                        matched,
                        _coverage(entry.norm, entry.heading_norm, terms),
                        entry,
                    )
                )
        ranked_structured.extend(_rank_group(scored, kind, query))

    results: list[dict[str, Any]] = []
    table_hits: list[dict[str, Any]] = []
    for score, matched, entry in ranked_tables:
        hit = _result(
            kind="table",
            title=f"{entry.label}：{entry.title}",
            snippet=_make_snippet(
                document.page_index.raw_pages.get(entry.page, entry.body),
                matched,
                width=260,
            ),
            score=score * GROUP_BASE["table"],
            page_start=entry.page,
            page_end=entry.page,
            table_label=entry.label,
            matched_terms=matched,
            meta={"table_title": entry.title},
        )
        results.append(hit)
        table_hits.append(hit)
    for score, matched, entry in ranked_structured:
        results.append(
            _result(
                kind=entry.kind,
                title=entry.title,
                snippet=_make_snippet(entry.body, matched, width=260),
                score=score * GROUP_BASE.get(entry.kind, 1.0),
                page_start=entry.page_start,
                page_end=entry.page_end,
                matched_terms=matched,
                meta=entry.meta,
            )
        )
    for score, matched, entry in ranked_sections:
        # Table-titled sections should cite the heading page, not the first
        # page where one of the query terms happens to occur.  Look inside the
        # section itself so the table-of-contents copy cannot win.
        page = None
        if TABLE_LABEL_RE.search(entry.title):
            title_norm = _norm(entry.title)
            for page_number, compact in _page_map(entry.raw):
                if title_norm in compact:
                    page = page_number
                    break
        page = page or document.page_index.page_for_terms(entry.raw, strong_terms)
        page_start = page or entry.page_start
        page_end = page or entry.page_end
        if page_start > page_end:
            page_start, page_end = page_end, page_start
        results.append(
            _result(
                kind="section",
                title=entry.title,
                snippet=_make_snippet(entry.raw, matched, width=260, fallback=entry.plain),
                score=score,
                page_start=page_start,
                page_end=page_end,
                section_path=entry.display_path,
                matched_terms=matched,
                meta={"chapter": entry.chapter},
            )
        )

    warnings: list[str] = []
    vector_used = False
    if mode != "lexical":
        fragments, warning = _vector_fragments(
            settings, document, query, terms, section_idf, limit
        )
        if warning:
            warnings.append(warning)
        if fragments:
            vector_used = True
            existing = {
                (item.get("section_path"), item.get("page_hint"))
                for item in results
                if item.get("kind") == "section"
            }
            for fragment in fragments:
                key = (fragment.get("section_path"), fragment.get("page_hint"))
                if key in existing:
                    continue
                existing.add(key)
                results.append(fragment)
    elif mode == "lexical":
        pass

    results.sort(
        key=lambda item: (item["score"], item["kind"] == "table"), reverse=True
    )
    # A table heading often also exists as a section with the same title.  Keep
    # the higher-ranked table hit so the result list does not repeat itself.
    deduped: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for item in results:
        key = _norm(str(item.get("title") or ""))
        if key and key in seen_titles:
            continue
        if key:
            seen_titles.add(key)
        deduped.append(item)
    results = deduped[:limit]
    related_tables = table_hits[:5]

    counts: dict[str, int] = {}
    for item in results:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    return {
        "query": query,
        "core_terms": core,
        "doc_id": document.doc_id,
        "source_name": document.source_name,
        "project_name": document.project_name,
        "mode": "hybrid" if vector_used else "lexical",
        "vector_used": vector_used,
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
        "total": len(results),
        "counts": counts,
        "tables": related_tables,
        "results": results,
    }
