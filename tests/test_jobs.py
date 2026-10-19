"""In-memory job store tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.jobs import JobConflict, JobQueueFull, JobStore


def _kwargs(tmp_path: Path, doc_id: str) -> dict:
    return {
        "filename": f"{doc_id}.pdf",
        "doc_id": doc_id,
        "parser": "auto",
        "extract": False,
        "ingest": False,
        "upload_dir": tmp_path / "tmp" / doc_id,
        "source_path": tmp_path / "tmp" / doc_id / f"{doc_id}.pdf",
    }


def test_active_job_for_same_doc_id_is_rejected(tmp_path: Path) -> None:
    store = JobStore(max_jobs=5, ttl_seconds=3600)
    job = store.create(**_kwargs(tmp_path, "doc-a"))

    with pytest.raises(JobConflict):
        store.create(**_kwargs(tmp_path, "doc-a"))

    store.mark_running(job.id)
    store.succeed(job.id)
    second = store.create(**_kwargs(tmp_path, "doc-a"))
    assert second.status == "queued"


def test_store_is_bounded_and_snapshot_hides_internal_paths(tmp_path: Path) -> None:
    store = JobStore(max_jobs=1, ttl_seconds=3600)
    first = store.create(**_kwargs(tmp_path, "doc-a"))
    store.mark_running(first.id)
    store.succeed(first.id)

    second = store.create(**_kwargs(tmp_path, "doc-b"))
    assert store.snapshot(first.id) is None

    snapshot = store.snapshot(second.id)
    assert snapshot is not None
    assert snapshot["status"] == "queued"
    assert snapshot["options"] == {"parser": "auto", "extract": False, "ingest": False}
    assert "upload_dir" not in snapshot
    assert "source_path" not in snapshot

    with pytest.raises(JobQueueFull):
        store.create(**_kwargs(tmp_path, "doc-c"))


def test_progress_is_trimmed_and_failure_keeps_the_stage(tmp_path: Path) -> None:
    store = JobStore(max_jobs=5, ttl_seconds=3600)
    job = store.create(**_kwargs(tmp_path, "doc-a"))
    store.mark_running(job.id)
    store.set_stage(job.id, "extract")
    for index in range(210):
        store.add_progress(job.id, f"step {index}")
    store.fail(job.id, "  LLM 超时  ")

    snapshot = store.snapshot(job.id)
    assert snapshot is not None
    assert len(snapshot["progress"]) == 200
    assert snapshot["progress"][-1] == "step 209"
    assert snapshot["status"] == "failed"
    assert snapshot["stage"] == "extract"
    assert snapshot["error"] == "LLM 超时"
