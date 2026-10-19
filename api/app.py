"""FastAPI application for asynchronous tender-document parsing jobs."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_serializer

from bidding import companies, storage
from bidding.companies import CompanyStoreError
from bidding.config import Settings, get_settings
from bidding.job_pipeline import execute_bid_job, execute_pipeline
from bidding.job_state import JobBackendError, JobConflict, RedisJobStore
from bidding.parsing.structure import sanitize_doc_id, sanitize_filename
from bidding.writing.profile import MAX_PROFILE_BYTES

from .celery_runner import CeleryJobRunner
from .jobs import JobQueueFull, JobRunner, JobStore
from .library import (
    ARTIFACT_NAMES,
    artifact_path,
    bid_detail,
    list_bid_history,
    list_documents,
    media_type_for,
)

ParserName = Literal["auto", "cloud", "local"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobStage = Literal["queued", "parse", "extract", "ingest", "write", "done"]
UPLOAD_CHUNK_SIZE = 1024 * 1024
PDF_HEADER_WINDOW = 1024
MAX_BID_SECTIONS = 3


class JobOptions(BaseModel):
    parser: ParserName | None = None
    extract: bool | None = None
    ingest: bool | None = None
    max_sections: int | None = None
    max_revisions: int | None = None

    @model_serializer(mode="wrap")
    def _omit_none(self, handler):
        """Keep parse and bid options disjoint in the polling response."""
        return {key: value for key, value in handler(self).items() if value is not None}


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
    kind: Literal["parse", "bid"] = "parse"
    filename: str
    doc_id: str
    options: JobOptions
    progress: list[str]
    result: dict[str, Any]
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


def _validated_profile(value: dict[str, Any]) -> dict[str, Any]:
    try:
        size = len(
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"profile 必须是可序列化的 JSON 对象：{exc}") from exc
    if size > MAX_PROFILE_BYTES:
        limit_mb = MAX_PROFILE_BYTES // (1024 * 1024)
        raise ValueError(f"profile 超过大小上限 {limit_mb} MB")
    return value


class BidJobRequest(BaseModel):
    """JSON body for ``POST /api/bids``.

    ``profile`` is the same object accepted by ``bidder_profile.json``.  Missing
    fields become 【待填写】 placeholders; an empty object is valid and produces
    a fully-placeholder draft.  When ``company_id`` is set, the persisted
    company profile is loaded first and ``profile`` is deep-merged on top of it.
    """

    doc_id: str = Field(min_length=1, max_length=64)
    profile: dict[str, Any] = Field(default_factory=dict)
    company_id: str | None = Field(
        default=None,
        max_length=32,
        description="可选：从 PostgreSQL 公司资料库载入资料，profile 中的字段覆盖同名字段",
    )
    max_sections: int = Field(
        default=0,
        ge=0,
        le=MAX_BID_SECTIONS,
        description="分析师新增章节上限；固定 11 章始终保留，0 表示不新增",
    )
    max_revisions: int = Field(
        default=2,
        ge=0,
        le=10,
        description="高优先级问题自动修订的章节数上限",
    )

    @field_validator("doc_id")
    @classmethod
    def _validate_doc_id(cls, value: str) -> str:
        cleaned = sanitize_doc_id(value)
        if not cleaned or cleaned != value:
            raise ValueError("doc_id 必须是已解析文档的安全目录名（不含空格或路径分隔符）")
        return cleaned

    @field_validator("company_id")
    @classmethod
    def _validate_company_id(cls, value: str | None) -> str | None:
        if value is not None and not companies.valid_company_id(value):
            raise ValueError("company_id 必须是 32 位十六进制公司编号")
        return value

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validated_profile(value)


class CompanyWriteRequest(BaseModel):
    """JSON body for creating or updating a company profile."""

    profile: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validated_profile(value)


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
            works={"bid": execute_bid_job},
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
    if isinstance(runner, JobRunner) and "bid" not in runner.works:
        runner.register("bid", execute_bid_job)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        runner.shutdown()

    application = FastAPI(
        title="Bidding API",
        version="0.1.0",
        description="招标文件解析、结构化抽取、向量入库与多 Agent 标书编写的异步任务接口。",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        # Vite development server and the Electron file:// renderer (Origin: null).
        allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|null)$",
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )
    application.state.settings = settings
    application.state.runner = runner

    @application.get("/api/health", summary="服务健康检查")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "bidding-api",
            "version": application.version,
            "backend": settings.task_backend,
            "storage": "minio" if storage.active(settings) else "local",
            "database": "postgresql" if settings.database_url else "off",
        }

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

    @application.post(
        "/api/bids",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=JobAccepted,
        summary="使用项目资料异步编写投标文件",
        responses={
            404: {"description": "doc_id 尚未解析或公司资料不存在"},
            409: {"description": "文档尚未完成抽取，或已有任务在执行"},
            422: {"description": "doc_id、profile、company_id 或参数不合法"},
            503: {"description": "任务表已满、公司资料库不可用或服务正在关闭"},
        },
    )
    async def start_bid(request: BidJobRequest, response: Response) -> JobAccepted:
        parsed_dir = settings.parsed_dir / request.doc_id
        if not (parsed_dir / "manifest.json").is_file():
            # Restore from MinIO when the local working cache was cleared or
            # the document was generated on another machine.
            storage.hydrate_document(request.doc_id, settings)
        if not (parsed_dir / "manifest.json").is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文档尚未解析或不存在，请先调用 POST /api/parse",
            )
        if not (parsed_dir / "extraction.json").is_file():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="文档尚未完成结构化抽取，无法编写投标文件",
            )

        profile = request.profile
        if request.company_id:
            try:
                record = await companies.get_company(settings, request.company_id)
            except CompanyStoreError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from exc
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="公司资料不存在或已删除，请刷新公司资料库",
                )
            profile = companies.merge_profiles(record["profile"], request.profile)

        job_id = uuid.uuid4().hex
        upload_dir = settings.upload_dir / "tmp" / job_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        profile_path = upload_dir / "bidder_profile.json"
        try:
            profile_path.write_text(
                json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            try:
                snapshot = runner.submit(
                    job_id=job_id,
                    filename=f"{request.doc_id} 投标文件",
                    doc_id=request.doc_id,
                    kind="bid",
                    profile_path=profile_path,
                    max_sections=request.max_sections,
                    max_revisions=request.max_revisions,
                    upload_dir=upload_dir,
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

    def _company_id_or_422(company_id: str) -> str:
        if not companies.valid_company_id(company_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="company_id 必须是 32 位十六进制公司编号",
            )
        return company_id

    @application.get(
        "/api/companies",
        response_model=list[dict[str, Any]],
        summary="列出 PostgreSQL 中的公司资料",
        responses={503: {"description": "公司资料库未配置或不可用"}},
    )
    async def company_list() -> list[dict[str, Any]]:
        try:
            return await companies.list_companies(settings)
        except CompanyStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

    @application.post(
        "/api/companies",
        status_code=status.HTTP_201_CREATED,
        response_model=dict[str, Any],
        summary="把投标人资料保存到 PostgreSQL",
        responses={422: {"description": "profile 不是合法 JSON 对象或超过 2 MB"}},
    )
    async def company_create(request: CompanyWriteRequest) -> dict[str, Any]:
        try:
            return await companies.create_company(settings, request.profile)
        except CompanyStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

    @application.get(
        "/api/companies/{company_id}",
        response_model=dict[str, Any],
        summary="读取单个公司资料",
        responses={404: {"description": "公司资料不存在"}},
    )
    async def company_detail(company_id: str) -> dict[str, Any]:
        _company_id_or_422(company_id)
        try:
            record = await companies.get_company(settings, company_id)
        except CompanyStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="公司资料不存在"
            )
        return record

    @application.put(
        "/api/companies/{company_id}",
        response_model=dict[str, Any],
        summary="更新公司资料",
        responses={404: {"description": "公司资料不存在"}},
    )
    async def company_update(
        company_id: str, request: CompanyWriteRequest
    ) -> dict[str, Any]:
        _company_id_or_422(company_id)
        try:
            record = await companies.update_company(
                settings, company_id, request.profile
            )
        except CompanyStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="公司资料不存在"
            )
        return record

    @application.delete(
        "/api/companies/{company_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="删除公司资料",
        responses={404: {"description": "公司资料不存在"}},
    )
    async def company_delete(company_id: str) -> Response:
        _company_id_or_422(company_id)
        try:
            deleted = await companies.delete_company(settings, company_id)
        except CompanyStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="公司资料不存在"
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get(
        "/api/documents",
        response_model=list[dict[str, Any]],
        summary="列出已解析文档",
    )
    def documents() -> list[dict[str, Any]]:
        return list_documents(settings)

    @application.get(
        "/api/bids",
        response_model=list[dict[str, Any]],
        summary="列出历史投标产物",
    )
    def bid_history() -> list[dict[str, Any]]:
        return list_bid_history(settings)

    @application.get(
        "/api/bids/{doc_id}",
        response_model=dict[str, Any],
        summary="读取投标产物详情与评分覆盖报告",
        responses={404: {"description": "投标产物不存在"}},
    )
    def bid_report(doc_id: str) -> dict[str, Any]:
        detail = bid_detail(settings, doc_id)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="投标产物不存在或清单已损坏",
            )
        return detail

    @application.get(
        "/api/bids/{doc_id}/files/{filename}",
        summary="预览或下载投标产物文件",
        responses={404: {"description": "文件不存在或不在允许列表中"}},
    )
    def bid_file(
        doc_id: str,
        filename: str,
        download: Annotated[
            bool, Query(description="true 时以附件下载，false 时内联预览")
        ] = False,
    ) -> FileResponse:
        path = artifact_path(settings, doc_id, filename)
        if path is None and filename in ARTIFACT_NAMES:
            # Cache the remote object locally so desktop "open folder" and
            # subsequent previews work without another download.
            storage.hydrate_bid_file(doc_id, filename, settings)
            path = artifact_path(settings, doc_id, filename)
        if path is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="投标产物文件不存在",
            )
        return FileResponse(
            path,
            media_type=media_type_for(filename),
            filename=filename if download else None,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
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
