"""PDF structure restoration and text helpers.

The functions here implement the deterministic parts of the design notes:
bookmark ranges (A1), page-to-bookmark assignment (A2), Markdown assembly
(A3), table-of-contents page detection (A4), balanced sharding (A5), local
text cleaning (A7) and HTML-table flattening (A8).
"""

from __future__ import annotations

import html
import math
import re
from html.parser import HTMLParser
from typing import Any, Iterable

from ..models import Section, TocEntry

PAGE_MARKER_RE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")
TOC_LEADER_RE = re.compile(r"[.．·]{3,}\s*\d+\s*$")
TOC_LONG_DOT_RE = re.compile(r"[.．]{6,}")
PAGE_NUMBER_RE = re.compile(r"^\s*[-—–]?\s*\d{1,4}\s*[-—–]?\s*$")
FINGERPRINT_RE = re.compile(r"\s*[0-9a-f]{20,}-\d{10,}\s*", re.IGNORECASE)
CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百]+章")

SECTION_NUMBER_PATTERNS = (
    re.compile(r"^第[一二三四五六七八九十百]+[章卷节]"),
    re.compile(r"^附件[一二三四五六七八九十\d]+"),
    re.compile(r"^表\d+"),
    re.compile(r"^[一二三四五六七八九十]+、"),
    re.compile(r"^\d+(?:\.\d+)*[.、\s]"),
    re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]"),
)

_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def clean_page_text(text: str) -> str:
    """Remove fingerprint lines, standalone page numbers and blank noise."""
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        line = FINGERPRINT_RE.sub(" ", line).strip()
        if not line or PAGE_NUMBER_RE.match(line):
            continue
        lines.append(line)

    cleaned: list[str] = []
    for line in lines:
        if line == "" and (not cleaned or cleaned[-1] == ""):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def read_toc(doc: Any, total_pages: int) -> list[TocEntry]:
    """Read PDF bookmarks and derive inclusive page ranges (A1)."""
    if total_pages <= 0:
        return []
    raw: list[tuple[int, str, int]] = []
    try:
        for item in doc.get_toc(simple=True) or []:
            if len(item) < 3:
                continue
            level, title, page = item[0], item[1], item[2]
            try:
                page_no = int(page)
            except (TypeError, ValueError):
                continue
            if page_no < 1:
                page_no = 1
            if page_no > total_pages:
                page_no = total_pages
            raw.append((int(level), str(title).strip(), page_no))
    except Exception:
        return []

    entries: list[TocEntry] = []
    for index, (level, title, page) in enumerate(raw):
        page_end = total_pages
        for later_level, _later_title, later_page in raw[index + 1 :]:
            if later_level <= level:
                page_end = max(page, later_page - 1)
                break
        entries.append(TocEntry(level=level, title=title, page=page, page_end=page_end))
    return entries


