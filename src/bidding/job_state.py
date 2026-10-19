"""Redis-backed job state shared by the API and Celery workers.

Job records are ordinary Redis hashes, progress is a capped Redis list, and a
per-document lock prevents the API from running two writers for one
``upload/parsed/<doc_id>`` directory.  Keeping this state outside the API
process is what lets ``POST /api/parse`` return before a worker finishes.
"""

from __future__ import annotations

import functools
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

import redis
from redis.exceptions import RedisError

from .config import Settings, get_settings
from .models import now_iso

logger = logging.getLogger(__name__)

JOB_PREFIX = "bidding:job:"
ACTIVE_DOC_PREFIX = "bidding:active-doc:"
MAX_PROGRESS_LINES = 200
MAX_PROGRESS_CHARS = 500


class JobConflict(RuntimeError):
    """Raised when another active job is already using the same doc_id."""


class JobBackendError(RuntimeError):
    """Raised when the shared job state cannot be reached or updated."""


def _guard(method: Callable[..., Any]) -> Callable[..., Any]:
    """Translate Redis transport failures into a transport-neutral error."""

    @functools.wraps(method)
    def wrapper(self: "RedisJobStore", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except JobConflict:
            raise
        except RedisError as exc:
            logger.exception("Redis 任务状态操作失败")
            raise JobBackendError("任务状态存储暂不可用，请确认 Redis 已启动") from exc

    return wrapper


class RedisJobStore:
    """Job table stored in Redis so the API and worker see the same state."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: redis.Redis | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ttl_seconds = max(0, self.settings.api_job_ttl_seconds)
        self._client = client or redis.Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=30,
        )

    def _job_key(self, job_id: str) -> str:
        return f"{JOB_PREFIX}{job_id}"

    def _progress_key(self, job_id: str) -> str:
        return f"{JOB_PREFIX}{job_id}:progress"

    def _lock_key(self, doc_id: str) -> str:
        return f"{ACTIVE_DOC_PREFIX}{doc_id}"

    def _touch(self, job_id: str) -> None:
        if self.ttl_seconds <= 0:
            return
        self._client.expire(self._job_key(job_id), self.ttl_seconds)
        self._client.expire(self._progress_key(job_id), self.ttl_seconds)

    @staticmethod
    def _snapshot(data: dict[str, str], progress: list[str]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for name in ("parse", "extract", "ingest"):
            raw = data.get(f"result_{name}")
            if not raw:
                continue
            try:
                results[name] = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("任务结果 %s 不是有效 JSON，已忽略", name)
        return {
            "job_id": data.get("job_id", ""),
            "status": data.get("status", "queued"),
            "stage": data.get("stage", "queued"),
            "filename": data.get("filename", ""),
            "doc_id": data.get("doc_id", ""),
            "options": {
                "parser": data.get("parser", "auto"),
                "extract": data.get("extract") == "1",
                "ingest": data.get("ingest") == "1",
            },
            "progress": progress,
            "result": results,
            "error": data.get("error") or None,
            "created_at": data.get("created_at", ""),
            "started_at": data.get("started_at") or None,
            "finished_at": data.get("finished_at") or None,
        }

    @_guard
    def create(
        self,
        *,
        filename: str,
        doc_id: str,
        parser: str,
        extract: bool,
        ingest: bool,
        upload_dir: Path,
        source_path: Path,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Register a queued job and take the per-document write lock."""
        job_id = job_id or uuid.uuid4().hex
        lock_ttl = max(self.ttl_seconds, 3600)
        acquired = self._client.set(
            self._lock_key(doc_id),
            job_id,
            nx=True,
            ex=lock_ttl,
        )
        if not acquired:
            raise JobConflict(f"文档 {doc_id} 已有任务在排队或执行中")

        data = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "filename": filename,
            "doc_id": doc_id,
            "parser": parser,
            "extract": "1" if extract else "0",
            "ingest": "1" if ingest else "0",
            "created_at": now_iso(),
            "started_at": "",
            "finished_at": "",
            "error": "",
        }
        try:
            self._client.hset(self._job_key(job_id), mapping=data)
            self._touch(job_id)
        except Exception:
            self._client.delete(self._job_key(job_id), self._progress_key(job_id))
            self.release_doc_lock(job_id, doc_id)
            raise
        return self._snapshot(data, [])

    @_guard
    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        data = self._client.hgetall(self._job_key(job_id))
        if not data:
            return None
        progress = self._client.lrange(self._progress_key(job_id), 0, -1)
        return self._snapshot(data, [str(item) for item in progress])

    @_guard
    def mark_running(self, job_id: str) -> None:
        self._client.hset(
            self._job_key(job_id),
            mapping={
                "status": "running",
                "stage": "parse",
                "started_at": now_iso(),
            },
        )
        self._touch(job_id)

    @_guard
    def set_stage(self, job_id: str, stage: str) -> None:
        self._client.hset(self._job_key(job_id), "stage", stage)
        self._touch(job_id)

    @_guard
    def set_doc_id(self, job_id: str, doc_id: str) -> None:
        self._client.hset(self._job_key(job_id), "doc_id", doc_id)
        self._touch(job_id)

    @_guard
    def set_result(self, job_id: str, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        self._client.hset(self._job_key(job_id), f"result_{key}", payload)
        self._touch(job_id)

    @_guard
    def add_progress(self, job_id: str, message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        if len(text) > MAX_PROGRESS_CHARS:
            text = text[:MAX_PROGRESS_CHARS] + "…"
        key = self._progress_key(job_id)
        self._client.rpush(key, text)
        self._client.ltrim(key, -MAX_PROGRESS_LINES, -1)
        self._touch(job_id)

    @_guard
    def succeed(self, job_id: str) -> None:
        self._client.hset(
            self._job_key(job_id),
            mapping={
                "status": "succeeded",
                "stage": "done",
                "finished_at": now_iso(),
            },
        )
        self._touch(job_id)

    @_guard
    def fail(self, job_id: str, error: str) -> None:
        self._client.hset(
            self._job_key(job_id),
            mapping={
                "status": "failed",
                "error": (error or "").strip() or "任务执行失败",
                "finished_at": now_iso(),
            },
        )
        self._touch(job_id)

    @_guard
    def remove(self, job_id: str) -> None:
        key = self._job_key(job_id)
        doc_id = self._client.hget(key, "doc_id")
        self._client.delete(key, self._progress_key(job_id))
        if doc_id:
            self.release_doc_lock(job_id, doc_id)

    @_guard
    def release_doc_lock(self, job_id: str, doc_id: str) -> None:
        key = self._lock_key(doc_id)
        if self._client.get(key) == job_id:
            self._client.delete(key)

    def close(self) -> None:
        try:
            self._client.close()
        except RedisError:
            logger.warning("关闭 Redis 任务状态连接失败", exc_info=True)
