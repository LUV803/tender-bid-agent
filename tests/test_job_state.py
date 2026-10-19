"""Offline tests for the Redis-backed job state."""

from __future__ import annotations

from pathlib import Path

import pytest
import redis

from bidding.job_state import (
    ACTIVE_DOC_PREFIX,
    JOB_PREFIX,
    JobBackendError,
    JobConflict,
    RedisJobStore,
)
from support import FakeRedis, make_settings


def _settings(tmp_path: Path, **overrides):
    return make_settings(tmp_path, **overrides)


def _kwargs(tmp_path: Path, doc_id: str, job_id: str | None = None) -> dict:
    values = {
        "filename": f"{doc_id}.pdf",
        "doc_id": doc_id,
        "parser": "auto",
        "extract": False,
        "ingest": True,
        "upload_dir": tmp_path / "upload" / "tmp" / (job_id or doc_id),
        "source_path": tmp_path / "upload" / "tmp" / (job_id or doc_id) / "x.pdf",
    }
    if job_id is not None:
        values["job_id"] = job_id
    return values


def _store(tmp_path: Path, client: FakeRedis) -> RedisJobStore:
    return RedisJobStore(_settings(tmp_path), client=client)


def test_lifecycle_round_trip_and_document_lock(tmp_path: Path) -> None:
    client = FakeRedis()
    store = _store(tmp_path, client)
    job = store.create(**_kwargs(tmp_path, "doc-a", "job-1"))

    assert job["job_id"] == "job-1"
    assert job["status"] == "queued"
    assert job["options"] == {"parser": "auto", "extract": False, "ingest": True}
    assert client.strings[f"{ACTIVE_DOC_PREFIX}doc-a"] == "job-1"
    assert client.expirations[f"{JOB_PREFIX}job-1"] == 3600
    assert client.expirations[f"{ACTIVE_DOC_PREFIX}doc-a"] == 3600

    with pytest.raises(JobConflict):
        store.create(**_kwargs(tmp_path, "doc-a"))

    store.mark_running("job-1")
    store.set_stage("job-1", "extract")
    store.set_result("job-1", "parse", {"doc_id": "doc-a", "pages": 3})
    store.add_progress("job-1", "  抽取完成  ")
    store.succeed("job-1")

    snapshot = store.snapshot("job-1")
    assert snapshot is not None
    assert snapshot["status"] == "succeeded"
    assert snapshot["stage"] == "done"
    assert snapshot["started_at"] is not None
    assert snapshot["finished_at"] is not None
    assert snapshot["progress"] == ["抽取完成"]
    assert snapshot["result"]["parse"] == {"doc_id": "doc-a", "pages": 3}
    assert "upload_dir" not in snapshot
    assert "source_path" not in snapshot

    store.remove("job-1")
    assert store.snapshot("job-1") is None
    assert f"{ACTIVE_DOC_PREFIX}doc-a" not in client.strings

    second = store.create(**_kwargs(tmp_path, "doc-a"))
    assert second["status"] == "queued"


def test_progress_is_trimmed_and_messages_are_capped(tmp_path: Path) -> None:
    store = _store(tmp_path, FakeRedis())
    job = store.create(**_kwargs(tmp_path, "doc-a"))
    job_id = job["job_id"]
    for index in range(210):
        store.add_progress(job_id, f"step {index}")
    store.add_progress(job_id, "x" * 600)
    store.set_stage(job_id, "extract")
    store.fail(job_id, "  LLM 超时  ")

    snapshot = store.snapshot(job_id)
    assert snapshot is not None
    assert len(snapshot["progress"]) == 200
    assert snapshot["progress"][-2] == "step 209"
    assert snapshot["progress"][-1] == "x" * 500 + "…"
    assert snapshot["status"] == "failed"
    assert snapshot["stage"] == "extract"
    assert snapshot["error"] == "LLM 超时"


def test_missing_job_and_backend_errors(tmp_path: Path) -> None:
    store = _store(tmp_path, FakeRedis())
    assert store.snapshot("missing") is None

    class BrokenRedis(FakeRedis):
        def hgetall(self, key: str) -> dict[str, str]:
            raise redis.exceptions.ConnectionError("redis is down")

    broken = RedisJobStore(_settings(tmp_path), client=BrokenRedis())
    with pytest.raises(JobBackendError) as caught:
        broken.snapshot("job-1")
    assert "Redis" in str(caught.value)
    assert "redis is down" not in str(caught.value)

    class RejectingRedis(FakeRedis):
        def set(self, *args, **kwargs):
            raise redis.exceptions.ConnectionError("cannot write")

    rejecting = RedisJobStore(_settings(tmp_path), client=RejectingRedis())
    with pytest.raises(JobBackendError):
        rejecting.create(**_kwargs(tmp_path, "doc-a"))


def test_close_releases_the_client(tmp_path: Path) -> None:
    client = FakeRedis()
    _store(tmp_path, client).close()
    assert client.closed
