"""CLI error-handling tests that never touch external services."""

from __future__ import annotations

from bidding.cli import main


def test_extract_rejects_illegal_doc_id(capsys) -> None:
    assert main(["extract", "../outside"]) == 1
    captured = capsys.readouterr()
    assert "非法的 doc_id" in captured.err


def test_parse_reports_missing_pdf(capsys) -> None:
    assert main(["parse", "upload/files/does-not-exist.pdf"]) == 1
    captured = capsys.readouterr()
    assert "错误：" in captured.err
