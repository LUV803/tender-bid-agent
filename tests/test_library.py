"""Offline tests for the document library and bid artifact APIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.app import create_app
from support import make_settings


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _library(tmp_path: Path):
    settings = make_settings(tmp_path)
    parsed = settings.parsed_dir / "doc-a"
    _write_json(
        parsed / "manifest.json",
        {
            "doc_id": "doc-a",
            "source_name": "tender.pdf",
            "pages": 10,
            "parser": "pymupdf",
            "parsed_at": "2026-10-19T10:00:00+08:00",
            "sections": [{"title": "第一章"}, {"title": "第二章"}],
            "toc": [{"title": "第一章"}],
            "warnings": ["示例警告"],
        },
    )
    _write_json(
        parsed / "extraction.json",
        {
            "doc_id": "doc-a",
            "project_name": "测试项目",
            "evaluation_factors": [{"factor": "技术分"}],
            "ungrounded_fields": ["字段一"],
            "extraction_meta": {"partial": False, "ungrounded_count": 1},
        },
    )
    _write_json(parsed / "index_manifest.json", {"chunks": 3})

    bid_dir = settings.bids_dir / "doc-a"
    _write_json(
        bid_dir / "bid_manifest.json",
        {
            "doc_id": "doc-a",
            "created_at": "2026-10-19T11:00:00+08:00",
            "elapsed_seconds": 12.5,
            "project": {"project_name": "测试项目"},
            "bidder": {"company_name": "测试投标人"},
            "stats": {
                "sections": 2,
                "pending_markers": 1,
                "issues": 2,
                "high_issues": 1,
                "medium_issues": 0,
                "low_issues": 1,
                "revisions": 0,
            },
            "settings": {"max_sections": 0, "max_revisions": 2},
            "warnings": ["生成警告"],
            "outline": {
                "sections": [
                    {
                        "id": "s1",
                        "title": "投标函",
                        "role": "admin",
                        "origin": "base",
                        "word_target": 100,
                    },
                    {
                        "id": "s2",
                        "title": "技术方案",
                        "role": "technical",
                        "origin": "base",
                        "word_target": 200,
                    },
                ]
            },
            "response_matrix": [
                {
                    "factor": "技术方案",
                    "category": "技术",
                    "max_score": 30,
                    "criteria": "技术方案完整",
                    "section_ids": ["s2"],
                    "section_titles": ["技术方案"],
                },
                {
                    "factor": "投标报价",
                    "category": "商务",
                    "max_score": 20,
                    "criteria": "报价合理",
                    "section_ids": [],
                    "section_titles": [],
                },
            ],
            "review": {
                "summary": "总体可行，需补充事实依据。",
                "issues": [
                    {
                        "severity": "high",
                        "category": "fact",
                        "section_id": "s2",
                        "problem": "缺少依据",
                        "fix": "补充证明材料",
                        "tender_basis": "",
                    },
                    {
                        "severity": "low",
                        "category": "pending",
                        "section_id": "",
                        "problem": "存在占位符",
                        "fix": "人工填写",
                        "tender_basis": "",
                    },
                ],
                "unverified_claims": [{"claim": "经验丰富"}],
                "missing_requirements": [],
            },
            "pending_markers": [
                {"marker": "【待填写：公司名称】", "sections": ["s1"]}
            ],
            "profile_gaps": [
                {
                    "label": "公司名称",
                    "path": "company.name",
                    "placeholder": "【待填写：公司名称】",
                    "section": "company",
                }
            ],
            "revisions": [],
        },
    )
    (bid_dir / "bid.md").write_text("# 投标文件\n\n正文\n", encoding="utf-8")
    (bid_dir / "bid_review.md").write_text("# 审查报告\n", encoding="utf-8")
    (bid_dir / "bid.docx").write_bytes(b"PK\x03\x04fake-docx")
    return settings


def test_lists_parsed_documents_and_bid_history(tmp_path: Path) -> None:
    settings = _library(tmp_path)
    with TestClient(create_app(settings=settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        documents = client.get("/api/documents")
        assert documents.status_code == 200
        items = documents.json()
        assert len(items) == 1
        item = items[0]
        assert item["doc_id"] == "doc-a"
        assert item["project_name"] == "测试项目"
        assert item["pages"] == 10
        assert item["section_count"] == 2
        assert item["has_extraction"] is True
        assert item["has_index"] is True
        assert item["evaluation_factor_count"] == 1
        assert item["ungrounded_count"] == 1
        assert item["bid"]["bidder"] == "测试投标人"

        history = client.get("/api/bids")
        assert history.status_code == 200
        bids = history.json()
        assert len(bids) == 1
        bid = bids[0]
        assert bid["project_name"] == "测试项目"
        assert bid["stats"]["issues"] == 2
        assert bid["folder"] == str((settings.bids_dir / "doc-a").resolve())
        names = {artifact["name"] for artifact in bid["artifacts"]}
        assert names == {"bid.md", "bid.docx", "bid_review.md", "bid_manifest.json"}


def test_bid_detail_reports_coverage_and_sections(tmp_path: Path) -> None:
    settings = _library(tmp_path)
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/api/bids/doc-a")
        assert response.status_code == 200
        detail = response.json()

    assert detail["review_summary"].startswith("总体可行")
    coverage = detail["coverage"]
    assert coverage["total_factors"] == 2
    assert coverage["covered_factors"] == 1
    assert coverage["factor_coverage"] == 0.5
    assert coverage["max_score"] == 50.0
    assert coverage["covered_max_score"] == 30.0
    assert coverage["score_coverage"] == 0.6
    assert coverage["unmapped_factors"][0]["factor"] == "投标报价"

    sections = {section["id"]: section for section in detail["sections"]}
    assert sections["s1"]["factor_count"] == 0
    assert sections["s2"]["factor_count"] == 1
    assert sections["s2"]["factor_score"] == 30.0
    assert sections["s2"]["high_issues"] == 1
    assert detail["pending_count"] == 1
    assert detail["profile_gap_count"] == 1


def test_artifact_preview_download_and_path_guards(tmp_path: Path) -> None:
    settings = _library(tmp_path)
    with TestClient(create_app(settings=settings)) as client:
        preview = client.get("/api/bids/doc-a/files/bid.md")
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("text/markdown")
        assert "投标文件" in preview.text
        assert "content-disposition" not in preview.headers

        download = client.get(
            "/api/bids/doc-a/files/bid.md", params={"download": "true"}
        )
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment")
        assert "bid.md" in download.headers["content-disposition"]

        docx = client.get("/api/bids/doc-a/files/bid.docx")
        assert docx.status_code == 200
        assert docx.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument"
        )

        unknown = client.get("/api/bids/doc-a/files/secret.txt")
        assert unknown.status_code == 404

        traversal = client.get("/api/bids/%2E%2E/files/bid.md")
        assert traversal.status_code == 404

        missing = client.get("/api/bids/missing")
        assert missing.status_code == 404
