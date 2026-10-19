"""Offline MinIO mirror, restore and API fallback tests."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.app import create_app
from api.library import bid_detail, list_documents
from bidding import storage
from support import make_settings

BUCKET = "bidding"


class FakeS3Error(Exception):
    def __init__(self, code: str = "NoSuchKey") -> None:
        super().__init__(code)
        self.code = code


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)
        self.closed = False
        self.released = False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self.closed = True
        self._stream.close()

    def release_conn(self) -> None:
        self.released = True


class _FakeObject:
    def __init__(self, key: str, size: int) -> None:
        self.object_name = key
        self.size = size
        self.last_modified = datetime(2026, 10, 19, tzinfo=timezone.utc)
        self.is_dir = False


class FakeMinio:
    """In-memory subset of the MinIO client used by ``bidding.storage``."""

    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.content_types: dict[str, str] = {}

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def fput_object(
        self,
        bucket: str,
        key: str,
        path: str,
        content_type: str | None = None,
    ) -> None:
        self.objects[(bucket, key)] = Path(path).read_bytes()
        self.content_types[key] = content_type or ""

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        length: int,
        content_type: str | None = None,
    ) -> None:
        self.objects[(bucket, key)] = data[:length]
        self.content_types[key] = content_type or ""

    def get_object(self, bucket: str, key: str) -> _FakeResponse:
        if (bucket, key) not in self.objects:
            raise FakeS3Error()
        return _FakeResponse(self.objects[(bucket, key)])

    def stat_object(self, bucket: str, key: str) -> _FakeObject:
        if (bucket, key) not in self.objects:
            raise FakeS3Error()
        return _FakeObject(key, len(self.objects[(bucket, key)]))

    def list_objects(
        self, bucket: str, prefix: str = "", recursive: bool = True
    ):
        for (current_bucket, key), data in sorted(self.objects.items()):
            if current_bucket == bucket and key.startswith(prefix):
                yield _FakeObject(key, len(data))

    def remove_object(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)


class BrokenMinio:
    def bucket_exists(self, bucket: str) -> bool:
        raise ConnectionError("MinIO unavailable")

    def make_bucket(self, bucket: str) -> None:
        raise ConnectionError("MinIO unavailable")


def _settings(tmp_path: Path):
    return make_settings(
        tmp_path,
        minio_enabled=True,
        minio_endpoint="127.0.0.1:9005",
        minio_access_key="access",
        minio_secret_key="secret",
        minio_bucket=BUCKET,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _seed_local(settings) -> Path:
    parsed = settings.parsed_dir / "doc-a"
    _write_json(
        parsed / "manifest.json",
        {
            "doc_id": "doc-a",
            "source_name": "tender.pdf",
            "source_path": settings.relative(
                settings.files_dir / "doc-a_tender.pdf"
            ),
            "pages": 3,
            "parser": "pymupdf",
            "parsed_at": "2026-10-19T10:00:00+08:00",
            "sections": [{"title": "第一章"}],
            "toc": [{"title": "第一章"}],
            "warnings": [],
        },
    )
    _write(parsed / "content.md", "# 招标文件\n")
    _write_json(parsed / "raw" / "sections.json", {"sections": [], "toc": []})
    _write_json(parsed / "extraction.json", {"project_name": "测试项目"})
    _write_json(parsed / "extraction_parts" / "u1.json", {"result": {}})
    _write_json(parsed / "index_manifest.json", {"chunks": 3})
    _write(settings.images_dir / "doc-a" / "image.png", "png-bytes")
    _write(settings.files_dir / "doc-a_tender.pdf", "%PDF-1.4 source")
    _write(settings.bids_dir / "doc-a" / "bid.md", "# 投标文件\n")
    _write(settings.bids_dir / "doc-a" / "bid.docx", "docx-bytes")
    _write(settings.bids_dir / "doc-a" / "bid_review.md", "# 审查\n")
    _write_json(
        settings.bids_dir / "doc-a" / "bid_manifest.json",
        {
            "doc_id": "doc-a",
            "created_at": "2026-10-19T11:00:00+08:00",
            "project": {"project_name": "测试项目"},
            "bidder": {"company_name": "测试投标人"},
            "stats": {"pending_markers": 1},
            "outline": {"sections": []},
            "response_matrix": [],
            "review": {"issues": []},
        },
    )
    return parsed


def _mirror_all(settings) -> None:
    assert storage.mirror_parse_artifacts("doc-a", settings) == []
    assert storage.mirror_extraction("doc-a", settings) == []
    assert storage.mirror_index("doc-a", settings) == []
    assert storage.mirror_bid_artifacts("doc-a", settings) == []


def _clear_local(settings) -> None:
    import shutil

    for path in (
        settings.parsed_dir / "doc-a",
        settings.images_dir / "doc-a",
        settings.bids_dir / "doc-a",
        settings.files_dir / "doc-a_tender.pdf",
    ):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def test_mirror_and_restore_round_trip(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    fake = FakeMinio()
    monkeypatch.setattr(storage, "_client", lambda _settings: fake)
    _seed_local(settings)

    _mirror_all(settings)

    keys = {key for bucket, key in fake.objects if bucket == BUCKET}
    assert "documents/doc-a/parsed/manifest.json" in keys
    assert "documents/doc-a/parsed/content.md" in keys
    assert "documents/doc-a/parsed/raw/sections.json" in keys
    assert "documents/doc-a/parsed/extraction.json" in keys
    assert "documents/doc-a/parsed/extraction_parts/u1.json" in keys
    assert "documents/doc-a/parsed/index_manifest.json" in keys
    assert "documents/doc-a/images/image.png" in keys
    assert "documents/doc-a/source/doc-a_tender.pdf" in keys
    assert "documents/doc-a/bids/bid.md" in keys
    assert storage.remote_document_ids(settings, kind="parsed") == ["doc-a"]
    assert storage.remote_document_ids(settings, kind="bids") == ["doc-a"]
    assert storage.remote_bid_files(settings, "doc-a")["bid.docx"] == len(
        b"docx-bytes"
    )

    _clear_local(settings)
    assert storage.hydrate_document(
        "doc-a", settings, include_images=True
    ) == []
    assert storage.hydrate_bid("doc-a", settings) == []

    parsed = settings.parsed_dir / "doc-a"
    assert (parsed / "content.md").read_text(encoding="utf-8") == "# 招标文件\n"
    assert (parsed / "raw" / "sections.json").is_file()
    assert (settings.images_dir / "doc-a" / "image.png").read_text() == "png-bytes"
    assert (settings.bids_dir / "doc-a" / "bid.md").read_text(encoding="utf-8") == (
        "# 投标文件\n"
    )
    source = storage.hydrate_source("doc-a", settings)
    assert source is not None and source.is_file()


def test_upload_tree_clean_removes_stale_objects(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    fake = FakeMinio()
    monkeypatch.setattr(storage, "_client", lambda _settings: fake)
    _seed_local(settings)
    fake.objects[(BUCKET, "documents/doc-a/parsed/old.md")] = b"stale"

    _mirror_all(settings)

    assert (BUCKET, "documents/doc-a/parsed/old.md") not in fake.objects
    assert (BUCKET, "documents/doc-a/parsed/content.md") in fake.objects


def test_storage_failures_are_best_effort(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(storage, "_client", lambda _settings: BrokenMinio())
    _seed_local(settings)

    warnings = storage.mirror_parse_artifacts("doc-a", settings)
    assert warnings and "MinIO 同步失败" in warnings[0]
    assert storage.hydrate_document("doc-a", settings)
    assert storage.hydrate_bid("doc-a", settings)
    assert storage.hydrate_bid_file("doc-a", "bid.md", settings) is False

    documents = list_documents(settings)
    assert [item["doc_id"] for item in documents] == ["doc-a"]


def test_library_reads_remote_artifacts_and_hydrates(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    fake = FakeMinio()
    monkeypatch.setattr(storage, "_client", lambda _settings: fake)
    _seed_local(settings)
    _mirror_all(settings)
    _clear_local(settings)

    documents = list_documents(settings)
    assert len(documents) == 1
    item = documents[0]
    assert item["project_name"] == "测试项目"
    assert item["has_extraction"] is True
    assert item["has_index"] is True
    assert item["bid"]["bidder"] == "测试投标人"
    assert {artifact["name"] for artifact in item["bid"]["artifacts"]} == {
        "bid.md",
        "bid.docx",
        "bid_review.md",
        "bid_manifest.json",
    }

    detail = bid_detail(settings, "doc-a")
    assert detail is not None
    assert detail["coverage"]["total_factors"] == 0
    # Viewing the detail restores the folder for desktop "open folder".
    assert (settings.bids_dir / "doc-a" / "bid.docx").is_file()


class _RecordingRunner:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit(self, **kwargs: Any) -> dict[str, str]:
        self.submitted.append(kwargs)
        return {
            "job_id": kwargs["job_id"],
            "status": "queued",
            "stage": "queued",
            "doc_id": kwargs["doc_id"],
        }

    def shutdown(self, wait: bool = True) -> None:
        return None


def test_api_restores_remote_document_for_bid_and_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    fake = FakeMinio()
    monkeypatch.setattr(storage, "_client", lambda _settings: fake)
    _seed_local(settings)
    _mirror_all(settings)
    _clear_local(settings)

    runner = _RecordingRunner()
    client = TestClient(create_app(settings=settings, runner=runner))

    response = client.post(
        "/api/bids",
        json={"doc_id": "doc-a", "profile": {"company": {"name": "示例"}}},
    )
    assert response.status_code == 202
    assert runner.submitted[0]["doc_id"] == "doc-a"
    assert (settings.parsed_dir / "doc-a" / "extraction.json").is_file()

    # Artifact fetch also works from the remote copy and caches it locally.
    _clear_local(settings)
    artifact = client.get("/api/bids/doc-a/files/bid.md")
    assert artifact.status_code == 200
    assert artifact.text == "# 投标文件\n"
    assert (settings.bids_dir / "doc-a" / "bid.md").is_file()
