"""Offline FastAPI tests: upload validation, polling and optional stages."""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.jobs import JobRunner, JobStore
from bidding.config import Settings, get_settings
from bidding.job_pipeline import execute_pipeline
from bidding.parsing import ParseError

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


class _InlineExecutor:
    """Run submitted work synchronously so tests can assert final state."""

    def submit(self, fn, /, *args, **kwargs) -> None:
        fn(*args, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    upload = tmp_path / "upload"
    values = {
        "upload_dir": upload,
        "files_dir": upload / "files",
        "parsed_dir": upload / "parsed",
        "images_dir": upload / "images",
        "bids_dir": upload / "bids",
        "api_max_upload_mb": 1,
        "api_job_workers": 1,
        "api_job_ttl_seconds": 3600,
        "api_max_jobs": 10,
        "minio_enabled": False,
        **overrides,
    }
    return replace(get_settings(), **values)


def _client(
    settings: Settings,
    runner: JobRunner,
) -> TestClient:
    return TestClient(create_app(settings=settings, runner=runner))


def test_parse_returns_202_before_work_finishes(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def work(job, store) -> None:
        store.set_stage(job.id, "parse")
        started.set()
        assert release.wait(5)
        store.set_result(job.id, "parse", {"doc_id": job.doc_id, "pages": 1})

    runner = JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        work,
        max_workers=1,
    )
    client = _client(_settings(tmp_path), runner)

    with client:
        response = client.post(
            "/api/parse",
            params={"parser": "local"},
            files={"file": ("示例招标.pdf", PDF_BYTES, "application/pdf")},
        )
        assert response.status_code == 202
        accepted = response.json()
        assert accepted["status"] == "queued"
        assert accepted["stage"] == "queued"
        assert accepted["doc_id"] == "示例招标"
        assert accepted["status_url"] == response.headers["location"]
        assert accepted["status_url"] == f"/api/jobs/{accepted['job_id']}"

        try:
            assert started.wait(5)
            running = client.get(accepted["status_url"]).json()
            assert running["status"] == "running"
            assert running["stage"] == "parse"
            assert running["filename"] == "示例招标.pdf"
            assert running["options"]["parser"] == "local"
            assert running["started_at"] is not None
            assert running["finished_at"] is None
        finally:
            release.set()

        deadline = time.monotonic() + 5
        while True:
            job = client.get(accepted["status_url"]).json()
            if job["status"] in {"succeeded", "failed"}:
                break
            assert time.monotonic() < deadline, "job did not finish in time"
            time.sleep(0.01)

    assert job["status"] == "succeeded"
    assert job["stage"] == "done"
    assert job["finished_at"] is not None
    assert job["result"]["parse"] == {"doc_id": "示例招标", "pages": 1}


@pytest.mark.parametrize(
    ("params", "expected_stages"),
    [
        ({}, []),
        ({"extract": "true"}, ["extract"]),
        ({"ingest": "true"}, ["ingest"]),
        ({"extract": "true", "ingest": "true"}, ["extract", "ingest"]),
    ],
)
def test_optional_extract_and_ingest_stages(
    tmp_path: Path,
    monkeypatch,
    params: dict[str, str],
    expected_stages: list[str],
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_parse(source: Path, *, parser: str = "auto") -> dict[str, Any]:
        calls.append(("parse", Path(source).stem))
        return {"doc_id": Path(source).stem, "pages": 2}

    def fake_extract(doc_id: str, *, progress=None) -> dict[str, Any]:
        calls.append(("extract", doc_id))
        if progress is not None:
            progress("抽取进度")
        return {"project_name": "测试项目"}

    def fake_ingest(doc_id: str, *, progress=None) -> dict[str, Any]:
        calls.append(("ingest", doc_id))
        if progress is not None:
            progress("入库进度")
        return {"chunks": 3}

    monkeypatch.setattr("bidding.job_pipeline.parse_document", fake_parse)
    monkeypatch.setattr("bidding.job_pipeline.run_extract", fake_extract)
    monkeypatch.setattr("bidding.job_pipeline.run_ingest", fake_ingest)

    settings = _settings(tmp_path)
    runner = JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        execute_pipeline,
        executor=_InlineExecutor(),
    )
    client = _client(settings, runner)

    with client:
        response = client.post(
            "/api/parse",
            params={"parser": "local", **params},
            files={"file": ("测试招标.pdf", PDF_BYTES, "application/pdf")},
        )
        assert response.status_code == 202
        accepted = response.json()
        job = client.get(accepted["status_url"]).json()

    assert job["status"] == "succeeded"
    assert job["stage"] == "done"
    assert calls == [
        ("parse", "测试招标"),
        *[(stage, "测试招标") for stage in expected_stages],
    ]
    assert set(job["result"]) == {"parse", *expected_stages}
    if "extract" in expected_stages:
        assert job["result"]["extract"] == {"project_name": "测试项目"}
        assert "抽取进度" in job["progress"]
    if "ingest" in expected_stages:
        assert job["result"]["ingest"] == {"chunks": 3}
        assert "入库进度" in job["progress"]
    assert not (settings.upload_dir / "tmp" / accepted["job_id"]).exists()


def test_pipeline_failure_is_reported_and_upload_is_cleaned(
    tmp_path: Path, monkeypatch
) -> None:
    def broken_parse(source: Path, *, parser: str = "auto") -> dict[str, Any]:
        raise ParseError("PDF 已损坏")

    monkeypatch.setattr("bidding.job_pipeline.parse_document", broken_parse)
    settings = _settings(tmp_path)
    runner = JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        execute_pipeline,
        executor=_InlineExecutor(),
    )
    client = _client(settings, runner)

    with client:
        response = client.post(
            "/api/parse",
            files={"file": ("坏文件.pdf", PDF_BYTES, "application/pdf")},
        )
        assert response.status_code == 202
        accepted = response.json()
        job = client.get(accepted["status_url"]).json()

    assert job["status"] == "failed"
    assert job["stage"] == "parse"
    assert job["error"] == "PDF 已损坏"
    assert job["result"] == {}
    assert not (settings.upload_dir / "tmp" / accepted["job_id"]).exists()


