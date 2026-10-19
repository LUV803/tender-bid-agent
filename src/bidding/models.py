"""Shared data structures for parsing, extraction and retrieval."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def now_iso() -> str:
    """Local ISO-8601 timestamp with timezone information."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class TocEntry:
    level: int
    title: str
    page: int
    page_end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Section:
    index: int
    level: int
    number: str
    title: str
    path: list[str]
    page_start: int
    page_end: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_summary(self) -> dict[str, Any]:
        """Manifest representation: metadata only, never the full body text."""
        summary = asdict(self)
        summary.pop("text", None)
        summary["char_count"] = len(self.text)
        return summary

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Section":
        return cls(
            index=int(data.get("index", 0)),
            level=int(data.get("level", 1)),
            number=str(data.get("number", "")),
            title=str(data.get("title", "")),
            path=[str(item) for item in (data.get("path") or [])],
            page_start=int(data.get("page_start", 0)),
            page_end=int(data.get("page_end", 0)),
            text=str(data.get("text", "")),
        )


@dataclass(slots=True)
class ImageRecord:
    file: str
    page: int
    xref: int = 0
    width: int = 0
    height: int = 0
    ext: str = "png"
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParseResult:
    """Output of a local or cloud parser, before artifacts are written."""

    parser: str
    page_count: int
    toc: list[TocEntry]
    sections: list[Section]
    markdown: str
    images: list[ImageRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    toc_pages: list[int] = field(default_factory=list)
    parser_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Chunk:
    """A retrieval unit ready to be embedded."""

    id: str
    text: str
    metadata: dict[str, Any]
