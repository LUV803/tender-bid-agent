"""Offline retrieval evaluation against a hand-labelled query set.

A gold file lists questions and the results a correct search should return.
A hit is relevant when it satisfies *all* constraints in one gold entry:

* ``kind`` — result kind such as ``table``, ``section`` or ``factor``;
* ``page`` — the hit's page range must contain this page;
* ``table_label`` — exact label such as ``表10``;
* ``title_contains`` — compact, case-insensitive title substring.

The evaluator only reads parsed artifacts and never calls an embedding or LLM
service in ``lexical`` mode, so it can run in CI.  Gold sets for real tenders
can contain project data; keep them under ``eval/private/`` or use the
``*.local.json`` suffix, both of which are ignored by Git.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import Settings
from .search import MAX_LIMIT, search_document

EVAL_MODES = ("auto", "lexical", "hybrid")


class EvaluationError(ValueError):
    """Raised when the gold file or evaluation settings are invalid."""


def _compact(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


@dataclass(frozen=True, slots=True)
class Relevance:
    """One acceptable result for a gold query."""

    kind: str | None = None
    page: int | None = None
    table_label: str | None = None
    title_contains: str | None = None

    @classmethod
    def from_dict(cls, payload: Any, *, context: str) -> "Relevance":
        if not isinstance(payload, dict) or not payload:
            raise EvaluationError(f"{context} 必须是非空对象")
        allowed = {"kind", "page", "table_label", "title_contains", "note"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise EvaluationError(f"{context} 包含未知字段：{', '.join(unknown)}")

        kind = payload.get("kind")
        if kind is not None:
            if not isinstance(kind, str) or not kind.strip():
                raise EvaluationError(f"{context}.kind 必须是非空字符串")
            kind = kind.strip()

        page = payload.get("page")
        if page is not None:
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise EvaluationError(f"{context}.page 必须是大于 0 的整数")

        table_label = payload.get("table_label")
        if table_label is not None:
            if not isinstance(table_label, str) or not table_label.strip():
                raise EvaluationError(f"{context}.table_label 必须是非空字符串")
            table_label = table_label.strip()

        title_contains = payload.get("title_contains")
        if title_contains is not None:
            if not isinstance(title_contains, str) or not title_contains.strip():
                raise EvaluationError(f"{context}.title_contains 必须是非空字符串")
            title_contains = title_contains.strip()

        if all(
            value is None
            for value in (kind, page, table_label, title_contains)
        ):
            raise EvaluationError(
                f"{context} 至少需要 kind、page、table_label 或 title_contains 之一"
            )
        return cls(
            kind=kind,
            page=page,
            table_label=table_label,
            title_contains=title_contains,
        )

    def matches(self, hit: Mapping[str, Any]) -> bool:
        if self.kind is not None and str(hit.get("kind") or "") != self.kind:
            return False
        if self.table_label is not None:
            if _compact(hit.get("table_label")) != _compact(self.table_label):
                return False
        if self.title_contains is not None:
            if _compact(self.title_contains) not in _compact(hit.get("title")):
                return False
        if self.page is not None:
            start = hit.get("page_start")
            end = hit.get("page_end")
            if isinstance(start, bool) or not isinstance(start, int):
                return False
            if isinstance(end, bool) or not isinstance(end, int):
                end = start
            if not start <= self.page <= end:
                return False
        return True


@dataclass(frozen=True, slots=True)
class GoldQuery:
    id: str
    query: str
    relevant: tuple[Relevance, ...]


@dataclass(frozen=True, slots=True)
class GoldSet:
    doc_id: str
    queries: tuple[GoldQuery, ...]
    description: str = ""


def load_gold(path: str | Path) -> GoldSet:
    """Load and validate a gold query set."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvaluationError(f"无法读取评测集：{source}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"评测集不是合法 JSON：{source} ({exc.msg})") from exc

    if not isinstance(payload, dict):
        raise EvaluationError("评测集顶层必须是对象")
    allowed = {"doc_id", "description", "queries"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise EvaluationError(f"评测集包含未知字段：{', '.join(unknown)}")

    doc_id = payload.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise EvaluationError("评测集缺少非空 doc_id")

    description = payload.get("description") or ""
    if not isinstance(description, str):
        raise EvaluationError("评测集 description 必须是字符串")

    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise EvaluationError("评测集 queries 必须是非空数组")

    queries: list[GoldQuery] = []
    seen_ids: set[str] = set()
    for position, item in enumerate(raw_queries, start=1):
        context = f"queries[{position}]"
        if not isinstance(item, dict):
            raise EvaluationError(f"{context} 必须是对象")
        unknown = sorted(set(item) - {"id", "query", "relevant", "note"})
        if unknown:
            raise EvaluationError(f"{context} 包含未知字段：{', '.join(unknown)}")

        query_id = item.get("id")
        if query_id is None:
            query_id = f"q{position}"
        if not isinstance(query_id, str) or not query_id.strip():
            raise EvaluationError(f"{context}.id 必须是非空字符串")
        query_id = query_id.strip()
        if query_id in seen_ids:
            raise EvaluationError(f"重复的查询 id：{query_id}")
        seen_ids.add(query_id)

        query = item.get("query")
        if not isinstance(query, str) or not query.strip():
            raise EvaluationError(f"{context}.query 必须是非空字符串")

        raw_relevant = item.get("relevant")
        if not isinstance(raw_relevant, list) or not raw_relevant:
            raise EvaluationError(f"{context}.relevant 必须是非空数组")
        relevant = tuple(
            Relevance.from_dict(entry, context=f"{context}.relevant[{index}]")
            for index, entry in enumerate(raw_relevant, start=1)
        )
        queries.append(GoldQuery(id=query_id, query=query.strip(), relevant=relevant))

    return GoldSet(
        doc_id=doc_id.strip(),
        queries=tuple(queries),
        description=description.strip(),
    )


@dataclass(frozen=True, slots=True)
class QueryEval:
    id: str
    query: str
    rank: int | None
    matched: int
    relevant: int
    elapsed_ms: int
    result_count: int
    response_mode: str
    vector_used: bool
    warnings: tuple[str, ...]
    top_title: str
    top_page_hint: str
    top_table_label: str

    @property
    def recall(self) -> float:
        return self.matched / self.relevant if self.relevant else 0.0

    @property
    def mrr(self) -> float:
        return 1.0 / self.rank if self.rank else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "query": self.query,
            "rank": self.rank,
            "hit": self.rank is not None,
            "matched": self.matched,
            "relevant": self.relevant,
            "recall": round(self.recall, 4),
            "mrr": round(self.mrr, 4),
            "elapsed_ms": self.elapsed_ms,
            "result_count": self.result_count,
            "response_mode": self.response_mode,
            "vector_used": self.vector_used,
            "top_title": self.top_title,
            "top_page_hint": self.top_page_hint,
            "top_table_label": self.top_table_label,
            "warnings": list(self.warnings),
        }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100.0 * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


