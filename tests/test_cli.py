"""CLI error-handling tests that never touch external services."""

from __future__ import annotations

import pytest

from bidding.cli import main


def test_extract_rejects_illegal_doc_id(capsys) -> None:
    assert main(["extract", "../outside"]) == 1
    captured = capsys.readouterr()
    assert "非法的 doc_id" in captured.err


def test_parse_reports_missing_pdf(capsys) -> None:
    assert main(["parse", "upload/files/does-not-exist.pdf"]) == 1
    captured = capsys.readouterr()
    assert "错误：" in captured.err


def test_extract_success_returns_zero(monkeypatch, capsys) -> None:
    payload = {
        "project_name": "测试项目",
        "tender_number": "T-001",
        "budget_amount_text": "100 万元",
        "bid_deadline": "2024-01-01",
        "evaluation_method": "综合评估法",
        "evaluation_factors": [],
        "preliminary_review": [],
        "qualifications": [],
        "bid_format": {"items": []},
        "key_dates": [],
        "ungrounded_fields": [],
        "extraction_meta": {
            "processed_units": 1,
            "unit_count": 1,
            "cached_units": 0,
            "failed_units": [],
            "partial": False,
            "elapsed_seconds": 0.1,
            "warnings": [],
        },
    }
    monkeypatch.setattr("bidding.cli.run_extract", lambda *args, **kwargs: payload)

    assert main(["extract", "doc-1"]) == 0
    output = capsys.readouterr().out
    assert "测试项目" in output
    assert "units         : 1/1" in output


def test_missing_subcommand_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2
