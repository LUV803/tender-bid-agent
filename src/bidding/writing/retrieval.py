"""Deterministic local retrieval of tender evidence for each bid section.

No external vector store is required: sections and structured extraction items
are scored by domain keywords and the best paragraphs are quoted with their
page ranges.  This keeps the CLI usable offline except for the LLM calls and
makes the evidence set auditable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Section
from ..parsing.structure import normalize_ws, strip_page_markers
from .outline import BidSection, BidOutline
from .tender import TenderContext

_TERM_SPLIT_RE = re.compile(
    r"[、，,；;：:（）()\[\]【】《》<>“”\"'/\|\\\s]+|与|和|及|以及|含"
)
_STOP_TERMS = {
    "工程",
    "项目",
    "投标",
    "文件",
    "要求",
    "措施",
    "方案",
    "相关",
    "有关",
    "进行",
    "以及",
    "其他",
    "主要",
    "工作",
    "内容",
}


@dataclass(slots=True)
class Evidence:
    ref: str
    kind: str
    title: str
    text: str
    page_start: int = 0
    page_end: int = 0
    score: float = 0.0

    @property
    def citation(self) -> str:
        if self.page_start and self.page_end:
            pages = (
                f"第{self.page_start}页"
                if self.page_start == self.page_end
                else f"第{self.page_start}-{self.page_end}页"
            )
            return f"{self.title}（{pages}）"
        return self.title

    def to_prompt(self) -> str:
        return f"[{self.ref}] {self.kind}｜{self.citation}\n{self.text.strip()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "title": self.title,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "score": round(self.score, 3),
            "excerpt": self.text[:500],
        }


@dataclass(slots=True)
class _SectionCard:
    section: Section
    visible: str
    title_norm: str
    path_norm: str
    norm: str


@dataclass(slots=True)
class _RequirementRecord:
    kind: str
    label: str
    text: str
    quote: str
    role: str
    factor_category: str = ""
    page: tuple[int, int] | None = None
    _search: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self._search = normalize_ws(f"{self.label}{self.text}")

    @property
    def citation(self) -> str:
        if self.page:
            start, end = self.page
            pages = f"第{start}页" if start == end else f"第{start}-{end}页"
            return f"{self.label}（{pages}）"
        return self.label


def _terms(plan: BidSection) -> list[str]:
    terms: list[str] = []
    candidates = [*plan.keywords, *plan.title.split("、")]
    for candidate in candidates:
        for term in _TERM_SPLIT_RE.split(candidate):
            term = (term or "").strip()
            if len(term) < 2 or term in _STOP_TERMS:
                continue
            if term not in terms:
                terms.append(term[:30])
    # Keep prompts and scoring bounded.  Plan keywords are ordered by importance.
    return terms[:40]


def _paragraphs(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    if len(parts) >= 3:
        return parts
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if len(lines) <= 1:
        return parts or ([normalized.strip()] if normalized.strip() else [])
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        current.append(line)
        length += len(line)
        if length >= 240:
            chunks.append("\n".join(current))
            current = []
            length = 0
    if current:
        chunks.append("\n".join(current))
    return chunks


def _excerpt(text: str, terms: list[str], limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return text[:limit]
    scored: list[tuple[int, float, str]] = []
    for index, paragraph in enumerate(paragraphs):
        hits = sum(paragraph.count(term) for term in terms)
        score = hits + (2.0 if index == 0 else 0.0)
        scored.append((index, score, paragraph))
    positive = [item for item in scored if item[1] > 0]
    selected = sorted(positive or scored[:2], key=lambda item: (-item[1], item[0]))
    chosen: list[tuple[int, str]] = []
    total = 0
    for index, _score, paragraph in selected:
        if chosen and total + len(paragraph) > limit:
            continue
        if not chosen and len(paragraph) > limit:
            paragraph = paragraph[:limit]
        chosen.append((index, paragraph))
        total += len(paragraph)
        if total >= limit * 0.9:
            break
    chosen.sort(key=lambda item: item[0])
    if not chosen:
        return text[:limit]
    joiner = "\n……\n" if len(chosen) > 1 else "\n"
    result = joiner.join(paragraph for _index, paragraph in chosen)
    if len(result) > limit:
        result = result[:limit] + "……"
    return result


def _role_of_factor(category: str) -> str:
    if category in {"技术", "商务", "报价", "其他"}:
        return "technical" if category == "技术" else "commercial"
    text = category or ""
    if any(keyword in text for keyword in ("技术", "质量", "安全", "进度", "施工")):
        return "technical"
    return "commercial"


class TenderRetriever:
    def __init__(self, tender: TenderContext) -> None:
        self.tender = tender
        self.cards: list[_SectionCard] = []
        for section in tender.sections:
            visible, _pages = strip_page_markers(section.text)
            visible = visible.strip()
            if len(visible) < 20:
                continue
            path = " > ".join(section.path)
            self.cards.append(
                _SectionCard(
                    section=section,
                    visible=visible,
                    title_norm=normalize_ws(section.title),
                    path_norm=normalize_ws(path),
                    norm=normalize_ws(visible),
                )
            )
        self.records = self._requirement_records()

    def _quote_page(self, quote: str) -> tuple[int, int] | None:
        return self.tender.locate_quote(quote)

    def _requirement_records(self) -> list[_RequirementRecord]:
        records: list[_RequirementRecord] = []
        for item in self.tender.evaluation_factors:
            factor = str(item.get("factor") or "").strip()
            if not factor:
                continue
            category = str(item.get("category") or "其他")
            score = item.get("max_score")
            score_text = f"{score:g}分" if isinstance(score, (int, float)) else "分值未提取"
            criteria = str(item.get("criteria") or "").strip()
            quote = str(item.get("source_quote") or "").strip()
            records.append(
                _RequirementRecord(
                    kind="评分因素",
                    label=f"[{category}] {factor}（{score_text}）",
                    text=criteria or factor,
                    quote=quote,
                    role=_role_of_factor(category),
                    factor_category=category,
                    page=self._quote_page(quote) if quote else None,
                )
            )
        for item in self.tender.qualifications:
            requirement = str(item.get("requirement") or "").strip()
            if not requirement:
                continue
            thresholds = "；".join(str(value) for value in item.get("thresholds") or [])
            quote = str(item.get("source_quote") or "").strip()
            records.append(
                _RequirementRecord(
                    kind="资格条件",
                    label=f"[{item.get('category') or '其他'}] 资格审查",
                    text=f"{requirement}" + (f"（门槛：{thresholds}）" if thresholds else ""),
                    quote=quote,
                    role="commercial",
                    page=self._quote_page(quote) if quote else None,
                )
            )
        for item in self.tender.bid_format_items:
            title = str(item.get("title") or "").strip()
            requirement = str(item.get("requirements") or "").strip()
            if not title:
                continue
            records.append(
                _RequirementRecord(
                    kind="格式要求",
                    label=f"投标文件格式：{title}",
                    text=requirement or title,
                    quote="",
                    role="admin",
                )
            )
        for item in self.tender.technical_requirements:
            topic = str(item.get("topic") or "").strip()
            requirement = str(item.get("requirement") or "").strip()
            if not topic and not requirement:
                continue
            quote = str(item.get("source_quote") or "").strip()
            records.append(
                _RequirementRecord(
                    kind="技术标准",
                    label=f"技术标准：{topic or '要求'}",
                    text=requirement or topic,
                    quote=quote,
                    role="technical",
                    page=self._quote_page(quote) if quote else None,
                )
            )
        for item in self.tender.contract_points:
            topic = str(item.get("topic") or "").strip()
            detail = str(item.get("detail") or "").strip()
            if not topic and not detail:
                continue
            quote = str(item.get("source_quote") or "").strip()
            records.append(
                _RequirementRecord(
                    kind="合同条款",
                    label=f"合同条款：{topic or '要点'}",
                    text=detail or topic,
                    quote=quote,
                    role="technical" if any(
                        keyword in f"{topic}{detail}"
                        for keyword in ("工期", "质量", "安全", "环保", "验收", "保修", "进度")
                    ) else "commercial",
                    page=self._quote_page(quote) if quote else None,
                )
            )
        for text in self.tender.other_requirements:
            if not text.strip():
                continue
            role = "technical" if any(
                keyword in text for keyword in ("施工", "安全", "质量", "环保", "技术", "现场")
            ) else "admin"
            records.append(
                _RequirementRecord(
                    kind="其他要求",
                    label="招标文件其他要求",
                    text=text,
                    quote="",
                    role=role,
                )
            )
        for item in self.tender.preliminary_review:
            stage = str(item.get("stage") or "初步评审")
            name = str(item.get("item") or "").strip()
            criteria = str(item.get("criteria") or "").strip()
            if not name and not criteria:
                continue
            quote = str(item.get("source_quote") or "").strip()
            records.append(
                _RequirementRecord(
                    kind="评审要求",
                    label=f"{stage}：{name or '评审因素'}",
                    text=criteria or name,
                    quote=quote,
                    role="admin",
                    page=self._quote_page(quote) if quote else None,
                )
            )
        # Preserve a stable order and drop exact text duplicates.
        seen: set[str] = set()
        unique: list[_RequirementRecord] = []
        for record in records:
            key = normalize_ws(record.text)[:300]
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(record)
        return unique

    def _score_record(self, record: _RequirementRecord, terms: list[str], role: str) -> float:
        score = 0.0
        for term in terms:
            if term in record.label:
                score += 5.0
            count = record._search.count(term)
            if count:
                score += min(count, 4) * 1.5
        if record.role == role:
            score += 1.5
        if record.kind in {"评分因素", "资格条件", "格式要求"}:
            score += 0.5
        return score

    def _select_records(
        self, plan: BidSection, terms: list[str]
    ) -> tuple[list[_RequirementRecord], list[str]]:
        warnings: list[str] = []
        scored = [
            (self._score_record(record, terms, plan.role), index, record)
            for index, record in enumerate(self.records)
        ]
        positives = [item for item in scored if item[0] > 0]
        if positives:
            positives.sort(key=lambda item: (-item[0], item[1]))
            selected = [record for _score, _index, record in positives[:8]]
        else:
            fallback = [record for record in self.records if record.role == plan.role][:6]
            if not fallback:
                fallback = self.records[:5]
            selected = fallback
            if fallback:
                warnings.append(f"章节“{plan.title}”没有关键词命中的抽取条目，使用同角色条目兜底")
        # Keep one copy of each record and respect the character budget.
        result: list[_RequirementRecord] = []
        total = 0
        for record in selected:
            length = len(record.text)
            if result and total + length > 4200:
                continue
            result.append(record)
            total += length
            if total >= 3600:
                break
        return result, warnings

    def _select_sections(
        self, plan: BidSection, terms: list[str]
    ) -> tuple[list[tuple[float, _SectionCard]], list[str]]:
        warnings: list[str] = []
        scored: list[tuple[float, int, _SectionCard]] = []
        for index, card in enumerate(self.cards):
            score = 0.0
            for term in terms:
                if term in card.title_norm:
                    score += 18.0
                elif term in card.path_norm:
                    score += 8.0
                count = card.visible.count(term)
                if count:
                    score += math.log1p(min(count, 20)) * 4.0
            if score > 0:
                scored.append((score, index, card))
        if not scored:
            warnings.append(f"章节“{plan.title}”未检索到招标文件原文，正文需依赖结构化抽取结果")
            return [], warnings
        scored.sort(key=lambda item: (-item[0], item[1]))
        # Avoid five near-identical hits from one chapter subtree.
        selected: list[tuple[float, _SectionCard]] = []
        seen_titles: set[str] = set()
        for score, _index, card in scored:
            title_key = card.title_norm
            if title_key in seen_titles:
                continue
            if len(card.visible) < 35:
                continue
            selected.append((score, card))
            seen_titles.add(title_key)
            if len(selected) >= 4:
                break
        return selected, warnings

    def retrieve(self, plan: BidSection) -> tuple[list[Evidence], list[str]]:
        terms = _terms(plan)
        warnings: list[str] = []
        evidence: list[Evidence] = []
        ref = 1

        records, record_warnings = self._select_records(plan, terms)
        warnings.extend(record_warnings)
        for record in records:
            evidence.append(
                Evidence(
                    ref=f"R{ref}",
                    kind=record.kind,
                    title=record.citation,
                    text=record.text.strip(),
                    page_start=record.page[0] if record.page else 0,
                    page_end=record.page[1] if record.page else 0,
                    score=1.0,
                )
            )
            ref += 1

        sections, section_warnings = self._select_sections(plan, terms)
        warnings.extend(section_warnings)
        for score, card in sections:
            excerpt = _excerpt(card.visible, terms, 2600)
            if not excerpt:
                continue
            evidence.append(
                Evidence(
                    ref=f"T{len(evidence) + 1}",
                    kind="招标文件原文",
                    title=" > ".join([*card.section.path, card.section.title]),
                    text=excerpt,
                    page_start=card.section.page_start,
                    page_end=card.section.page_end,
                    score=score,
                )
            )
        return evidence, warnings

    def retrieve_all(
        self, outline: BidOutline
    ) -> tuple[dict[str, list[Evidence]], list[str]]:
        bundles: dict[str, list[Evidence]] = {}
        warnings: list[str] = []
        for plan in outline.sections:
            items, item_warnings = self.retrieve(plan)
            bundles[plan.id] = items
            for warning in item_warnings:
                if warning not in warnings:
                    warnings.append(warning)
        return bundles, warnings


def format_evidence(evidence: list[Evidence], *, max_chars: int = 9000) -> str:
    if not evidence:
        return "（无检索到的招标文件原文；只能使用上方项目信息和结构化抽取结果，缺失事实用【待核对：...】）"
    blocks: list[str] = []
    total = 0
    for item in evidence:
        block = item.to_prompt()
        if blocks and total + len(block) > max_chars:
            break
        if not blocks and len(block) > max_chars:
            block = block[:max_chars] + "……"
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks)
