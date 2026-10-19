"""LangGraph workflow for the multi-agent bid-writing pipeline.

The graph deliberately keeps the mandatory outline deterministic.  The analyst
agent may add a small number of extra sections and supply drafting briefs, but
it cannot remove sections required by the canonical bid structure.  Writers run
sequentially by role to keep provider rate limits and debugging predictable.
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import BidAgents, deterministic_review
from .outline import BidOutline, build_outline, build_response_matrix
from .profile import BidderProfile
from .render import PENDING_RE, build_bid_markdown, collect_pending_markers
from .retrieval import Evidence, TenderRetriever
from .tender import TenderContext

ProgressCallback = Callable[[str], None]


def _noop_progress(_message: str) -> None:
    return None


class BidState(TypedDict, total=False):
    analysis: dict[str, Any]
    outline: BidOutline
    matrix: list[dict[str, Any]]
    evidence: dict[str, list[Evidence]]
    drafts: dict[str, str]
    review: dict[str, Any]
    revision_targets: list[dict[str, Any]]
    revisions: list[dict[str, Any]]
    markdown: str
    warnings: Annotated[list[str], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]


def _entry(
    *,
    node: str,
    label: str,
    started: float,
    before_calls: int,
    llm: Any,
    details: dict[str, Any] | None = None,
    status: str = "ok",
) -> dict[str, Any]:
    calls = getattr(llm, "calls", 0)
    return {
        "node": node,
        "label": label,
        "status": status,
        "duration_seconds": round(time.perf_counter() - started, 2),
        "llm_calls": max(0, int(calls) - int(before_calls)),
        "details": details or {},
    }


def _revision_priority(issues: list[dict[str, Any]]) -> int:
    """Prefer fact/fabrication fixes over coverage polish when revisions are capped."""
    for issue in issues:
        category = str(issue.get("category") or "")
        problem = str(issue.get("problem") or "")
        if (
            category in {"fact", "事实依据"}
            or "未经投标人资料支持" in problem
            or "肯定句" in problem
            or "预算" in problem
        ):
            return 0
    return 1


def _section_factors(matrix: list[dict[str, Any]], section_id: str) -> list[str]:
    factors: list[str] = []
    for row in matrix:
        if section_id not in (row.get("section_ids") or []):
            continue
        factor = str(row.get("factor") or "").strip()
        if factor and factor not in factors:
            factors.append(factor)
    return factors


def _writer_node(
    role: str,
    label: str,
    *,
    agents: BidAgents,
    progress: ProgressCallback,
) -> Callable[[BidState], dict[str, Any]]:
    def node(state: BidState) -> dict[str, Any]:
        started = time.perf_counter()
        before_calls = getattr(agents.llm, "calls", 0)
        outline = state["outline"]
        analysis = state.get("analysis") or {}
        evidence = state.get("evidence") or {}
        matrix = state.get("matrix") or []
        sections = outline.for_role(role)
        drafts = dict(state.get("drafts") or {})
        warnings: list[str] = []
        failures = 0
        for index, section in enumerate(sections, start=1):
            progress(f"正在编写{label}（{index}/{len(sections)}）：{section.title}")
            try:
                text = agents.write_section(
                    section,
                    evidence.get(section.id, []),
                    analysis,
                    factors=_section_factors(matrix, section.id),
                )
            except Exception as exc:  # noqa: BLE001 - BidWriteError is imported lazily
                fatal = bool(getattr(exc, "fatal", False))
                if fatal:
                    raise
                failures += 1
                drafts[section.id] = (
                    f"【待核对：章节“{section.title}”生成失败（{exc}），"
                    "请重试或人工补写】"
                )
                warnings.append(f"章节“{section.title}”生成失败：{exc}")
                continue
            if not text.strip():
                failures += 1
                drafts[section.id] = f"【待核对：章节“{section.title}”生成结果为空】"
                warnings.append(f"章节“{section.title}”生成结果为空")
                continue
            drafts[section.id] = text.strip()
        details = {
            "sections": [section.id for section in sections],
            "failures": failures,
            "total_chars": sum(len(drafts.get(section.id, "")) for section in sections),
        }
        return {
            "drafts": drafts,
            "warnings": warnings,
            "trace": [
                _entry(
                    node=f"{role}_writer",
                    label=label,
                    started=started,
                    before_calls=before_calls,
                    llm=agents.llm,
                    details=details,
                    status="partial" if failures else "ok",
                )
            ],
        }

    return node


def build_bid_graph(
    *,
    agents: BidAgents,
    retriever: TenderRetriever,
    tender: TenderContext,
    profile: BidderProfile,
    max_revisions: int = 2,
    progress: ProgressCallback | None = None,
):
    progress = progress or _noop_progress
    max_revisions = max(0, int(max_revisions))

    def analyst_node(state: BidState) -> dict[str, Any]:
        del state
        started = time.perf_counter()
        before_calls = getattr(agents.llm, "calls", 0)
        progress("分析师 Agent：分析招标要求与编写策略")
        warnings = list(tender.warnings)
        status = "ok"
        try:
            analysis = agents.analyze()
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "fatal", False):
                raise
            analysis = {}
            status = "fallback"
            warnings.append(f"分析师 Agent 失败，使用默认大纲继续：{exc}")
        outline = build_outline(analysis, tender)
        matrix = build_response_matrix(outline, tender)
        warnings.extend(outline.warnings)
        details = {
            "sections": len(outline.sections),
            "extra_sections": [
                section.title for section in outline.sections if section.origin == "analyst"
            ],
            "strategy_items": len(outline.strategy),
            "matrix_items": len(matrix),
            "risks": len(outline.risks),
        }
        return {
            "analysis": analysis,
            "outline": outline,
            "matrix": matrix,
            "warnings": warnings,
            "trace": [
                _entry(
                    node="analyst",
                    label="分析师 Agent",
                    started=started,
                    before_calls=before_calls,
                    llm=agents.llm,
                    details=details,
                    status=status,
                )
            ],
        }

    def retrieval_node(state: BidState) -> dict[str, Any]:
        started = time.perf_counter()
        before_calls = getattr(agents.llm, "calls", 0)
        outline = state["outline"]
        progress("检索招标文件证据并分配引用")
        bundles, warnings = retriever.retrieve_all(outline)
        with_evidence = sum(1 for items in bundles.values() if items)
        details = {
            "sections": len(bundles),
            "sections_with_evidence": with_evidence,
            "evidence_items": sum(len(items) for items in bundles.values()),
        }
        return {
            "evidence": bundles,
            "warnings": warnings,
            "trace": [
                _entry(
                    node="retriever",
                    label="证据检索",
                    started=started,
                    before_calls=before_calls,
                    llm=agents.llm,
                    details=details,
                )
            ],
        }

    def review_node(state: BidState) -> dict[str, Any]:
        started = time.perf_counter()
        before_calls = getattr(agents.llm, "calls", 0)
        outline = state["outline"]
        drafts = state.get("drafts") or {}
        matrix = state.get("matrix") or []
        progress("审查 Agent：核对事实、评分响应与合规性")
        local_issues = deterministic_review(tender, outline, drafts, profile, matrix)
        warnings: list[str] = []
        status = "ok"
        try:
            review = agents.review(outline, drafts, matrix, local_issues)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "fatal", False):
                raise
            status = "fallback"
            review = {
                "summary": "审查 Agent 调用失败，以下结论仅来自确定性检查。",
                "issues": local_issues,
                "unverified_claims": [],
                "missing_requirements": [],
            }
            warnings.append(f"审查 Agent 失败，使用确定性检查结果：{exc}")

        issues = [item for item in review.get("issues") or [] if isinstance(item, dict)]
        claims = [
            item
            for item in review.get("unverified_claims") or []
            if isinstance(item, dict)
            and not PENDING_RE.search(str(item.get("claim") or ""))
        ]
        review["unverified_claims"] = claims
        outline_by_id = outline.by_id()
        known_problems = {
            str(item.get("problem") or "").strip()
            for item in issues
        }
        for claim in claims:
            section_id = str(claim.get("section_id") or "")
            if section_id not in outline_by_id:
                continue
            problem = f"未经投标人资料支持的事实：{str(claim.get('claim') or '')[:300]}"
            if problem in known_problems:
                continue
            issues.append(
                {
                    "severity": "high",
                    "section_id": section_id,
                    "category": "事实依据",
                    "problem": problem,
                    "fix": str(claim.get("reason") or "删除该事实或改为【待填写：...】占位"),
                    "tender_basis": "",
                }
            )
            known_problems.add(problem)
        review["issues"] = issues

        grouped: dict[str, list[dict[str, Any]]] = {}
        for issue in issues:
            if issue.get("severity") != "high":
                continue
            section_id = str(issue.get("section_id") or "")
            if section_id not in outline_by_id or section_id not in drafts:
                continue
            grouped.setdefault(section_id, []).append(issue)
        targets = []
        ordered = sorted(grouped.items(), key=lambda item: _revision_priority(item[1]))
        for section_id, section_issues in ordered:
            if len(targets) >= max_revisions:
                break
            targets.append(
                {
                    "section_id": section_id,
                    "section_title": outline_by_id[section_id].title,
                    "issues": section_issues,
                }
            )
        if len(grouped) > len(targets):
            warnings.append(
                f"高优先级问题涉及 {len(grouped)} 个章节，按上限仅自动修订 {len(targets)} 个"
            )
        details = {
            "summary": review.get("summary", "")[:300],
            "issues": len(issues),
            "high_issues": sum(1 for item in issues if item.get("severity") == "high"),
            "unverified_claims": len(claims),
            "revision_targets": [item["section_id"] for item in targets],
        }
        return {
            "review": review,
            "revision_targets": targets,
            "warnings": warnings,
            "trace": [
                _entry(
                    node="reviewer",
                    label="审查 Agent",
                    started=started,
                    before_calls=before_calls,
                    llm=agents.llm,
                    details=details,
                    status=status,
                )
            ],
        }

    def reviser_node(state: BidState) -> dict[str, Any]:
        started = time.perf_counter()
        before_calls = getattr(agents.llm, "calls", 0)
        outline = state["outline"]
        outline_by_id = outline.by_id()
        evidence = state.get("evidence") or {}
        matrix = state.get("matrix") or []
        targets = state.get("revision_targets") or []
        drafts = dict(state.get("drafts") or {})
        revisions: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, target in enumerate(targets, start=1):
            section_id = target["section_id"]
            section = outline_by_id.get(section_id)
            if section is None:
                continue
            progress(f"修订 Agent（{index}/{len(targets)}）：{section.title}")
            before = drafts.get(section_id, "")
            try:
                revised = agents.revise(
                    section,
                    before,
                    target.get("issues") or [],
                    evidence.get(section_id, []),
                    factors=_section_factors(matrix, section_id),
                )
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "fatal", False):
                    raise
                warnings.append(f"章节“{section.title}”自动修订失败，保留原稿：{exc}")
                continue
            if not revised.strip():
                warnings.append(f"章节“{section.title}”修订结果为空，保留原稿")
                continue
            drafts[section_id] = revised.strip()
            revisions.append(
                {
                    "section_id": section_id,
                    "section_title": section.title,
                    "issues": target.get("issues") or [],
                    "before_chars": len(before),
                    "after_chars": len(revised.strip()),
                }
            )
        details = {
            "requested": len(targets),
            "completed": len(revisions),
            "sections": [item["section_id"] for item in revisions],
        }
        return {
            "drafts": drafts,
            "revisions": revisions,
            "warnings": warnings,
            "trace": [
                _entry(
                    node="reviser",
                    label="修订 Agent",
                    started=started,
                    before_calls=before_calls,
                    llm=agents.llm,
                    details=details,
                    status="partial" if len(revisions) < len(targets) else "ok",
                )
            ],
        }

    def assembler_node(state: BidState) -> dict[str, Any]:
        started = time.perf_counter()
        before_calls = getattr(agents.llm, "calls", 0)
        progress("汇总章节、附录表格与评标响应索引")
        outline = state["outline"]
        markdown = build_bid_markdown(
            tender=tender,
            profile=profile,
            outline=outline,
            drafts=state.get("drafts") or {},
            matrix=state.get("matrix") or [],
        )
        pending = collect_pending_markers(markdown)
        details = {
            "chars": len(markdown),
            "pending_markers": len(pending),
            "sections": len(outline.sections),
        }
        return {
            "markdown": markdown,
            "trace": [
                _entry(
                    node="assembler",
                    label="文档汇总",
                    started=started,
                    before_calls=before_calls,
                    llm=agents.llm,
                    details=details,
                )
            ],
        }

    def after_review(state: BidState) -> str:
        return "reviser" if state.get("revision_targets") else "assembler"

    graph = StateGraph(BidState)
    graph.add_node("analyst", analyst_node)
    graph.add_node("retriever", retrieval_node)
    graph.add_node(
        "admin_writer", _writer_node("admin", "行政商务文件", agents=agents, progress=progress)
    )
    graph.add_node(
        "commercial_writer",
        _writer_node("commercial", "商务资料", agents=agents, progress=progress),
    )
    graph.add_node(
        "technical_writer",
        _writer_node("technical", "技术方案", agents=agents, progress=progress),
    )
    graph.add_node("reviewer", review_node)
    graph.add_node("reviser", reviser_node)
    graph.add_node("assembler", assembler_node)

    graph.add_edge(START, "analyst")
    graph.add_edge("analyst", "retriever")
    graph.add_edge("retriever", "admin_writer")
    graph.add_edge("admin_writer", "commercial_writer")
    graph.add_edge("commercial_writer", "technical_writer")
    graph.add_edge("technical_writer", "reviewer")
    graph.add_conditional_edges(
        "reviewer",
        after_review,
        {"reviser": "reviser", "assembler": "assembler"},
    )
    graph.add_edge("reviser", "assembler")
    graph.add_edge("assembler", END)
    return graph.compile()


def run_writing_graph(
    *,
    agents: BidAgents,
    retriever: TenderRetriever,
    tender: TenderContext,
    profile: BidderProfile,
    max_revisions: int = 2,
    progress: ProgressCallback | None = None,
) -> BidState:
    graph = build_bid_graph(
        agents=agents,
        retriever=retriever,
        tender=tender,
        profile=profile,
        max_revisions=max_revisions,
        progress=progress,
    )
    result = graph.invoke(
        {"warnings": [], "trace": [], "revisions": []},
        config={"recursion_limit": 40},
    )
    return result  # type: ignore[return-value]
