"""Read-only views over parsed tenders and generated bid artifacts.

The HTTP layer only exposes files that live directly under
``upload/bids/<doc_id>/`` and only the four known pipeline outputs.  Listing
scans the filesystem instead of maintaining a database so documents produced
by the CLI show up in the desktop app immediately.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bidding.config import Settings
from bidding.parsing.structure import sanitize_doc_id

BID_FILENAMES: dict[str, str] = {
    "markdown": "bid.md",
    "docx": "bid.docx",
    "review": "bid_review.md",
    "manifest": "bid_manifest.json",
}
ARTIFACT_LABELS: dict[str, str] = {
    "markdown": "投标文件 Markdown",
    "docx": "投标文件 Word",
    "review": "内部审查报告",
    "manifest": "生成清单",
}
ARTIFACT_MEDIA_TYPES: dict[str, str] = {
    "bid.md": "text/markdown; charset=utf-8",
    "bid_review.md": "text/markdown; charset=utf-8",
    "bid.docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    "bid_manifest.json": "application/json; charset=utf-8",
}
ARTIFACT_NAMES = frozenset(BID_FILENAMES.values())


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _timestamp(path: Path) -> str:
    try:
        moment = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return ""
    return moment.isoformat(timespec="seconds")


def _safe_bid_dir(settings: Settings, doc_id: str) -> Path | None:
    """Return a bid directory only for a canonical, direct child doc_id."""
    if not doc_id or sanitize_doc_id(doc_id) != doc_id:
        return None
    root = settings.bids_dir.resolve()
    candidate = (root / doc_id).resolve()
    if candidate.parent != root or not candidate.is_dir():
        return None
    return candidate


def artifact_path(settings: Settings, doc_id: str, filename: str) -> Path | None:
    """Resolve one allow-listed artifact without leaving the bids directory."""
    if filename not in ARTIFACT_NAMES:
        return None
    bid_dir = _safe_bid_dir(settings, doc_id)
    if bid_dir is None:
        return None
    path = (bid_dir / filename).resolve()
    try:
        path.relative_to(settings.bids_dir.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def media_type_for(filename: str) -> str:
    return ARTIFACT_MEDIA_TYPES.get(filename, "application/octet-stream")


def _artifact_entries(doc_id: str, bid_dir: Path) -> list[dict[str, Any]]:
    encoded = quote(doc_id, safe="")
    entries: list[dict[str, Any]] = []
    for key, filename in BID_FILENAMES.items():
        path = bid_dir / filename
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append(
            {
                "key": key,
                "name": filename,
                "label": ARTIFACT_LABELS[key],
                "size": size,
                "previewable": filename.endswith(".md"),
                "content_url": f"/api/bids/{encoded}/files/{filename}",
                "download_url": f"/api/bids/{encoded}/files/{filename}?download=1",
            }
        )
    return entries


def bid_summary(settings: Settings, doc_id: str) -> dict[str, Any] | None:
    """Compact manifest-backed summary for one generated bid."""
    bid_dir = _safe_bid_dir(settings, doc_id)
    if bid_dir is None:
        return None
    manifest = _read_json(bid_dir / "bid_manifest.json")
    if manifest is None:
        return None
    project = _mapping(manifest.get("project"))
    bidder = _mapping(manifest.get("bidder"))
    stats = _mapping(manifest.get("stats"))
    settings_data = _mapping(manifest.get("settings"))
    artifacts = _artifact_entries(doc_id, bid_dir)
    return {
        "doc_id": doc_id,
        "project_name": str(project.get("project_name") or ""),
        "bidder": str(bidder.get("company_name") or ""),
        "created_at": str(manifest.get("created_at") or _timestamp(bid_dir)),
        "elapsed_seconds": _number(manifest.get("elapsed_seconds")),
        "stats": stats,
        "settings": settings_data,
        "artifacts": artifacts,
        "folder": str(bid_dir),
        "warnings": [str(item) for item in _list(manifest.get("warnings"))],
    }


def list_bid_history(settings: Settings) -> list[dict[str, Any]]:
    root = settings.bids_dir
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        summary = bid_summary(settings, child.name)
        if summary is not None:
            items.append(summary)
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def _extraction_meta(extraction: dict[str, Any] | None) -> dict[str, Any]:
    return _mapping((extraction or {}).get("extraction_meta"))


def list_documents(settings: Settings) -> list[dict[str, Any]]:
    """List every directory under ``upload/parsed`` with a parse manifest."""
    root = settings.parsed_dir
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir() or not (child / "manifest.json").is_file():
            continue
        doc_id = child.name
        if sanitize_doc_id(doc_id) != doc_id:
            continue
        manifest = _read_json(child / "manifest.json")
        if manifest is None:
            continue
        extraction = _read_json(child / "extraction.json")
        meta = _extraction_meta(extraction)
        bid = bid_summary(settings, doc_id)
        evaluation_factors = _list((extraction or {}).get("evaluation_factors"))
        ungrounded = _list((extraction or {}).get("ungrounded_fields"))
        items.append(
            {
                "doc_id": doc_id,
                "source_name": str(manifest.get("source_name") or ""),
                "project_name": str(
                    (extraction or {}).get("project_name")
                    or (bid or {}).get("project_name")
                    or ""
                ),
                "doc_type": str(manifest.get("doc_type") or ""),
                "pages": int(manifest.get("pages") or 0),
                "parser": str(manifest.get("parser") or ""),
                "parsed_at": str(manifest.get("parsed_at") or _timestamp(child)),
                "section_count": len(_list(manifest.get("sections"))),
                "toc_count": len(_list(manifest.get("toc"))),
                "warning_count": len(_list(manifest.get("warnings"))),
                "has_extraction": (child / "extraction.json").is_file(),
                "extraction_partial": bool(meta.get("partial")),
                "ungrounded_count": int(
                    meta.get("ungrounded_count") or len(ungrounded)
                ),
                "evaluation_factor_count": len(evaluation_factors),
                "has_index": (child / "index_manifest.json").is_file(),
                "bid": bid,
            }
        )
    items.sort(key=lambda item: item.get("parsed_at") or "", reverse=True)
    return items


def _coverage(matrix: list[dict[str, Any]]) -> dict[str, Any]:
    """Coverage of extracted scoring factors by outline sections.

    The pipeline never assigns earned marks to a draft; this reports which
    factors the deterministic response matrix maps to a section, not a fake
    bidder score.
    """
    total_factors = len(matrix)
    covered = [
        row
        for row in matrix
        if isinstance(row.get("section_ids"), list) and row.get("section_ids")
    ]
    total_score = sum(_number(row.get("max_score")) for row in matrix)
    covered_score = sum(_number(row.get("max_score")) for row in covered)
    categories: dict[str, dict[str, Any]] = {}
    for row in matrix:
        name = str(row.get("category") or "未分类")
        bucket = categories.setdefault(
            name,
            {"category": name, "factors": 0, "covered": 0, "max_score": 0.0},
        )
        bucket["factors"] += 1
        bucket["max_score"] += _number(row.get("max_score"))
        if isinstance(row.get("section_ids"), list) and row.get("section_ids"):
            bucket["covered"] += 1
    for bucket in categories.values():
        bucket["max_score"] = round(bucket["max_score"], 2)
    unmapped = [
        {
            "factor": str(row.get("factor") or ""),
            "category": str(row.get("category") or ""),
            "max_score": row.get("max_score"),
        }
        for row in matrix
        if not row.get("section_ids")
    ]
    return {
        "total_factors": total_factors,
        "covered_factors": len(covered),
        "factor_coverage": (
            round(len(covered) / total_factors, 4) if total_factors else None
        ),
        "max_score": round(total_score, 2),
        "covered_max_score": round(covered_score, 2),
        "score_coverage": (
            round(covered_score / total_score, 4) if total_score else None
        ),
        "by_category": list(categories.values()),
        "unmapped_factors": unmapped,
    }


def _section_report(
    matrix: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    issue_by_section: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        section_id = str(issue.get("section_id") or "")
        if section_id:
            issue_by_section.setdefault(section_id, []).append(issue)

    source = _list(_mapping(manifest.get("outline")).get("sections"))
    reports: list[dict[str, Any]] = []
    for raw in source:
        if not isinstance(raw, dict):
            continue
        section_id = str(raw.get("id") or "")
        factors = [
            row
            for row in matrix
            if section_id in _list(row.get("section_ids"))
        ]
        related = issue_by_section.get(section_id, [])
        severities = [str(item.get("severity") or "") for item in related]
        reports.append(
            {
                "id": section_id,
                "title": str(raw.get("title") or ""),
                "role": str(raw.get("role") or ""),
                "origin": str(raw.get("origin") or "base"),
                "word_target": int(raw.get("word_target") or 0),
                "factor_count": len(factors),
                "factor_score": round(
                    sum(_number(row.get("max_score")) for row in factors), 2
                ),
                "factors": [str(row.get("factor") or "") for row in factors],
                "issue_count": len(related),
                "high_issues": severities.count("high"),
                "medium_issues": severities.count("medium"),
                "low_issues": severities.count("low"),
            }
        )
    return reports


def bid_detail(settings: Settings, doc_id: str) -> dict[str, Any] | None:
    """Summary plus scoring coverage and per-section review data."""
    summary = bid_summary(settings, doc_id)
    if summary is None:
        return None
    bid_dir = _safe_bid_dir(settings, doc_id)
    manifest = _read_json(bid_dir / "bid_manifest.json") if bid_dir else None
    if manifest is None:
        return None
    review = _mapping(manifest.get("review"))
    issues = [item for item in _list(review.get("issues")) if isinstance(item, dict)]
    matrix = [
        item
        for item in _list(manifest.get("response_matrix"))
        if isinstance(item, dict)
    ]
    pending = [
        item
        for item in _list(manifest.get("pending_markers"))
        if isinstance(item, dict)
    ]
    gaps = [item for item in _list(manifest.get("profile_gaps")) if isinstance(item, dict)]
    result = dict(summary)
    result.update(
        {
            "review_summary": str(review.get("summary") or ""),
            "issues": issues,
            "unverified_claims": [
                item
                for item in _list(review.get("unverified_claims"))
                if isinstance(item, dict)
            ],
            "missing_requirements": [
                item
                for item in _list(review.get("missing_requirements"))
                if isinstance(item, dict)
            ],
            "coverage": _coverage(matrix),
            "sections": _section_report(matrix, issues, manifest),
            "response_matrix": matrix,
            "pending_markers": pending[:100],
            "pending_count": int(summary["stats"].get("pending_markers") or len(pending)),
            "profile_gaps": gaps[:100],
            "profile_gap_count": len(gaps),
            "revisions": [
                item for item in _list(manifest.get("revisions")) if isinstance(item, dict)
            ],
        }
    )
    return result