def is_toc_page(text: str) -> bool:
    """Detect a table-of-contents page by dot leaders (A4)."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    leaders = sum(
        1
        for line in lines
        if TOC_LEADER_RE.search(line) or TOC_LONG_DOT_RE.search(line)
    )
    return leaders >= 2 and leaders / len(lines) >= 0.3


def detect_toc_pages(pages: dict[int, str]) -> list[int]:
    return [page_no for page_no in sorted(pages) if is_toc_page(pages[page_no])]


def assign_pages_to_entries(
    entries: list[TocEntry], pages: dict[int, str]
) -> tuple[dict[int, list[str]], list[str]]:
    """Assign every non-empty page to its deepest bookmark (A2)."""
    assigned: dict[int, list[str]] = {}
    uncovered: list[str] = []
    for page_no in sorted(pages):
        text = pages[page_no]
        if not text.strip():
            continue
        block = f"<!-- page: {page_no} -->\n{text}"
        covering = [
            index
            for index, entry in enumerate(entries)
            if entry.page <= page_no <= entry.page_end
        ]
        if covering:
            best = max(covering, key=lambda index: (entries[index].level, index))
            assigned.setdefault(best, []).append(block)
        else:
            uncovered.append(block)
    return assigned, uncovered


def section_number(title: str) -> str:
    for pattern in SECTION_NUMBER_PATTERNS:
        match = pattern.match(title.strip())
        if match:
            return match.group(0).strip()
    return ""


def ancestor_titles(entries: list[TocEntry], index: int) -> list[str]:
    """Return the ancestor title chain from root to parent."""
    chain: list[str] = []
    current = entries[index]
    target = current.level - 1
    cursor = index - 1
    while cursor >= 0 and target >= 1:
        candidate = entries[cursor]
        if candidate.level == target:
            chain.append(candidate.title)
            target -= 1
        cursor -= 1
    chain.reverse()
    return chain


def compose_sections(
    entries: list[TocEntry],
    assigned: dict[int, list[str]],
    uncovered: list[str],
) -> tuple[str, list[Section]]:
    """Build page-annotated Markdown and per-bookmark sections (A3)."""
    if not entries:
        return "\n\n".join(uncovered).strip(), []

    parts: list[str] = []
    if uncovered:
        parts.extend(uncovered)

    sections: list[Section] = []
    for index, entry in enumerate(entries):
        body = "\n\n".join(assigned.get(index, []))
        heading = "#" * min(entry.level + 1, 6) + " " + entry.title
        parts.append(f"{heading}\n\n{body}".rstrip())
        sections.append(
            Section(
                index=index,
                level=entry.level,
                number=section_number(entry.title),
                title=entry.title,
                path=ancestor_titles(entries, index),
                page_start=entry.page,
                page_end=entry.page_end,
                text=body,
            )
        )
    return "\n\n".join(part for part in parts if part).strip(), sections


def detect_doc_type(markdown: str, toc_count: int) -> str:
    """Heuristic document type classification (A2.6)."""
    features = ("评标办法", "投标人须知", "投标文件格式")
    hits = sum(1 for feature in features if feature in markdown)
    if hits >= 2:
        return "tender_file"
    if "招标公告" in markdown:
        return "announcement"
    if toc_count > 20:
        return "tender_file"
    return "unknown"


class _TableCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] = []
        self.in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            self.in_cell = True
            self.cell = []
        elif tag == "br" and self.in_cell:
            self.cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self.in_cell:
            value = "".join(self.cell).strip()
            self.row.append(re.sub(r"\s+", " ", value))
            self.in_cell = False
            self.cell = []
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell.append(data)

    def close(self) -> None:
        super().close()
        if self.row:
            self.rows.append(self.row)
            self.row = []


def _flatten_table(fragment: str) -> str:
    collector = _TableCollector()
    try:
        collector.feed(fragment)
        collector.close()
    except Exception:
        return fragment
    lines = [" | ".join(cell for cell in row) for row in collector.rows if any(row)]
    if not lines:
        return fragment
    return "\n".join(lines)


def html_table_to_text(text: str) -> str:
    """Flatten HTML tables into ``cell | cell`` lines (A8)."""
    if "<table" not in text.lower():
        return text

    def replace(match: re.Match[str]) -> str:
        return _flatten_table(match.group(0))

    flattened = re.sub(
        r"<table\b[^>]*>.*?</table>",
        replace,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(flattened)


def strip_page_markers(text: str) -> tuple[str, list[int]]:
    """Remove page markers and return their numbers in order."""
    pages = [int(value) for value in PAGE_MARKER_RE.findall(text)]
    return PAGE_MARKER_RE.sub("", text).strip(), pages


def split_by_blank_lines(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return [part.strip() for part in parts if part.strip()]


def split_page_ranges(page_count: int, max_pages: int) -> list[tuple[int, int]]:
    """Return balanced 0-based inclusive page ranges (A5)."""
    if page_count <= 0:
        return []
    if max_pages <= 0 or page_count <= max_pages:
        return [(0, page_count - 1)]
    parts = math.ceil(page_count / max_pages)
    base, remainder = divmod(page_count, parts)
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(parts):
        size = base + (1 if index < remainder else 0)
        ranges.append((start, start + size - 1))
        start += size
    return ranges


def sanitize_filename(name: str, fallback: str = "document") -> str:
    """Make a user-supplied name safe for a Windows file name."""
    cleaned = _INVALID_FILENAME_RE.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return fallback
    stem = cleaned.split(".")[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:120]


def sanitize_doc_id(name: str) -> str:
    """Keep a readable doc_id that is also safe in a directory name."""
    cleaned = sanitize_filename(name, fallback="")
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.strip("._-")
    return cleaned[:64]


def is_chapter_title(title: str) -> bool:
    return bool(CHAPTER_RE.match(title.strip()))


def normalize_ws(value: str) -> str:
    """Normalize text for grounding checks (whitespace and table pipes)."""
    return re.sub(r"[\s|｜]+", "", value or "")


def chapter_indices(entries: list[TocEntry]) -> list[int]:
    return [index for index, entry in enumerate(entries) if is_chapter_title(entry.title)]


def subtree_range(entries: list[TocEntry], index: int) -> tuple[int, int]:
    start = index
    end = len(entries)
    level = entries[index].level
    for cursor in range(index + 1, len(entries)):
        if entries[cursor].level <= level:
            end = cursor
            break
    return start, end


def subtree_text(sections: list[Section], entries: list[TocEntry], index: int) -> str:
    start, end = subtree_range(entries, index)
    return "\n\n".join(
        sections[cursor].text for cursor in range(start, end) if sections[cursor].text
    )


def iter_toc_titles(entries: Iterable[TocEntry]) -> Iterable[str]:
    for entry in entries:
        yield entry.title
