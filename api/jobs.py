"""In-memory job table and background runner for the parse API.

This is the fallback backend (``TASK_BACKEND=memory``) and the backend used by
offline tests.  Jobs live only in this process: a restart loses every record
(and any running work), while artifacts already written under ``upload/``
remain on disk.  The table is bounded by ``API_MAX_JOBS`` and
``API_JOB_TTL_SECONDS``.  Production defaults to the Redis + Celery backend.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bidding.job_state import JobConflict
from bidding.models import now_iso

logger = logging.getLogger(__name__)

__all__ = [
    "Job",
    "JobConflict",
    "JobNotFound",
    "JobQueueFull",
    "JobRunner",
    "JobStore",
]

ACTIVE_STATUSES = {"queued", "running"}
FINISHED_STATUSES = {"succeeded", "failed"}
MAX_PROGRESS_LINES = 200
MAX_PROGRESS_CHARS = 500

Work = Callable[["Job", "JobStore"], None]


class JobNotFound(KeyError):
    """Raised when a job id is unknown (or has expired)."""


class JobQueueFull(RuntimeError):
    """Raised when the in-memory job table has no room for another job."""


@dataclass(slots=True)
class Job:
    id: str
    filename: str
    doc_id: str
    parser: str
    extract: bool
    ingest: bool
    upload_dir: Path = field(repr=False)
    source_path: Path | None = field(default=None, repr=False)
    profile_path: Path | None = field(default=None, repr=False)
    kind: str = "parse"
    options: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    stage: str = "queued"
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    progress: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-ready copy; internal paths are never exposed."""
        options = self.options or {
            "parser": self.parser,
            "extract": self.extract,
            "ingest": self.ingest,
        }
        return {
            "job_id": self.id,
            "status": self.status,
            "stage": self.stage,
            "kind": self.kind,
            "filename": self.filename,
            "doc_id": self.doc_id,
            "options": dict(options),
            "progress": list(self.progress),
            "result": dict(self.result),
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobStore:
    """Thread-safe in-memory job records."""

    def __init__(self, *, max_jobs: int = 100, ttl_seconds: int = 86400) -> None:
        self.max_jobs = max(1, max_jobs)
        self.ttl_seconds = max(0, ttl_seconds)
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()

    def create(
        self,
        *,
        filename: str,
        doc_id: str,
        kind: str = "parse",
        parser: str = "auto",
        extract: bool = False,
        ingest: bool = False,
        max_sections: int = 0,
        max_revisions: int = 2,
        upload_dir: Path,
        source_path: Path | None = None,
        profile_path: Path | None = None,
        job_id: str | None = None,
    ) -> Job:
        """Register a queued job.

        Only one queued/running job may target a doc_id, because parsing and
        bid writing share the per-document artifacts under ``upload/``.
        """
        kind = (kind or "parse").strip().lower()
        if kind not in {"parse", "bid"}:
            raise ValueError(f"不支持的任务类型：{kind}")
        if kind == "bid":
            options: dict[str, Any] = {
                "max_sections": max(0, min(int(max_sections), 3)),
                "max_revisions": max(0, int(max_revisions)),
            }
        else:
            options = {
                "parser": parser or "auto",
                "extract": bool(extract),
                "ingest": bool(ingest),
            }

        job_id = job_id or uuid.uuid4().hex
        with self._lock:
            self._prune_locked()
            if any(
                job.doc_id == doc_id and job.status in ACTIVE_STATUSES
                for job in self._jobs.values()
            ):
                raise JobConflict(f"文档 {doc_id} 已有任务在排队或执行中")
            if len(self._jobs) >= self.max_jobs:
                self._evict_finished_locked(until_below=self.max_jobs)
                if len(self._jobs) >= self.max_jobs:
                    raise JobQueueFull("任务表已满，请稍后重试")

            job = Job(
                id=job_id,
                filename=filename,
                doc_id=doc_id,
                parser=parser or "",
                extract=bool(extract),
                ingest=bool(ingest),
                upload_dir=upload_dir,
                source_path=source_path,
                profile_path=profile_path,
                kind=kind,
                options=options,
            )
            self._jobs[job_id] = job
            return job

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(job_id)
            return job.snapshot() if job is not None else None

    def mark_running(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(job_id)
            job.status = "running"
            job.stage = "write" if job.kind == "bid" else "parse"
            job.started_at = now_iso()
            return job

    def set_stage(self, job_id: str, stage: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.stage = stage

    def set_doc_id(self, job_id: str, doc_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.doc_id = doc_id

    def set_result(self, job_id: str, key: str, value: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.result[key] = value

    def add_progress(self, job_id: str, message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        if len(text) > MAX_PROGRESS_CHARS:
            text = text[:MAX_PROGRESS_CHARS] + "…"
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress.append(text)
            overflow = len(job.progress) - MAX_PROGRESS_LINES
            if overflow > 0:
                del job.progress[:overflow]

    def succeed(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "succeeded"
            job.stage = "done"
            job.finished_at = now_iso()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "failed"
            job.error = error.strip() or "任务执行失败"
            job.finished_at = now_iso()

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def _prune_locked(self) -> None:
        if not self.ttl_seconds:
            return
        now = datetime.now().astimezone()
        for job_id, job in list(self._jobs.items()):
            if job.status not in FINISHED_STATUSES:
                continue
            stamp = job.finished_at or job.created_at
            try:
                finished = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if (now - finished).total_seconds() > self.ttl_seconds:
                del self._jobs[job_id]

    def _evict_finished_locked(self, *, until_below: int) -> None:
        while len(self._jobs) >= until_below:
            victim = next(
                (
                    job_id
                    for job_id, job in self._jobs.items()
                    if job.status in FINISHED_STATUSES
                ),
                None,
            )
            if victim is None:
                return
            del self._jobs[victim]


class JobRunner:
    """Submit jobs to a thread pool and track their lifecycle."""

    def __init__(
        self,
        store: JobStore,
        work: Work,
        *,
        max_workers: int = 2,
        executor: Executor | None = None,
        works: dict[str, Work] | None = None,
    ) -> None:
        self.store = store
        self.work = work
        self.works: dict[str, Work] = dict(works or {})
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="bidding-job",
        )
        self._accepting = True
        self._lock = threading.Lock()

    def register(self, kind: str, work: Work) -> None:
        """Register an additional job kind (e.g. ``bid``)."""
        self.works[kind] = work

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        job = self.store.create(**kwargs)
        snapshot = self.store.snapshot(job.id) or job.snapshot()
        try:
            with self._lock:
                if not self._accepting:
                    raise RuntimeError("API 正在关闭，无法接受新任务")
                self._executor.submit(self._run, job.id)
        except Exception:
            self.store.remove(job.id)
            raise
        return snapshot

    def _run(self, job_id: str) -> None:
        try:
            job = self.store.mark_running(job_id)
        except JobNotFound:
            return
        work = self.works.get(job.kind)
        if work is None:
            if job.kind != "parse":
                self.store.fail(job_id, f"不支持的任务类型：{job.kind}")
                return
            work = self.work
        try:
            work(job, self.store)
        except Exception as exc:  # noqa: BLE001 - the job must capture all failures
            logger.exception("解析任务 %s 失败", job_id)
            self.store.fail(job_id, str(exc) or exc.__class__.__name__)
        else:
            self.store.succeed(job_id)

    def shutdown(self) -> None:
        with self._lock:
            self._accepting = False
        self._executor.shutdown(wait=False, cancel_futures=True)
