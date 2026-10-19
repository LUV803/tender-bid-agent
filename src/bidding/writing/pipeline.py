"""End-to-end bid-writing pipeline: load inputs, run agents, write artifacts."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import Settings, get_settings
from ..models import now_iso
from ..storage import hydrate_document, mirror_bid_artifacts
from .agents import BidAgents
from .errors import BidWriteError
from .graph import run_writing_graph
from .llm import BidLLM, OpenAIBidLLM
from .outline import BidOutline
from .profile import BidderProfile, load_profile
from .render import (
    build_review_markdown,
    collect_pending_markers,
    export_docx,
)
from .retrieval import TenderRetriever
from .tender import TenderContext, load_tender_context

ProgressCallback = Callable[[str], None]


@dataclass(slots=True)
class BidWriteResult:
    doc_id: str
    output_dir: Path
    markdown_path: Path
    docx_path: Path
    review_path: Path
    manifest_path: Path
    manifest: dict[str, Any]


def default_profile_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.upload_dir / "bidder_profile.json"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _write_text_atomic(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _llm_info(settings: Settings, llm: BidLLM) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    raw_records = getattr(llm, "records", None) or []
    for record in raw_records:
        if hasattr(record, "to_dict"):
            records.append(record.to_dict())
        elif isinstance(record, dict):
            records.append(record)
    return {
        "provider": getattr(llm, "provider", settings.llm_provider),
        "model": getattr(llm, "model", settings.llm_model),
        "calls": int(getattr(llm, "calls", len(records))),
        "records": records,
    }


def _build_manifest(
    *,
    settings: Settings,
    tender: TenderContext,
    profile: BidderProfile,
    outline: BidOutline,
    state: dict[str, Any],
    llm_info: dict[str, Any],
    markdown: str,
    artifacts: dict[str, Path],
    output_dir: Path,
    created_at: str,
    elapsed_seconds: float,
    max_revisions: int,
    max_sections: int,
) -> dict[str, Any]:
    pending = collect_pending_markers(markdown)
    review = state.get("review") or {}
    issues = review.get("issues") or []
    draft_chars = sum(len(text) for text in (state.get("drafts") or {}).values())
    return {
        "doc_id": tender.doc_id,
        "created_at": created_at,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "project": dict(tender.project),
        "source": {
            "name": tender.source_name,
            "parsed_at": tender.parsed_at,
            "extraction_partial": tender.extraction_partial,
            "manifest": tender.manifest.get("artifacts_dir", ""),
        },
        "bidder": {
            "source_path": str(profile.source_path) if profile.source_path else "",
            "sha256": profile.sha256,
            "company_name": profile.company_name,
            "provided_field_count": profile.provided_field_count,
            "gap_count": len(profile.gaps),
        },
        "llm": llm_info,
        "settings": {
            "max_revisions": max_revisions,
            "max_sections": max_sections,
            "output_dir": settings.relative(output_dir),
        },
        "artifacts": {
            key: settings.relative(path) for key, path in artifacts.items()
        },
        "stats": {
            "sections": len(outline.sections),
            "draft_chars": draft_chars,
            "document_chars": len(markdown),
            "pending_markers": len(pending),
            "issues": len(issues),
            "high_issues": sum(1 for item in issues if item.get("severity") == "high"),
            "medium_issues": sum(
                1 for item in issues if item.get("severity") == "medium"
            ),
            "low_issues": sum(1 for item in issues if item.get("severity") == "low"),
            "revisions": len(state.get("revisions") or []),
        },
        "pending_markers": [
            {"marker": item.marker, "sections": item.sections} for item in pending
        ],
        "profile_gaps": [
            {
                "label": gap.label,
                "placeholder": gap.placeholder,
                "section": gap.section,
                "path": gap.path,
            }
            for gap in profile.gaps
        ],
        "outline": {
            "sections": [
                {
                    "id": section.id,
                    "title": section.title,
                    "role": section.role,
                    "origin": section.origin,
                    "word_target": section.word_target,
                    "tables": section.tables,
                }
                for section in outline.sections
            ],
            "project_summary": outline.project_summary,
            "risks": outline.risks,
        },
        "response_matrix": state.get("matrix") or [],
        "review": review,
        "revisions": state.get("revisions") or [],
        "warnings": _dedupe(list(state.get("warnings") or [])),
        "trace": state.get("trace") or [],
    }


def write_bid(
    doc_id: str,
    *,
    profile_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    settings: Settings | None = None,
    llm: BidLLM | None = None,
    max_revisions: int = 2,
    max_sections: int | None = None,
    enable_revision: bool = True,
    progress: ProgressCallback | None = None,
) -> BidWriteResult:
    """Generate Markdown, Word and an internal review report for one tender.

    ``max_sections`` caps the analyst-added sections (0–3).  ``None`` keeps the
    CLI default of 3; the HTTP API passes 0 unless the client asks otherwise.
    The 11 canonical sections are always generated.
    """
    settings = settings or get_settings()
    settings.ensure_directories()
    progress = progress or (lambda _message: None)
    max_revisions = max(0, int(max_revisions))
    if not enable_revision:
        max_revisions = 0
    max_sections = 3 if max_sections is None else max(0, min(int(max_sections), 3))
    started = time.perf_counter()
    created_at = now_iso()

    profile_file = Path(profile_path) if profile_path else default_profile_path(settings)
    progress(f"读取投标人资料：{profile_file}")
    profile = load_profile(profile_file)
    progress(
        f"投标人资料已加载：{profile.company_name}；"
        f"缺失字段 {len(profile.gaps)} 项（将生成【待填写】占位符）"
    )

    progress(f"读取招标文件解析与抽取结果：{doc_id}")
    hydrate_document(doc_id, settings)
    tender = load_tender_context(doc_id, settings)
    progress(
        f"招标文件已加载：{tender.project_name}；"
        f"章节 {len(tender.sections)}，评分因素 {len(tender.evaluation_factors)}"
    )

    client = llm or OpenAIBidLLM(settings)
    retriever = TenderRetriever(tender)
    agents = BidAgents(client, profile, tender, retriever)
    progress(
        f"启动 LangGraph 多 Agent 流程：{getattr(client, 'provider', settings.llm_provider)}/"
        f"{getattr(client, 'model', settings.llm_model)}，"
        f"自动修订上限 {max_revisions}"
    )
    state = run_writing_graph(
        agents=agents,
        retriever=retriever,
        tender=tender,
        profile=profile,
        max_revisions=max_revisions,
        max_extra_sections=max_sections,
        progress=progress,
    )
    markdown = state.get("markdown") or ""
    if not markdown.strip():
        raise BidWriteError("多 Agent 流程没有生成投标文件正文")
    outline = state.get("outline")
    if outline is None:
        raise BidWriteError("多 Agent 流程没有生成投标文件大纲")

    output = Path(output_dir) if output_dir else settings.bids_dir / doc_id
    output = output.expanduser()
    if not output.is_absolute():
        output = (settings.project_root / output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    llm_info = _llm_info(settings, client)
    progress("生成 Markdown 与内部审查报告")
    review_markdown = build_review_markdown(
        tender=tender,
        profile=profile,
        outline=outline,
        matrix=state.get("matrix") or [],
        review=state.get("review") or {},
        revisions=state.get("revisions") or [],
        warnings=_dedupe(list(state.get("warnings") or [])),
        llm_info=llm_info,
        markdown=markdown,
        generated_at=created_at,
    )

    markdown_path = output / "bid.md"
    docx_path = output / "bid.docx"
    review_path = output / "bid_review.md"
    manifest_path = output / "bid_manifest.json"

    progress(f"导出 Word：{docx_path}")
    export_docx(
        markdown,
        docx_path,
        header_text=f"{tender.project_name} 投标文件",
    )

    manifest = _build_manifest(
        settings=settings,
        tender=tender,
        profile=profile,
        outline=outline,
        state=state,
        llm_info=llm_info,
        markdown=markdown,
        artifacts={
            "markdown": markdown_path,
            "docx": docx_path,
            "review": review_path,
            "manifest": manifest_path,
        },
        output_dir=output,
        created_at=created_at,
        elapsed_seconds=time.perf_counter() - started,
        max_revisions=max_revisions,
        max_sections=max_sections,
    )

    _write_text_atomic(markdown_path, markdown)
    _write_text_atomic(review_path, review_markdown)
    # Manifest is written last: its presence means the artifact set is complete.
    _write_text_atomic(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    mirror_bid_artifacts(doc_id, settings, output_dir=output)
    progress(f"投标文件已生成：{output}")
    return BidWriteResult(
        doc_id=doc_id,
        output_dir=output,
        markdown_path=markdown_path,
        docx_path=docx_path,
        review_path=review_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )
