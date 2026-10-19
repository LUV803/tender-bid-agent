"""Parse orchestration: source intake, parser fallback and artifact writing."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..models import ParseResult, Section, TocEntry, now_iso
from .errors import MineruError, ParseError
from .local import parse_local
from .mineru import parse_cloud
from .structure import detect_doc_type, sanitize_doc_id, sanitize_filename

PARSERS = {"auto", "cloud", "local"}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def manifest_path(doc_id: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.parsed_dir / doc_id / "manifest.json"


def load_manifest(doc_id: str, settings: Settings | None = None) -> dict[str, Any]:
    path = manifest_path(doc_id, settings)
    if not path.is_file():
        raise ParseError(f"文档 {doc_id} 尚未解析：找不到 {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"读取 manifest 失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ParseError("manifest 结构异常")
    return data


def load_sections(
    doc_id: str, settings: Settings | None = None
) -> tuple[list[Section], list[TocEntry], dict[str, Any]]:
    settings = settings or get_settings()
    manifest = load_manifest(doc_id, settings)
    raw_path = settings.parsed_dir / doc_id / "raw" / "sections.json"
    if not raw_path.is_file():
        raise ParseError(
            f"文档 {doc_id} 缺少章节正文缓存 {raw_path}，请重新解析"
        )
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"读取章节正文失败：{exc}") from exc
    sections = [
        Section.from_dict(item)
        for item in (data.get("sections") or [])
        if isinstance(item, dict)
    ]
    toc = [
        TocEntry(
            level=int(item.get("level", 1)),
            title=str(item.get("title", "")),
            page=int(item.get("page", 1)),
            page_end=int(item.get("page_end", item.get("page", 1))),
        )
        for item in (data.get("toc") or [])
        if isinstance(item, dict)
    ]
    return sections, toc, manifest


def _remove_tree(path: Path, root: Path) -> None:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise ParseError(f"拒绝清理越界目录：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved, ignore_errors=True)


def _clear_directory(path: Path) -> None:
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _invalidate_extraction(artifacts_dir: Path) -> None:
    extraction = artifacts_dir / "extraction.json"
    if extraction.exists():
        extraction.unlink()
    parts = artifacts_dir / "extraction_parts"
    if parts.exists():
        shutil.rmtree(parts, ignore_errors=True)


def _prepare_source(
    pdf_path: Path, doc_id: str, settings: Settings
) -> tuple[Path, str]:
    source = pdf_path.resolve()
    if settings.files_dir.resolve() in source.parents:
        return source, source.name
    target = settings.files_dir / f"{doc_id}_{sanitize_filename(source.name)}"
    if target.exists() and sha256_file(target) == sha256_file(source):
        return target, source.name
    shutil.copy2(source, target)
    return target, source.name


def _cache_matches(manifest: dict[str, Any], parser: str, digest: str) -> bool:
    if manifest.get("source_sha256") != digest:
        return False
    if parser == "auto":
        return True
    if parser == "local":
        return manifest.get("parser") == "pymupdf"
    if parser == "cloud":
        return manifest.get("parser") == "mineru"
    return False


def _run_parser(
    source: Path,
    parser: str,
    settings: Settings,
    doc_id: str,
    artifacts_dir: Path,
    images_dir: Path,
    raw_dir: Path,
    warnings: list[str],
    strict_parser: bool,
) -> tuple[ParseResult, dict[str, Any]]:
    requested = parser.lower()
    if requested not in PARSERS:
        raise ValueError(f"未知解析方式：{parser}")

    use_cloud = requested == "cloud" or (
        requested == "auto" and settings.mineru_mode == "cloud"
    )
    fallback_reason = ""
    cloud_attempted = False

    if use_cloud and not settings.mineru_configured:
        message = "跳过云端解析（no_api_key）"
        if strict_parser or requested == "cloud":
            raise ParseError(f"{message}，MINERU_API_KEY 未配置")
        warnings.append(message)
        fallback_reason = "no_api_key"
        use_cloud = False

    if use_cloud and settings.mineru_mode == "local" and requested == "auto":
        fallback_reason = "mode_local"
        use_cloud = False

    if use_cloud:
        cloud_attempted = True
        try:
            result = parse_cloud(
                source,
                settings,
                doc_id,
                raw_dir,
                images_dir,
                artifacts_dir,
            )
            result.parser_meta["cloud_attempted"] = True
            result.parser_meta["fallback_reason"] = ""
            return result, {
                "requested_parser": requested,
                "mineru_mode": settings.mineru_mode,
                "cloud_attempted": True,
                "fallback_reason": "",
            }
        except Exception as exc:
            if strict_parser or requested == "cloud":
                raise ParseError(f"云端解析失败：{exc}") from exc
            reason = str(exc)
            warnings.append(f"云端解析失败，回退本地解析：{reason}")
            fallback_reason = reason
            use_cloud = False

    result = parse_local(source, images_dir)
    result.parser_meta["cloud_attempted"] = cloud_attempted
    result.parser_meta["fallback_reason"] = fallback_reason
    result.parser_meta["requested_parser"] = requested
    result.parser_meta["mineru_mode"] = settings.mineru_mode
    return result, {
        "requested_parser": requested,
        "mineru_mode": settings.mineru_mode,
        "cloud_attempted": cloud_attempted,
        "fallback_reason": fallback_reason,
    }


def parse_document(
    pdf_path: str | Path,
    parser: str = "auto",
    *,
    strict_parser: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Parse a PDF and write ``content.md``, ``manifest.json`` and images.

    The function is the reusable entry point for both the CLI and a future API.
    """
    settings = get_settings()
    settings.ensure_directories()

    path = Path(pdf_path).expanduser()
    if not path.is_absolute():
        path = (settings.project_root / path).resolve()
    if not path.is_file():
        raise ParseError(f"PDF 不存在：{path}")
    if path.suffix.lower() != ".pdf":
        raise ParseError(f"仅支持 PDF 文件：{path.name}")

    digest = sha256_file(path)
    doc_id = sanitize_doc_id(path.stem) or digest[:12]
    source, source_name = _prepare_source(path, doc_id, settings)
    source_bytes = source.stat().st_size

    artifacts_dir = settings.parsed_dir / doc_id
    images_dir = settings.images_dir / doc_id
    raw_dir = artifacts_dir / "raw"
    mpath = artifacts_dir / "manifest.json"

    if not force and mpath.is_file():
        try:
            cached = load_manifest(doc_id, settings)
            if _cache_matches(cached, parser.lower(), digest):
                return cached
        except ParseError:
            pass

    warnings: list[str] = []
    _remove_tree(artifacts_dir, settings.parsed_dir)
    _remove_tree(images_dir, settings.images_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    result, attempt_meta = _run_parser(
        source=source,
        parser=parser,
        settings=settings,
        doc_id=doc_id,
        artifacts_dir=artifacts_dir,
        images_dir=images_dir,
        raw_dir=raw_dir,
        warnings=warnings,
        strict_parser=strict_parser,
    )

    warnings = _unique([*result.warnings, *warnings])
    doc_type = detect_doc_type(result.markdown, len(result.toc))

    manifest_images: list[dict[str, Any]] = []
    for image in result.images:
        image_path = images_dir / image.file
        if image_path.is_file():
            image.path = settings.relative(image_path)
            manifest_images.append(image.to_dict())

    content_path = artifacts_dir / "content.md"
    _write_text(content_path, result.markdown)
    _write_json(
        raw_dir / "sections.json",
        {
            "doc_id": doc_id,
            "parser": result.parser,
            "toc": [entry.to_dict() for entry in result.toc],
            "sections": [section.to_dict() for section in result.sections],
            "toc_pages": result.toc_pages,
        },
    )

    parser_meta = {
        **result.parser_meta,
        **attempt_meta,
    }
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "doc_id": doc_id,
        "doc_type": doc_type,
        "parser": result.parser,
        "source_name": source_name,
        "source_path": settings.relative(source),
        "source_sha256": digest,
        "source_bytes": source_bytes,
        "pages": result.page_count,
        "toc": [entry.to_dict() for entry in result.toc],
        "sections": [section.to_summary() for section in result.sections],
        "images": manifest_images,
        "markdown": "",
        "warnings": warnings,
        "toc_pages": result.toc_pages,
        "artifacts_dir": settings.relative(artifacts_dir),
        "markdown_path": settings.relative(content_path),
        "images_dir": settings.relative(images_dir),
        "parsed_at": now_iso(),
        "parser_meta": parser_meta,
    }
    _write_json(mpath, manifest)
    return manifest