def evaluate(
    settings: Settings,
    gold: GoldSet,
    *,
    doc_id: str | None = None,
    limit: int = 10,
    mode: str = "lexical",
    k: int = 5,
) -> dict[str, Any]:
    """Run every gold query and return per-query rows plus aggregate metrics."""
    effective_doc_id = (doc_id or gold.doc_id).strip()
    if not effective_doc_id:
        raise EvaluationError("doc_id 不能为空")

    effective_mode = (mode or "lexical").strip().lower()
    if effective_mode not in EVAL_MODES:
        raise EvaluationError("mode 必须是 auto、lexical 或 hybrid")

    effective_limit = max(1, min(int(limit), MAX_LIMIT))
    effective_k = max(1, min(int(k), effective_limit))

    rows: list[QueryEval] = []
    source_name = ""
    project_name = ""
    for item in gold.queries:
        response = search_document(
            settings,
            effective_doc_id,
            item.query,
            limit=effective_limit,
            mode=effective_mode,
        )
        source_name = source_name or str(response.get("source_name") or "")
        project_name = project_name or str(response.get("project_name") or "")
        results = list(response.get("results") or [])
        # All ranking metrics use the same cutoff.  ``limit`` only controls
        # how many results the search returns; ``k`` is the evaluation cutoff.
        cutoff = results[:effective_k]

        matched_items: set[int] = set()
        rank: int | None = None
        for position, hit in enumerate(cutoff, start=1):
            hit_matched = False
            for index, relevance in enumerate(item.relevant):
                if index in matched_items:
                    continue
                if relevance.matches(hit):
                    matched_items.add(index)
                    hit_matched = True
            if hit_matched and rank is None:
                rank = position

        top = results[0] if results else {}
        rows.append(
            QueryEval(
                id=item.id,
                query=item.query,
                rank=rank,
                matched=len(matched_items),
                relevant=len(item.relevant),
                elapsed_ms=int(response.get("elapsed_ms") or 0),
                result_count=len(results),
                response_mode=str(response.get("mode") or ""),
                vector_used=bool(response.get("vector_used")),
                warnings=tuple(str(value) for value in response.get("warnings") or []),
                top_title=str(top.get("title") or ""),
                top_page_hint=str(top.get("page_hint") or ""),
                top_table_label=str(top.get("table_label") or ""),
            )
        )

    latencies = [row.elapsed_ms for row in rows]
    warning_messages = sorted({message for row in rows for message in row.warnings})
    summary = {
        "queries": len(rows),
        "hit_at_1": _mean([1.0 if row.rank == 1 else 0.0 for row in rows]),
        "hit_at_3": _mean(
            [1.0 if row.rank is not None and row.rank <= 3 else 0.0 for row in rows]
        ),
        "hit_at_k": _mean([1.0 if row.rank is not None else 0.0 for row in rows]),
        "recall_at_k": _mean([row.recall for row in rows]),
        "mrr": _mean([row.mrr for row in rows]),
        "top1_accuracy": _mean([1.0 if row.rank == 1 else 0.0 for row in rows]),
        "avg_elapsed_ms": round(_mean([float(value) for value in latencies]), 1),
        "p95_elapsed_ms": _percentile(latencies, 95),
        "empty_results": sum(1 for row in rows if not row.result_count),
        "vector_used": any(row.vector_used for row in rows),
        "warning_messages": warning_messages,
    }
    return {
        "doc_id": effective_doc_id,
        "source_name": source_name,
        "project_name": project_name,
        "description": gold.description,
        "mode": effective_mode,
        "k": effective_k,
        "limit": effective_limit,
        "summary": summary,
        "queries": [row.as_dict() for row in rows],
    }


