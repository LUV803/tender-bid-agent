"""FastAPI application for asynchronous tender-document parsing jobs."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel

from bidding.config import Settings, get_settings
from bidding.job_pipeline import execute_pipeline
from bidding.job_state import JobBackendError, JobConflict, RedisJobStore
from bidding.parsing.structure import sanitize_doc_id, sanitize_filename

from .celery_runner import CeleryJobRunner
from .jobs import JobQueueFull, JobRunner, JobStore

ParserName = Literal["auto", "cloud", "local"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobStage = Literal["queued", "parse", "extract", "ingest", "done"]
UPLOAD_CHUNK_SIZE = 1024 * 1024
PDF_HEADER_WINDOW = 1024


class JobOptions(BaseModel):
    parser: ParserName
    extract: bool
    ingest: bool


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus
    stage: JobStage
    doc_id: str
    status_url: str


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    stage: JobStage
    filename: str
    doc_id: str
    options: JobOptions
    progress: list[str]
    result: dict[str, Any]
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class UploadRejected(ValueError):
    status_code = status.HTTP_400_BAD_REQUEST


class UploadTooLarge(UploadRejected):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE


def _display_name(filename: str | None) -> str:
    """Strip client-side path components without trusting the name."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or "upload.pdf"


async def _save_upload(
    upload: UploadFile, destination: Path, *, max_bytes: int
) -> str:
    """Stream an upload to disk and return the SHA-256 hex digest."""
    digest = hashlib.sha256()
    size = 0
    header = b""
    try:
        with destination.open("xb") as output:
            while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                if not header:
                    header = chunk[:PDF_HEADER_WINDOW]
                size += len(chunk)
                if size > max_bytes:
                    limit_mb = max_bytes // (1024 * 1024)
                    raise UploadTooLarge(f"PDF 超过大小上限 {limit_mb} MB")
                digest.update(chunk)
                output.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if size == 0:
        destination.unlink(missing_ok=True)
        raise UploadRejected("上传文件为空")
    if b"%PDF-" not in header:
        destination.unlink(missing_ok=True)
        raise UploadRejected("上传文件不是有效的 PDF（缺少 %PDF- 文件头）")
    return digest.hexdigest()


def _default_runner(settings: Settings) -> JobRunner | CeleryJobRunner:
    """Build the configured job backend without touching Redis yet."""
    backend = (settings.task_backend or "celery").strip().lower()
    if backend == "celery":
        return CeleryJobRunner(RedisJobStore(settings))
    if backend == "memory":
        return JobRunner(
            JobStore(
                max_jobs=settings.api_max_jobs,
                ttl_seconds=settings.api_job_ttl_seconds,
            ),
            execute_pipeline,
            max_workers=settings.api_job_workers,
        )
    raise RuntimeError(f"未知 TASK_BACKEND：{backend}（可选 celery 或 memory）")


def create_app(
    *,
    settings: Settings | None = None,
    runner: JobRunner | CeleryJobRunner | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if runner is None:
        runner = _default_runner(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        runner.shutdown()

    application = FastAPI(
        title="Bidding API",
        version="0.1.0",
        description="招标文件解析、结构化抽取与向量入库的异步任务接口。",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.runner = runner

    @application.post(
        "/api/parse",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=JobAccepted,
        summary="上传 PDF 并创建解析任务",
        responses={
            400: {"description": "文件为空、非 PDF"},
            409: {"description": "同一文档已有任务在执行"},
            413: {"description": "文件超过大小上限"},
            503: {"description": "任务表已满或服务正在关闭"},
        },
    )
    async def start_parse(
        file: Annotated[UploadFile, File(description="待解析的 PDF 文件")],
        response: Response,
        extract: Annotated[bool, Query(description="解析后继续结构化抽取")] = False,
        ingest: Annotated[
            bool, Query(description="解析后继续分块、嵌入并写入 Chroma")
        ] = False,
        parser: Annotated[
            ParserName,
            Query(description="auto 优先 MinerU 并回退 PyMuPDF；cloud/local 强制单一解析器"),
        ] = "auto",
    ) -> JobAccepted:
        filename = _display_name(file.filename)
        if not filename.lower().endswith(".pdf"):
            await file.close()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="仅支持 .pdf 文件",
            )

        job_id = uuid.uuid4().hex
        upload_dir = settings.upload_dir / "tmp" / job_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = sanitize_filename(Path(filename).stem, fallback="upload")
        source_path = upload_dir / f"{safe_stem}.pdf"
        try:
            digest = await _save_upload(
                file,
                source_path,
                max_bytes=settings.api_max_upload_mb * 1024 * 1024,
            )
            doc_id = sanitize_doc_id(safe_stem) or digest[:12]

            try:
                snapshot = runner.submit(
                    job_id=job_id,
                    filename=filename,
                    doc_id=doc_id,
                    parser=parser,
                    extract=extract,
                    ingest=ingest,
                    upload_dir=upload_dir,
                    source_path=source_path,
                )
            except JobConflict as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            except (JobQueueFull, JobBackendError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from exc
            except RuntimeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from exc
        except UploadRejected as exc:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except HTTPException:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise

        location = f"/api/jobs/{snapshot['job_id']}"
        response.headers["Location"] = location
        return JobAccepted(
            job_id=snapshot["job_id"],
            status=snapshot["status"],
            stage=snapshot["stage"],
            doc_id=snapshot["doc_id"],
            status_url=location,
        )

    @application.get(
        "/api/jobs/{job_id}",
        response_model=JobResponse,
        summary="查询任务状态与结果",
        responses={
            404: {"description": "任务不存在或已过期"},
            503: {"description": "任务状态存储暂不可用"},
        },
    )
    def get_job(job_id: str) -> JobResponse:
        try:
            snapshot = runner.store.snapshot(job_id)
        except JobBackendError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="任务不存在或已过期",
            )
        return JobResponse(**snapshot)

    return application


app = create_app()
