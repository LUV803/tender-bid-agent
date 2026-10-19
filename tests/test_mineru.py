"""MinerU client and content-list tests with no external requests."""

from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bidding.config import get_settings
from bidding.parsing.errors import MineruError
from bidding.parsing.mineru import (
    MineruClient,
    Shard,
    _content_list_to_pages,
    _extract_zip,
)


def _settings(**overrides: Any):
    return replace(
        get_settings(),
        mineru_api_key="test-key",
        mineru_language="ch",
        mineru_model_version="v4",
        mineru_poll_interval=0.01,
        mineru_max_polls=2,
        **overrides,
    )


def test_create_batch_builds_file_upload_payload(monkeypatch) -> None:
    settings = _settings(
        mineru_batch_upload_endpoint="https://mineru.test/file-urls/batch",
        mineru_is_ocr=True,
        mineru_enable_formula=True,
        mineru_enable_table=True,
    )
    client = object.__new__(MineruClient)
    client.settings = settings
    shards = [
        Shard(0, Path("part1.pdf"), "doc_part1.pdf", 0, 105),
        Shard(1, Path("part2.pdf"), "doc_part2.pdf", 105, 105),
    ]
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(method=method, url=url, kwargs=kwargs)
        return {
            "batch_id": "batch-1",
            "file_urls": ["https://upload/1", "https://upload/2"],
        }

    monkeypatch.setattr(client, "_request_json", fake_request)

    batch_id, urls = client.create_batch(shards)

    assert batch_id == "batch-1"
    assert urls == ["https://upload/1", "https://upload/2"]
    assert captured["method"] == "POST"
    assert captured["url"] == settings.mineru_batch_upload_endpoint
    payload = captured["kwargs"]["json"]
    assert [item["name"] for item in payload["files"]] == [
        "doc_part1.pdf",
        "doc_part2.pdf",
    ]
    assert all(item["is_ocr"] is True for item in payload["files"])
    assert payload["enable_table"] is True


def test_wait_for_batch_handles_done_and_failed_states(monkeypatch) -> None:
    settings = _settings(
        mineru_batch_result_endpoint="https://mineru.test/result/{batch_id}"
    )
    client = object.__new__(MineruClient)
    client.settings = settings

    done = {
        "extract_result": [
            {"state": "done", "file_name": "doc_part1.pdf"},
            {"state": "success", "file_name": "doc_part2.pdf"},
        ]
    }
    monkeypatch.setattr(client, "_request_json", lambda *args, **kwargs: done)
    results = client.wait_for_batch("batch-1", expected=2)
    assert [item["file_name"] for item in results] == [
        "doc_part1.pdf",
        "doc_part2.pdf",
    ]

    failed = {
        "extract_result": [
            {"state": "failed", "file_name": "bad.pdf", "err_msg": "解析失败"}
        ]
    }
    monkeypatch.setattr(client, "_request_json", lambda *args, **kwargs: failed)
    with pytest.raises(MineruError, match="bad.pdf"):
        client.wait_for_batch("batch-1", expected=1)


def test_extract_zip_rejects_path_traversal_entries(tmp_path: Path) -> None:
    archive_path = tmp_path / "result.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("result/good.md", "# 正常内容")
        archive.writestr("../evil.txt", "越界内容")
        archive.writestr("/absolute.txt", "绝对路径")

    destination = tmp_path / "out"
    _extract_zip(archive_path, destination)

    assert (destination / "result" / "good.md").read_text(encoding="utf-8") == "# 正常内容"
    assert not (tmp_path / "evil.txt").exists()
    assert not (destination / "absolute.txt").exists()


def test_content_list_maps_shard_offsets_and_flattens_tables(tmp_path: Path) -> None:
    shard = Shard(
        index=1,
        path=tmp_path / "part2.pdf",
        upload_name="doc_part2.pdf",
        page_offset=10,
        page_count=5,
    )
    content_list = [
        {"page_idx": 0, "type": "text", "text": "第二册第一页"},
        {
            "page_idx": 1,
            "type": "table",
            "table_caption": ["表1"],
            "table_body": "<table><tr><td>A</td><td>B</td></tr></table>",
        },
        {"type": "text", "text": "缺少页码"},
        {"page_idx": 99, "type": "text", "text": "越界页面"},
    ]
    warnings: list[str] = []

    pages, images = _content_list_to_pages(
        content_list,
        shard,
        tmp_path,
        tmp_path / "images",
        "images",
        page_count=20,
        warnings=warnings,
    )

    assert pages == {
        11: "第二册第一页",
        12: "表1\n\nA | B",
    }
    assert images == []
    assert "缺少 page_idx 的版面块" in warnings
