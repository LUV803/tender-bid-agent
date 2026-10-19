"""Celery tasks for asynchronous tender-document processing."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from .celery_app import celery_app
from .job_pipeline import PipelineJob, execute_pipeline
from .job_state import RedisJobStore

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 2000


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:MAX_ERROR_CHARS]


@celery_app.task(name="bidding.ping")
def ping() -> str:
    """Cheap connectivity probe for ``celery call bidding.ping``."""
    return "pong"


@celery_app.task(name="bidding.parse_pipeline")
def parse_pipeline_task(payload: dict[str, Any]) -> dict[str, str]:
    """Run the shared pipeline and record its lifecycle in Redis.

    The task deliberately returns only a tiny summary.  Parse manifests,
    extraction results and index manifests are stored in the Redis job hash by
    ``execute_pipeline``, so they never travel through the Celery result
    backend.
    """
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        raise ValueError("Celery 任务缺少 job_id")

    job = PipelineJob(
        id=job_id,
        filename=str(payload.get("filename") or ""),
        doc_id=str(payload["doc_id"]),
        parser=str(payload.get("parser") or "auto"),
        extract=bool(payload.get("extract")),
        ingest=bool(payload.get("ingest")),
        upload_dir=Path(payload["upload_dir"]),
        source_path=Path(payload["source_path"]),
    )
    store = RedisJobStore()
    status = "failed"
    try:
        store.mark_running(job.id)
        if not job.source_path.is_file():
            raise FileNotFoundError("上传文件不存在或已被清理")
        execute_pipeline(job, store)
    except Exception as exc:  # noqa: BLE001 - every failure belongs to the job
        logger.exception("解析任务 %s 失败", job.id)
        try:
            store.fail(job.id, _error_text(exc))
        except Exception:  # noqa: BLE001 - Redis may itself be unavailable
            logger.exception("记录任务 %s 失败状态时出错", job.id)
    else:
        try:
            store.succeed(job.id)
            status = "succeeded"
        except Exception:  # noqa: BLE001
            logger.exception("记录任务 %s 成功状态时出错", job.id)
    finally:
        # execute_pipeline cleans up too; this also covers failures before it
        # starts (Redis unavailable, missing upload) and is idempotent.
        shutil.rmtree(job.upload_dir, ignore_errors=True)
        try:
            store.release_doc_lock(job.id, job.doc_id)
        except Exception:  # noqa: BLE001 - lock also expires with its own TTL
            logger.exception("释放文档 %s 的任务锁时出错", job.doc_id)
        store.close()

    return {"job_id": job.id, "status": status}
