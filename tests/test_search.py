"""Offline tests for hybrid tender search and the ``GET /api/search`` endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.jobs import JobRunner, JobStore
from bidding.search import load_document, search_document
from tests.support import make_settings

DOC_ID = "demo-tender"
SOURCE_NAME = "示例招标文件.pdf"

CONTENT_MD = """\
<!-- page: 1 -->
目录
表10：投标报价得分计算表 ...... 2
表11：投标总价评分表 ...... 3
<!-- page: 2 -->
## 评标办法
投标报价得分计算：投标报价等于评标基准价时得50分，每高1%扣1分。
表10：投标报价得分计算表
投标人 | 投标报价（元） | 得分
甲公司 | 1000000 | 50
<!-- page: 3 -->
表11：投标总价评分表
投标总价得分 = 投标报价得分 + 其他因素得分。评审时按得分由高到低排序。
<!-- page: 4 -->
六、施工组织设计
施工组织设计应包括施工方案、施工进度计划、质量保证措施和安全生产措施，并按评标办法要求编制。
<!-- page: 5 -->
## 资格审查资料
（一）投标人基本情况表
（二）近年财务状况表
（三）近三年完成的类似项目情况表
（四）投标人信誉情况表
资格审查需要提供营业执照、资质证书和安全生产许可证等材料。
"""

SECTIONS: list[dict[str, Any]] = [
    {
        "title": "投标报价得分计算",
        "path": ["第二章 评标办法"],
        "page_start": 2,
        "page_end": 2,
        "text": (
            "<!-- page: 2 -->\n"
            "投标报价得分计算：投标报价等于评标基准价时得50分，每高1%扣1分。"
        ),
    },
    {
        "title": "表11：投标总价评分表",
        "path": ["第二章 评标办法"],
        "page_start": 3,
        "page_end": 3,
        "text": (
            "<!-- page: 3 -->\n"
            "投标总价得分 = 投标报价得分 + 其他因素得分。评审时按得分由高到低排序。"
        ),
    },
    {
        "title": "六、施工组织设计",
        "path": ["第六章 投标文件格式"],
        "page_start": 4,
        "page_end": 4,
        "text": (
            "<!-- page: 4 -->\n"
            "施工组织设计应包括施工方案、施工进度计划、质量保证措施和安全生产措施，"
            "并按评标办法要求编制。"
        ),
    },
    {
        "title": "资格审查资料",
        "path": ["第七章 资格审查"],
        "page_start": 5,
        "page_end": 5,
        "text": (
            "<!-- page: 5 -->\n"
            "（一）投标人基本情况表\n（二）近年财务状况表\n"
            "（三）近三年完成的类似项目情况表\n（四）投标人信誉情况表\n"
            "资格审查需要提供营业执照、资质证书和安全生产许可证等材料。"
        ),
    },
]

EXTRACTION: dict[str, Any] = {
    "project_name": "示例供水工程",
    "evaluation_factors": [
        {
            "factor": "投标报价",
            "max_score": 50,
            "category": "商务",
            "criteria": "投标报价等于评标基准价时得50分，每高1%扣1分。",
            "source_quote": "投标报价得分计算：投标报价等于评标基准价时得50分，每高1%扣1分。",
        },
        {
            "factor": "施工组织设计",
            "max_score": 30,
            "category": "技术",
            "criteria": "施工方案、进度计划、质量保证措施和安全生产措施完整。",
            "source_quote": "施工组织设计应包括施工方案、施工进度计划、质量保证措施和安全生产措施",
        },
    ],
    "qualifications": [
        {
            "category": "资格",
            "requirement": "具有市政公用工程施工总承包三级及以上资质",
            "thresholds": ["市政公用工程施工总承包三级"],
            "source_quote": "资格审查需要提供营业执照、资质证书和安全生产许可证等材料。",
        }
    ],
    "preliminary_review": [
        {
            "stage": "资格评审",
            "item": "营业执照",
            "criteria": "提供有效的营业执照副本",
            "source_quote": "资格审查需要提供营业执照、资质证书和安全生产许可证等材料。",
        }
    ],
    "bid_format": {
        "items": [
            {
                "title": "资格审查资料",
                "requirements": "包括投标人基本情况表、近年财务状况表、类似项目情况表、信誉情况表。",
            }
        ]
    },
}


def _write_document(settings) -> None:
    parsed = settings.parsed_dir / DOC_ID
    (parsed / "raw").mkdir(parents=True, exist_ok=True)
    (parsed / "manifest.json").write_text(
        json.dumps(
            {"source_name": SOURCE_NAME, "toc_pages": [1], "page_count": 5},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (parsed / "content.md").write_text(CONTENT_MD, encoding="utf-8")
    (parsed / "raw" / "sections.json").write_text(
        json.dumps({"sections": SECTIONS}, ensure_ascii=False),
        encoding="utf-8",
    )
    (parsed / "extraction.json").write_text(
        json.dumps(EXTRACTION, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture()
def settings(tmp_path: Path):
    value = make_settings(tmp_path, zhipu_api_key="")
    _write_document(value)
    return value


def test_load_document_reads_artifacts(settings) -> None:
    document = load_document(settings, DOC_ID)
    assert document.source_name == SOURCE_NAME
    assert document.project_name == "示例供水工程"
    assert len(document.sections) == 4
    assert "## 资格审查资料" in document.page_index.raw_pages[5]


def test_price_score_query_cites_tables(settings) -> None:
    response = search_document(
        settings, DOC_ID, "投标报价得分怎么算", mode="lexical"
    )
    results = response["results"]
    assert results, "search should return hits"
    top = results[0]
    assert top["kind"] == "table"
    assert top["table_label"] == "表10"
    assert top["page_start"] == 2
    assert "50分" in top["snippet"]

    labels = {
        item.get("table_label") for item in results if item.get("kind") == "table"
    }
    assert "表11" in labels or any(
        item["title"].startswith("表11") for item in results
    )
    assert "表11" in {item.get("table_label") for item in response["tables"]}
    # The table heading also exists as a section; it must only be returned once.
    titles = [item["title"] for item in results]
    assert len(titles) == len(set(titles))
    assert any(item["kind"] == "factor" and "投标报价" in item["title"] for item in results)
    assert response["mode"] == "lexical"
    assert response["warnings"] == []


def test_table_heading_uses_its_own_page(settings) -> None:
    response = search_document(settings, DOC_ID, "表11 得分", mode="lexical")
    hits = [item for item in response["results"] if item.get("table_label") == "表11"]
    assert hits, response["results"]
    assert hits[0]["page_start"] == 3
    assert hits[0]["page_hint"] == "p3"


def test_construction_query_hits_section_and_factor(settings) -> None:
    response = search_document(
        settings, DOC_ID, "施工组织设计怎么写", mode="lexical"
    )
    top_three = response["results"][:3]
    kinds = {item["kind"] for item in top_three}
    assert kinds == {"section", "factor"}, top_three
    assert any("施工组织设计" in item["title"] for item in top_three)
    assert any(item["page_start"] == 4 for item in top_three)


def test_qualification_query_cites_material_page(settings) -> None:
    response = search_document(
        settings, DOC_ID, "资格审查需要什么材料", mode="lexical"
    )
    results = response["results"]
    assert results
    assert any(
        item["page_start"] == 5
        for item in results
        if item["kind"] in {"qualification", "review"}
    ), results
    assert any("资格" in item["title"] for item in results)


def test_limit_is_clamped_to_supported_range(settings) -> None:
    response = search_document(settings, DOC_ID, "投标报价", mode="lexical", limit=1)
    assert len(response["results"]) == 1
    response = search_document(
        settings, DOC_ID, "投标报价", mode="lexical", limit=999
    )
    assert len(response["results"]) <= 30


def test_unknown_mode_is_rejected(settings) -> None:
    with pytest.raises(Exception) as excinfo:
        search_document(settings, DOC_ID, "投标报价", mode="semantic")
    assert "mode" in str(excinfo.value)


def _client(settings) -> TestClient:
    runner = JobRunner(JobStore(max_jobs=2, ttl_seconds=60), lambda job, store: None)
    return TestClient(create_app(settings=settings, runner=runner))


def test_search_endpoint_returns_cited_hits(settings) -> None:
    with _client(settings) as client:
        response = client.get(
            "/api/search",
            params={
                "doc_id": DOC_ID,
                "q": "投标报价得分怎么算",
                "mode": "lexical",
                "limit": 5,
            },
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["doc_id"] == DOC_ID
    assert payload["source_name"] == SOURCE_NAME
    assert payload["project_name"] == "示例供水工程"
    assert len(payload["results"]) <= 5
    assert payload["results"][0]["table_label"] == "表10"
    assert payload["tables"][0]["page_hint"] == "p2"


def test_search_endpoint_rejects_bad_requests(settings) -> None:
    with _client(settings) as client:
        missing = client.get(
            "/api/search", params={"doc_id": "not-parsed", "q": "投标报价"}
        )
        assert missing.status_code == 404

        unsafe = client.get(
            "/api/search", params={"doc_id": "../escape", "q": "投标报价"}
        )
        assert unsafe.status_code == 422

        empty = client.get("/api/search", params={"doc_id": DOC_ID, "q": " "})
        assert empty.status_code == 200 or empty.status_code == 422

        too_long = client.get(
            "/api/search", params={"doc_id": DOC_ID, "q": "报" * 201}
        )
        assert too_long.status_code == 422

        bad_mode = client.get(
            "/api/search",
            params={"doc_id": DOC_ID, "q": "投标报价", "mode": "semantic"},
        )
        assert bad_mode.status_code == 422

        bad_limit = client.get(
            "/api/search",
            params={"doc_id": DOC_ID, "q": "投标报价", "limit": 31},
        )
        assert bad_limit.status_code == 422
