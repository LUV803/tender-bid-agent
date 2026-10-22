"""Offline tests for the retrieval evaluation harness and demo gold set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bidding.evaluation import (
    EvaluationError,
    Relevance,
    Thresholds,
    check_thresholds,
    evaluate,
    format_report,
    load_gold,
)
from tests.support import make_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_PARSED = PROJECT_ROOT / "eval" / "demo_parsed"
DEMO_GOLD = PROJECT_ROOT / "eval" / "demo_queries.json"


@pytest.fixture()
def demo_settings(tmp_path: Path):
    return make_settings(
        tmp_path,
        parsed_dir=DEMO_PARSED,
        minio_enabled=False,
    )


def test_load_gold_reads_demo_queries() -> None:
    gold = load_gold(DEMO_GOLD)
    assert gold.doc_id == "demo-tender"
    assert len(gold.queries) == 8
    assert gold.queries[0].id == "price-score-table"
    assert gold.queries[0].relevant[0].table_label == "表10"


def test_demo_evaluation_meets_ci_thresholds(demo_settings) -> None:
    result = evaluate(
        demo_settings,
        load_gold(DEMO_GOLD),
        mode="lexical",
        limit=10,
        k=5,
    )
    summary = result["summary"]
    assert summary["queries"] == 8
    assert summary["hit_at_3"] == pytest.approx(1.0)
    assert summary["hit_at_k"] == pytest.approx(1.0)
    assert summary["recall_at_k"] >= 0.8
    assert summary["mrr"] >= 0.7
    assert summary["empty_results"] == 0
    assert summary["vector_used"] is False

    rows = {row["id"]: row for row in result["queries"]}
    assert rows["price-score-table"]["rank"] == 1
    assert rows["price-score-table"]["top_table_label"] == "表10"
    assert rows["table11-score"]["top_table_label"] == "表11"
    assert rows["qualification-section"]["matched"] >= 1


def test_relevance_matches_constraints() -> None:
    page_only = Relevance(page=3)
    assert page_only.matches({"kind": "table", "page_start": 2, "page_end": 3})
    assert not page_only.matches({"kind": "table", "page_start": 4, "page_end": 5})
    assert not page_only.matches({"kind": "table"})

    labelled = Relevance(kind="table", table_label="表 10", page=2)
    assert labelled.matches(
        {"kind": "table", "table_label": "表10", "page_start": 2, "page_end": 2}
    )
    assert not labelled.matches(
        {"kind": "section", "table_label": "表10", "page_start": 2, "page_end": 2}
    )
    assert not labelled.matches(
        {"kind": "table", "table_label": "表11", "page_start": 2, "page_end": 2}
    )

    titled = Relevance(title_contains="施工组织 设计")
    assert titled.matches({"title": "六、施工组织设计"})
    assert not titled.matches({"title": "资格审查资料"})


def test_check_thresholds_reports_failures() -> None:
    result = {
        "k": 5,
        "summary": {
            "hit_at_1": 0.5,
            "hit_at_3": 0.9,
            "recall_at_k": 0.7,
            "mrr": 0.8,
        },
    }
    assert check_thresholds(result, Thresholds(hit_at_1=0.4, mrr=0.7)) == []
    failures = check_thresholds(
        result,
        Thresholds(hit_at_1=0.6, hit_at_3=0.95, recall_at_k=0.8, mrr=0.9),
    )
    assert len(failures) == 4
    assert all("低于要求" in failure for failure in failures)


def test_format_report_contains_metrics(demo_settings) -> None:
    result = evaluate(
        demo_settings,
        load_gold(DEMO_GOLD),
        mode="lexical",
        limit=10,
        k=5,
    )
    report = format_report(result)
    assert "Retrieval evaluation" in report
    assert "Hit@1" in report
    assert "Recall@5" in report
    assert "price-score-table" in report


def _write_gold(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_gold_rejects_invalid_entries(tmp_path: Path) -> None:
    path = _write_gold(tmp_path, {"doc_id": "demo", "queries": []})
    with pytest.raises(EvaluationError):
        load_gold(path)

    path = _write_gold(
        tmp_path,
        {
            "doc_id": "demo",
            "queries": [{"id": "q1", "query": "问题", "relevant": [{}]}],
        },
    )
    with pytest.raises(EvaluationError):
        load_gold(path)

    path = _write_gold(
        tmp_path,
        {
            "doc_id": "demo",
            "queries": [
                {
                    "id": "q1",
                    "query": "问题",
                    "relevant": [{"page": 1, "unexpected": True}],
                }
            ],
        },
    )
    with pytest.raises(EvaluationError):
        load_gold(path)

    path = _write_gold(
        tmp_path,
        {
            "doc_id": "demo",
            "queries": [
                {"id": "q1", "query": "问题", "relevant": [{"page": 1}]},
                {"id": "q1", "query": "问题二", "relevant": [{"page": 2}]},
            ],
        },
    )
    with pytest.raises(EvaluationError):
        load_gold(path)