def test_rejects_non_pdf_filename_and_missing_header(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runner = JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        execute_pipeline,
        executor=_InlineExecutor(),
    )

    with _client(settings, runner) as client:
        wrong_extension = client.post(
            "/api/parse",
            files={"file": ("notes.txt", PDF_BYTES, "text/plain")},
        )
        assert wrong_extension.status_code == 400
        assert "仅支持 .pdf" in wrong_extension.json()["detail"]

        wrong_content = client.post(
            "/api/parse",
            files={"file": ("fake.pdf", b"not really a pdf", "application/pdf")},
        )
        assert wrong_content.status_code == 400
        assert "%PDF-" in wrong_content.json()["detail"]

    temporary = settings.upload_dir / "tmp"
    assert not temporary.exists() or not any(temporary.iterdir())


def test_rejects_oversized_upload(tmp_path: Path) -> None:
    settings = _settings(tmp_path, api_max_upload_mb=0)
    runner = JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        execute_pipeline,
        executor=_InlineExecutor(),
    )

    with _client(settings, runner) as client:
        response = client.post(
            "/api/parse",
            files={"file": ("large.pdf", PDF_BYTES, "application/pdf")},
        )

    assert response.status_code == 413
    assert "大小上限" in response.json()["detail"]
    temporary = settings.upload_dir / "tmp"
    assert not temporary.exists() or not any(temporary.iterdir())


def test_invalid_parser_and_unknown_job(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runner = JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        execute_pipeline,
        executor=_InlineExecutor(),
    )

    with _client(settings, runner) as client:
        invalid = client.post(
            "/api/parse",
            params={"parser": "unknown"},
            files={"file": ("tender.pdf", PDF_BYTES, "application/pdf")},
        )
        assert invalid.status_code == 422

        missing = client.get("/api/jobs/does-not-exist")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "任务不存在或已过期"


def test_active_duplicate_upload_returns_409(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def work(job, store) -> None:
        started.set()
        assert release.wait(5)

    runner = JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        work,
        max_workers=1,
    )
    client = _client(_settings(tmp_path), runner)

    with client:
        first = client.post(
            "/api/parse",
            files={"file": ("重复.pdf", PDF_BYTES, "application/pdf")},
        )
        assert first.status_code == 202
        assert started.wait(5)
        try:
            duplicate = client.post(
                "/api/parse",
                files={"file": ("重复.pdf", PDF_BYTES, "application/pdf")},
            )
            assert duplicate.status_code == 409
            assert "已有任务" in duplicate.json()["detail"]
        finally:
            release.set()


def _prepare_bid_artifacts(settings: Settings, doc_id: str) -> None:
    parsed = settings.parsed_dir / doc_id
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / "manifest.json").write_text(
        json.dumps({"doc_id": doc_id, "pages": 1}, ensure_ascii=False),
        encoding="utf-8",
    )
    (parsed / "extraction.json").write_text(
        json.dumps({"project_name": "测试项目"}, ensure_ascii=False),
        encoding="utf-8",
    )


def _bid_runner(work) -> JobRunner:
    return JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        execute_pipeline,
        executor=_InlineExecutor(),
        works={"bid": work},
    )


