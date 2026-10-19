"""Celery-backed job runner used by the HTTP API by default."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from bidding.job_state import JobBackendError, RedisJobStore
from bidding.tasks import parse_pipeline_task, write_bid_task

logger = logging.getLogger(__name__)


class _RunnerClosed(RuntimeError):
    """The API is shutting down and no longer accepts new jobs."""


class CeleryJobRunner:
    """Create a Redis job record, then enqueue the pipeline on Celery.

    The Redis record is the single source of truth for polling; Celery only
    carries the small task payload and result.  The API and worker must share
    the local filesystem because the task reads the uploaded PDF and writes
    artifacts under ``upload/``.
    """

    def __init__(
        self,
        store: RedisJobStore,
        *,
        task: Any | None = None,
        tasks: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self._tasks: dict[str, Any] = {"parse": task or parse_pipeline_task}
        if tasks:
            self._tasks.update(tasks)
        self._tasks.setdefault("bid", write_bid_task)
        self._accepting = True
        self._lock = threading.Lock()

    def register(self, kind: str, task: Any) -> None:
        """Register an additional Celery task kind (e.g. ``bid``)."""
        self._tasks[kind] = task

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        values = dict(kwargs)
        kind = str(values.pop("kind", "parse") or "parse").strip().lower()
        task = self._tasks.get(kind)
        if task is None:
            raise RuntimeError(f"不支持的任务类型：{kind}")
        with self._lock:
            if not self._accepting:
                raise _RunnerClosed("API 正在关闭，无法接受新任务")

        snapshot = self.store.create(kind=kind, **values)
        job_id = snapshot["job_id"]
        upload_dir: Path = values["upload_dir"]
        if kind == "bid":
            profile_path: Path = values["profile_path"]
            payload = {
                "job_id": job_id,
                "doc_id": snapshot["doc_id"],
                "profile_path": str(profile_path.resolve()),
                "max_sections": snapshot["options"]["max_sections"],
                "max_revisions": snapshot["options"]["max_revisions"],
                "upload_dir": str(upload_dir.resolve()),
            }
        else:
            source_path: Path = values["source_path"]
            payload = {
                "job_id": job_id,
                "filename": snapshot["filename"],
                "doc_id": snapshot["doc_id"],
                "parser": snapshot["options"]["parser"],
                "extract": snapshot["options"]["extract"],
                "ingest": snapshot["options"]["ingest"],
                "source_path": str(source_path.resolve()),
                "upload_dir": str(upload_dir.resolve()),
            }

        try:
            with self._lock:
                if not self._accepting:
                    raise _RunnerClosed("API 正在关闭，无法接受新任务")
                task.apply_async(kwargs={"payload": payload}, task_id=job_id)
        except _RunnerClosed:
            self._discard(job_id)
            raise
        except Exception as exc:  # noqa: BLE001 - broker errors vary by transport
            self._discard(job_id)
            logger.exception("提交 Celery 任务 %s 失败", job_id)
            raise JobBackendError(
                "无法提交后台任务，请确认 Redis 与 Celery worker 已启动"
            ) from exc
        return snapshot

    def _discard(self, job_id: str) -> None:
        try:
            self.store.remove(job_id)
        except Exception:  # noqa: BLE001 - keep the original broker error
            logger.exception("清理未提交的任务 %s 失败", job_id)

    def shutdown(self) -> None:
        with self._lock:
            self._accepting = False
