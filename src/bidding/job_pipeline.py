"""Shared parse → extract → ingest pipeline used by both job backends.

The API's in-memory thread runner and the Celery worker execute the same
function, so the two paths cannot drift apart.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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