def test_bid_accepts_json_profile_and_reuses_polling(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    doc_id = "招标A"
    _prepare_bid_artifacts(settings, doc_id)
    seen: list[dict[str, Any]] = []

    def work(job, store) -> None:
        try:
            profile = json.loads(job.profile_path.read_text(encoding="utf-8"))
            seen.append(profile)
            store.add_progress(job.id, "编写完成")
            store.set_result(
                job.id,
                "bid",
                {
                    "doc_id": job.doc_id,
                    "bidder": profile["company"]["name"],
                    "stats": {"sections": 11},
                    "artifacts": {"markdown": "bid.md"},
                    "warnings": [],
                },
            )
        finally:
            shutil.rmtree(job.upload_dir, ignore_errors=True)

    client = _client(settings, _bid_runner(work))
    profile = {"company": {"name": "测试投标人"}}

    with client:
        response = client.post(
            "/api/bids",
            json={
                "doc_id": doc_id,
                "profile": profile,
                "max_sections": 2,
                "max_revisions": 1,
            },
        )
        assert response.status_code == 202
        accepted = response.json()
        assert accepted["status"] == "queued"
        assert accepted["stage"] == "queued"
        assert accepted["doc_id"] == doc_id
        assert accepted["status_url"] == response.headers["location"]
        assert accepted["status_url"] == f"/api/jobs/{accepted['job_id']}"

        job = client.get(accepted["status_url"]).json()

    assert job["status"] == "succeeded"
    assert job["stage"] == "done"
    assert job["kind"] == "bid"
    assert job["filename"] == f"{doc_id} 投标文件"
    assert job["options"] == {"max_sections": 2, "max_revisions": 1}
    assert job["result"]["bid"]["bidder"] == "测试投标人"
    assert job["progress"] == ["编写完成"]
    assert seen == [profile]
    assert not (settings.upload_dir / "tmp" / accepted["job_id"]).exists()


def test_bid_validates_request_and_requires_parsed_extraction(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runner = _bid_runner(lambda job, store: None)
    client = _client(settings, runner)

    with client:
        missing = client.post("/api/bids", json={"doc_id": "未解析"})
        assert missing.status_code == 404
        assert "尚未解析" in missing.json()["detail"]

        partial = settings.parsed_dir / "仅解析"
        partial.mkdir(parents=True)
        (partial / "manifest.json").write_text("{}", encoding="utf-8")
        unparsed = client.post("/api/bids", json={"doc_id": "仅解析"})
        assert unparsed.status_code == 409
        assert "结构化抽取" in unparsed.json()["detail"]

        bad_doc = client.post("/api/bids", json={"doc_id": "../evil"})
        assert bad_doc.status_code == 422

        too_many = client.post(
            "/api/bids", json={"doc_id": "未解析", "max_sections": 4}
        )
        assert too_many.status_code == 422

        too_many_revisions = client.post(
            "/api/bids", json={"doc_id": "未解析", "max_revisions": 11}
        )
        assert too_many_revisions.status_code == 422

        bad_profile = client.post(
            "/api/bids", json={"doc_id": "未解析", "profile": ["not", "an", "object"]}
        )
        assert bad_profile.status_code == 422

        oversized = client.post(
            "/api/bids",
            json={"doc_id": "未解析", "profile": {"note": "x" * (2 * 1024 * 1024)}},
        )
        assert oversized.status_code == 422

    temporary = settings.upload_dir / "tmp"
    assert not temporary.exists() or not any(temporary.iterdir())


def test_duplicate_active_bid_for_same_document_returns_409(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    doc_id = "重复写标"
    _prepare_bid_artifacts(settings, doc_id)
    started = threading.Event()
    release = threading.Event()

    def work(job, store) -> None:
        try:
            started.set()
            assert release.wait(5)
        finally:
            shutil.rmtree(job.upload_dir, ignore_errors=True)

    runner = JobRunner(
        JobStore(max_jobs=5, ttl_seconds=3600),
        execute_pipeline,
        max_workers=1,
        works={"bid": work},
    )
    client = _client(settings, runner)
    body = {"doc_id": doc_id, "profile": {"company": {"name": "投标人"}}}

    with client:
        first = client.post("/api/bids", json=body)
        assert first.status_code == 202
        assert started.wait(5)
        try:
            duplicate = client.post("/api/bids", json=body)
            assert duplicate.status_code == 409
            assert "已有任务" in duplicate.json()["detail"]
            temporary = settings.upload_dir / "tmp"
            assert [path.name for path in temporary.iterdir()] == [
                first.json()["job_id"]
            ]
        finally:
            release.set()
