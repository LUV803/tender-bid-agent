"""Offline tests for Celery wiring, task lifecycle and backend selection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.celery_runner import CeleryJobRunner
from api.jobs import JobRunner
from bidding.job_pipeline import PipelineJob
from bidding.job_state import (
    ACTIVE_DOC_PREFIX,
    JobBackendError,
    RedisJobStore,
)
from bidding.tasks import parse_pipeline_task, ping
from support import FakeRedis, make_settings

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def _kwargs(tmp_path: Path, doc_id: str = "doc-a", job_id: str = "job-1") -> dict:
    upload_dir = tmp_path / "upload" / "tmp" / job_id
    return {
        "job_id": job_id,
        "filename": f"{doc_id}.pdf",
        "doc_id": doc_id,
        "parser": "auto",
        "extract": False,
        "ingest": True,
        "upload_dir": upload_dir,
        "source_path": upload_dir / f"{doc_id}.pdf",
    }


def _payload(tmp_path: Path, job_id: str = "job-1") -> dict[str, Any]:
    upload_dir = tmp_path / "upload" / "tmp" / job_id
    upload_dir.mkdir(parents=True)
    source = upload_dir / "doc-a.pdf"
    source.write_bytes(PDF_BYTES)
    return {
        "job_id": job_id,
        "filename": "doc-a.pdf",
        "doc_id": "doc-a",
        "parser": "local",
        "extract": True,
        "ingest": False,
        "source_path": str(source),
        "upload_dir": str(upload_dir),
    }


class FakeTask:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def apply_async(self, *, kwargs: dict[str, Any], task_id: str | None = None):
        if self.error is not None:
            raise self.error
        self.calls.append({"kwargs": kwargs, "task_id": task_id})
        return None


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False

    def mark_running(self, job_id: str) -> None:
        self.calls.append(("mark_running", job_id))

    def fail(self, job_id: str, error: str) -> None:
        self.calls.append(("fail", job_id, error))

    def succeed(self, job_id: str) -> None:
        self.calls.append(("succeed", job_id))

    def release_doc_lock(self, job_id: str, doc_id: str) -> None:
        self.calls.append(("release_doc_lock", job_id, doc_id))

    def close(self) -> None:
        self.closed = True


def test_runner_enqueues_payload_and_stops_accepting(tmp_path: Path) -> None:
    redis_client = FakeRedis()
    store = RedisJobStore(make_settings(tmp_path), client=redis_client)
    task = FakeTask()
    runner = CeleryJobRunner(store, task=task)

    snapshot = runner.submit(**_kwargs(tmp_path))
    assert snapshot["job_id"] == "job-1"
    assert len(task.calls) == 1
    call = task.calls[0]
    assert call["task_id"] == "job-1"
    payload = call["kwargs"]["payload"]
    assert payload["job_id"] == "job-1"
    assert payload["doc_id"] == "doc-a"
    assert payload["source_path"].endswith("doc-a.pdf")
    assert Path(payload["source_path"]).is_absolute()
    assert Path(payload["upload_dir"]).is_absolute()
    assert redis_client.strings[f"{ACTIVE_DOC_PREFIX}doc-a"] == "job-1"

    runner.shutdown()
    with pytest.raises(RuntimeError, match="正在关闭"):
        runner.submit(**_kwargs(tmp_path, "doc-b", "job-2"))
    assert len(task.calls) == 1
    assert f"{ACTIVE_DOC_PREFIX}doc-b" not in redis_client.strings


def test_broker_failure_removes_record_and_lock(tmp_path: Path) -> None:
    redis_client = FakeRedis()
    store = RedisJobStore(make_settings(tmp_path), client=redis_client)
    runner = CeleryJobRunner(store, task=FakeTask(RuntimeError("broker refused")))

    with pytest.raises(JobBackendError) as caught:
        runner.submit(**_kwargs(tmp_path))
    assert "broker refused" not in str(caught.value)
    assert store.snapshot("job-1") is None
    assert f"{ACTIVE_DOC_PREFIX}doc-a" not in redis_client.strings


def test_task_success_records_state_and_releases_lock(
    tmp_path: Path, monkeypatch
) -> None:
    store = RecordingStore()
    executed: list[PipelineJob] = []

    def fake_execute(job: PipelineJob, state: RecordingStore) -> None:
        executed.append(job)

    monkeypatch.setattr("bidding.tasks.RedisJobStore", lambda: store)
    monkeypatch.setattr("bidding.tasks.execute_pipeline", fake_execute)

    result = parse_pipeline_task.run(_payload(tmp_path))

    assert result == {"job_id": "job-1", "status": "succeeded"}
    assert executed == [
        PipelineJob(
            id="job-1",
            filename="doc-a.pdf",
            doc_id="doc-a",
            parser="local",
            extract=True,
            ingest=False,
            upload_dir=tmp_path / "upload" / "tmp" / "job-1",
            source_path=tmp_path / "upload" / "tmp" / "job-1" / "doc-a.pdf",
        )
    ]
    assert store.calls == [
        ("mark_running", "job-1"),
        ("succeed", "job-1"),
        ("release_doc_lock", "job-1", "doc-a"),
    ]
    assert store.closed


def test_task_failure_is_recorded_and_lock_is_released(
    tmp_path: Path, monkeypatch
) -> None:
    store = RecordingStore()

    def broken_execute(job: PipelineJob, state: RecordingStore) -> None:
        raise RuntimeError("LLM 超时")

    monkeypatch.setattr("bidding.tasks.RedisJobStore", lambda: store)
    monkeypatch.setattr("bidding.tasks.execute_pipeline", broken_execute)

    result = parse_pipeline_task.run(_payload(tmp_path))

    assert result == {"job_id": "job-1", "status": "failed"}
    assert store.calls == [
        ("mark_running", "job-1"),
        ("fail", "job-1", "LLM 超时"),
        ("release_doc_lock", "job-1", "doc-a"),
    ]
    assert store.closed


def test_task_rejects_missing_source_or_job_id(tmp_path: Path, monkeypatch) -> None:
    store = RecordingStore()
    monkeypatch.setattr("bidding.tasks.RedisJobStore", lambda: store)
    monkeypatch.setattr(
        "bidding.tasks.execute_pipeline",
        lambda job, state: pytest.fail("pipeline must not run"),
    )
    payload = _payload(tmp_path)
    Path(payload["source_path"]).unlink()

    upload_dir = Path(payload["upload_dir"])
    result = parse_pipeline_task.run(payload)
    assert result["status"] == "failed"
    assert ("fail", "job-1", "上传文件不存在或已被清理") in store.calls
    assert ("release_doc_lock", "job-1", "doc-a") in store.calls
    assert not upload_dir.exists()

    with pytest.raises(ValueError, match="job_id"):
        parse_pipeline_task.run({})


def test_ping_task() -> None:
    assert ping.run() == "pong"


def test_app_selects_backend(tmp_path: Path) -> None:
    memory_app = create_app(settings=make_settings(tmp_path, task_backend="memory"))
    assert isinstance(memory_app.state.runner, JobRunner)
    memory_app.state.runner.shutdown()

    celery_app = create_app(settings=make_settings(tmp_path, task_backend="celery"))
    assert isinstance(celery_app.state.runner, CeleryJobRunner)
    assert isinstance(celery_app.state.runner.store, RedisJobStore)

    with pytest.raises(RuntimeError, match="TASK_BACKEND"):
        create_app(settings=make_settings(tmp_path, task_backend="bogus"))


def test_app_reports_backend_outage(tmp_path: Path) -> None:
    class BrokenStore:
        def snapshot(self, job_id: str):
            raise JobBackendError("任务状态存储暂不可用")

    class BrokenRunner:
        def __init__(self) -> None:
            self.store = BrokenStore()

        def submit(self, **kwargs: Any):
            raise JobBackendError("任务状态存储暂不可用")

        def shutdown(self) -> None:
            return None

    settings = make_settings(tmp_path)
    with TestClient(create_app(settings=settings, runner=BrokenRunner())) as client:
        posted = client.post(
            "/api/parse",
            files={"file": ("tender.pdf", PDF_BYTES, "application/pdf")},
        )
        assert posted.status_code == 503
        assert posted.json()["detail"] == "任务状态存储暂不可用"
        polled = client.get("/api/jobs/anything")
        assert polled.status_code == 503

    temporary = settings.upload_dir / "tmp"
    assert not temporary.exists() or not any(temporary.iterdir())


def test_api_and_worker_share_redis_state(tmp_path: Path, monkeypatch) -> None:
    """End-to-end offline: HTTP upload → Celery task → polling from Redis."""

    def fake_parse(source: Path, *, parser: str = "auto") -> dict[str, Any]:
        return {"doc_id": Path(source).stem, "pages": 1}

    monkeypatch.setattr("bidding.job_pipeline.parse_document", fake_parse)

    redis_client = FakeRedis()
    settings = make_settings(tmp_path, task_backend="celery")
    api_store = RedisJobStore(settings, client=redis_client)
    worker_store = RedisJobStore(settings, client=redis_client)
    monkeypatch.setattr("bidding.tasks.RedisJobStore", lambda: worker_store)

    class InlineTask:
        def apply_async(self, *, kwargs: dict[str, Any], task_id: str | None = None):
            assert task_id == kwargs["payload"]["job_id"]
            parse_pipeline_task.run(kwargs["payload"])

    runner = CeleryJobRunner(api_store, task=InlineTask())
    with TestClient(create_app(settings=settings, runner=runner)) as client:
        response = client.post(
            "/api/parse",
            params={"parser": "local"},
            files={"file": ("集成招标.pdf", PDF_BYTES, "application/pdf")},
        )
        assert response.status_code == 202
        accepted = response.json()
        assert accepted["status"] == "queued"
        assert accepted["doc_id"] == "集成招标"

        job = client.get(accepted["status_url"]).json()
        assert job["status"] == "succeeded"
        assert job["stage"] == "done"
        assert job["result"]["parse"] == {"doc_id": "集成招标", "pages": 1}
        assert job["progress"][-1] == "解析完成：集成招标（1 页）"

    assert not (settings.upload_dir / "tmp" / accepted["job_id"]).exists()
    assert f"{ACTIVE_DOC_PREFIX}集成招标" not in redis_client.strings