def format_report(result: Mapping[str, Any]) -> str:
    """Render a compact table followed by aggregate metrics."""
    summary = result["summary"]
    k = result["k"]
    lines = [
        (
            f"Retrieval evaluation · {result['doc_id']} · "
            f"{summary['queries']} queries · mode={result['mode']} · k={k}"
        )
    ]
    if result.get("source_name"):
        document = result["source_name"]
        if result.get("project_name"):
            document = f"{document} / {result['project_name']}"
        lines.append(f"Document: {document}")
    lines.append("")
    header = f"{'id':<24} {'rank':>4} {'recall':>7} {'top1':>5} {'ms':>6}  query"
    lines.append(header)
    lines.append("-" * 60)
    for row in result["queries"]:
        rank = "-" if row["rank"] is None else str(row["rank"])
        top1 = "yes" if row["rank"] == 1 else "no"
        lines.append(
            f"{row['id']:<24} {rank:>4} "
            f"{row['matched']}/{row['relevant']:<5} {top1:>5} "
            f"{row['elapsed_ms']:>6}  {row['query']}"
        )
        if row["rank"] is None and row["top_title"]:
            page = f" {row['top_page_hint']}" if row["top_page_hint"] else ""
            lines.append(f"{'':<24} top result: {row['top_title']}{page}")

    lines.append("")
    lines.append(
        f"Hit@1 {summary['hit_at_1']:.1%} · "
        f"Hit@3 {summary['hit_at_3']:.1%} · "
        f"Hit@{k} {summary['hit_at_k']:.1%} · "
        f"Recall@{k} {summary['recall_at_k']:.1%} · "
        f"MRR {summary['mrr']:.3f}"
    )
    lines.append(
        f"Avg {summary['avg_elapsed_ms']} ms · "
        f"P95 {summary['p95_elapsed_ms']} ms · "
        f"empty {summary['empty_results']} · "
        f"warnings {len(summary['warning_messages'])}"
    )
    for warning in summary["warning_messages"][:3]:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Optional minimum values; each check is skipped when the value is None."""

    hit_at_1: float | None = None
    hit_at_3: float | None = None
    recall_at_k: float | None = None
    mrr: float | None = None


def check_thresholds(result: Mapping[str, Any], thresholds: Thresholds) -> list[str]:
    """Return human-readable failures for metrics below their minimums."""
    summary = result["summary"]
    checks = (
        ("Hit@1", summary["hit_at_1"], thresholds.hit_at_1),
        ("Hit@3", summary["hit_at_3"], thresholds.hit_at_3),
        (f"Recall@{result['k']}", summary["recall_at_k"], thresholds.recall_at_k),
        ("MRR", summary["mrr"], thresholds.mrr),
    )
    failures: list[str] = []
    for name, actual, minimum in checks:
        if minimum is None:
            continue
        if not 0.0 <= minimum <= 1.0:
            raise EvaluationError(f"{name} 阈值必须在 0 到 1 之间")
        if actual + 1e-9 < minimum:
            failures.append(
                f"{name} {actual:.1%} 低于要求的 {minimum:.1%}"
            )
    return failures
