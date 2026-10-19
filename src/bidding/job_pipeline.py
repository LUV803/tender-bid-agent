"""Shared parse → extract → ingest pipeline used by both job backends.

The API's in-memory thread runner and the Celery worker execute the same
function, so the two paths cannot drift apart.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .extraction import run_extract
from .indexing import run_ingest
from .parsing import parse_document


class JobLike(Protocol):
    """Fields a job must expose while the pipeline runs."""

    id: str
    filename: str
    doc_id: str
    parser: str
    extract: bool
    ingest: bool
    upload_dir: Path
    source_path: Path


class JobStateStore(Protocol):
    """State operations the pipeline needs; both backends implement them."""

    def set_stage(self, job_id: str, stage: str) -> None: ...

    def set_doc_id(self, job_id: str, doc_id: str) -> None: ...

    def set_result(self, job_id: str, key: str, value: object) -> None: ...

    def add_progress(self, job_id: str, message: str) -> None: ...


@dataclass(slots=True)
class PipelineJob:
    """Minimal job payload reconstructed by a Celery worker."""

    id: str
    filename: str
    doc_id: str
    parser: str
    extract: bool
    ingest: bool
    upload_dir: Path = field(repr=False)
    source_path: Path = field(repr=False)


@dataclass(slots=True)
class BidPipelineJob:
    """Minimal bid-writing payload reconstructed by a Celery worker."""

    id: str
    doc_id: str
    profile_path: Path = field(repr=False)
    upload_dir: Path = field(repr=False)
    max_sections: int = 0
    max_revisions: int = 2


def execute_pipeline(job: JobLike, store: JobStateStore) -> None:
    """Parse the uploaded PDF and optionally extract and index it."""

    def progress(message: str) -> None:
        store.add_progress(job.id, message)

    try:
        store.set_stage(job.id, "parse")
        progress(f"开始解析：{job.filename}")
        manifest = parse_document(job.source_path, parser=job.parser)
        store.set_result(job.id, "parse", manifest)
        doc_id = str(manifest.get("doc_id") or job.doc_id)
        if doc_id != job.doc_id:
            store.set_doc_id(job.id, doc_id)
        progress(f"解析完成：{doc_id}（{manifest.get('pages', 0)} 页）")

        if job.extract:
            store.set_stage(job.id, "extract")
            progress("开始结构化抽取")
            extraction = run_extract(doc_id, progress=progress)
            store.set_result(job.id, "extract", extraction)

        if job.ingest:
            store.set_stage(job.id, "ingest")
            progress("开始分块、嵌入并写入 Chroma")
            index = run_ingest(doc_id, progress=progress)
            store.set_result(job.id, "ingest", index)
    finally:
        shutil.rmtree(job.upload_dir, ignore_errors=True)


def bid_result_summary(result: Any) -> dict[str, Any]:
    """Small, JSON-ready summary stored in the Redis job record.

    The full manifest stays on disk in ``upload/bids/<doc_id>/bid_manifest.json``;
    the polling payload only carries what a frontend needs to show progress and
    link to the artifacts.
    """
    manifest = getattr(result, "manifest", None) or {}
    project = manifest.get("project") or {}
    bidder = manifest.get("bidder") or {}
    return {
        "doc_id": getattr(result, "doc_id", ""),
        "project_name": project.get("project_name", ""),
        "bidder": bidder.get("company_name", ""),
        "stats": manifest.get("stats") or {},
        "artifacts": manifest.get("artifacts") or {},
        "warnings": list(manifest.get("warnings") or [])[:20],
    }


def execute_bid_job(job: BidPipelineJob, store: JobStateStore) -> None:
    """Run the bid-writing pipeline and publish its summary.

    Imported lazily so parse-only workers do not load the LLM clients.
    """
    from .writing import write_bid

    def progress(message: str) -> None:
        store.add_progress(job.id, message)

    try:
        store.set_stage(job.id, "write")
        progress("开始编写投标文件")
        result = write_bid(
            job.doc_id,
            profile_path=job.profile_path,
            max_revisions=job.max_revisions,
            max_sections=job.max_sections,
            progress=progress,
        )
        store.set_result(job.id, "bid", bid_result_summary(result))
    finally:
        shutil.rmtree(job.upload_dir, ignore_errors=True)
