"""MinerU cloud parser (v4 batch upload flow).

The single-file ``/extract/task`` endpoint only accepts public URLs, so local
files are submitted through the signed upload-url batch endpoint:

    POST /file-urls/batch  -> batch_id + signed URLs
    PUT  <signed URL>      -> upload each PDF shard
    GET  /extract-results/batch/<batch_id> -> poll until done
    GET  full_zip_url      -> download the result archive

Pages are split in a balanced way when the document exceeds MinerU's
per-file limit, and results are merged back onto the original page numbers
and PFD bookmarks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import pymupdf

from ..config import Settings
from ..models import ImageRecord, ParseResult
from .errors import MineruError, ParseError
from .structure import (
    assign_pages_to_entries,
    clean_page_text,
    compose_sections,
    detect_toc_pages,
    html_table_to_text,
    read_toc,
    sanitize_filename,
    split_page_ranges,
    strip_page_markers,
)

DONE_STATES = {"done", "success", "succeeded", "completed"}
FAILED_STATES = {"failed", "error", "fail"}


@dataclass(slots=True)
class Shard:
    index: int
    path: Path
    upload_name: str
    page_offset: int
    page_count: int


def _text_list(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _safe_upload_name(doc_id: str, index: int) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", doc_id).strip("._-") or "document"
    return f"{token}_part{index + 1}.pdf"


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a MinerU archive while rejecting path traversal entries."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                relative = PurePosixPath(name)
                if (
                    not name
                    or relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                ):
                    continue
                target = (dest / Path(*relative.parts)).resolve()
                if dest != target and dest not in target.parents:
                    continue
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except zipfile.BadZipFile as exc:
        raise MineruError(f"下载的解析结果不是有效 ZIP：{exc}") from exc


def _find_content_list(directory: Path) -> Path | None:
    candidates = sorted(directory.rglob("*content_list*.json"))
    return candidates[0] if candidates else None


def _find_markdown(directory: Path) -> Path | None:
    full = sorted(directory.rglob("full.md"))
    if full:
        return full[0]
    candidates = sorted(
        path for path in directory.rglob("*.md") if not path.name.startswith(".")
    )
    return candidates[0] if candidates else None


def _read_content_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MineruError(f"读取 content_list 失败：{exc}") from exc
    if isinstance(data, dict):
        data = data.get("content_list") or data.get("items") or data.get("data") or []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


class MineruClient:
    """Small synchronous client for the MinerU v4 public API."""

    def __init__(self, settings: Settings) -> None:
        if not settings.mineru_configured:
            raise MineruError("MINERU_API_KEY 未配置")
        self.settings = settings

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.mineru_api_key}",
            "Content-Type": "application/json",
        }

    def _backoff(self, attempt: int) -> None:
        if attempt <= 0:
            return
        time.sleep(self.settings.mineru_retry_backoff ** attempt)

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        tolerate_404 = bool(kwargs.pop("_tolerate_404", False))
        last_error: Exception | None = None
        attempts = self.settings.mineru_max_retries + 1
        for attempt in range(attempts):
            try:
                timeout = httpx.Timeout(self.settings.mineru_timeout)
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    response = client.request(
                        method,
                        url,
                        headers=self._auth_headers,
                        **kwargs,
                    )
                if response.status_code == 404 and tolerate_404:
                    return {}
                if response.status_code >= 400:
                    body = response.text[:400]
                    raise MineruError(
                        f"MinerU 请求失败（HTTP {response.status_code}）：{body}"
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise MineruError("MinerU 返回了非 JSON 响应") from exc
                if not isinstance(payload, dict):
                    raise MineruError("MinerU 返回结构异常")
                code = payload.get("code")
                if code not in (None, 0, 200, "0", "200"):
                    message = payload.get("msg") or payload.get("message") or payload
                    raise MineruError(f"MinerU 接口错误：{message}")
                data = payload.get("data")
                return data if isinstance(data, dict) else payload
            except (httpx.HTTPError, MineruError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    self._backoff(attempt)
        raise MineruError(str(last_error) if last_error else "MinerU 请求失败")

    def create_batch(self, shards: list[Shard]) -> tuple[str, list[str]]:
        files = [
            {"name": shard.upload_name, "is_ocr": self.settings.mineru_is_ocr}
            for shard in shards
        ]
        payload = {
            "enable_formula": self.settings.mineru_enable_formula,
            "enable_table": self.settings.mineru_enable_table,
            "language": self.settings.mineru_language,
            "model_version": self.settings.mineru_model_version,
            "files": files,
        }
        data = self._request_json(
            "POST", self.settings.mineru_batch_upload_endpoint, json=payload
        )
        batch_id = data.get("batch_id") or data.get("batchId")
        urls = data.get("file_urls") or data.get("fileUrls") or []
        if not batch_id or not isinstance(urls, list) or len(urls) != len(shards):
            raise MineruError("MinerU 未返回有效的 batch_id 或上传地址")
        return str(batch_id), [str(url) for url in urls]

    def upload_shard(self, url: str, path: Path) -> None:
        last_error: Exception | None = None
        attempts = self.settings.mineru_max_retries + 1
        for attempt in range(attempts):
            try:
                timeout = httpx.Timeout(
                    self.settings.mineru_download_timeout,
                    connect=self.settings.mineru_timeout,
                )
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    response = client.put(url, content=path.read_bytes())
                if response.status_code >= 400:
                    raise MineruError(
                        f"上传分片失败（HTTP {response.status_code}）：{path.name}"
                    )
                return
            except (httpx.HTTPError, OSError, MineruError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    self._backoff(attempt)
        raise MineruError(str(last_error) if last_error else "上传分片失败")

    def wait_for_batch(self, batch_id: str, expected: int) -> list[dict[str, Any]]:
        endpoint = self.settings.mineru_batch_result_endpoint.format(batch_id=batch_id)
        polls = max(1, self.settings.mineru_max_polls)
        for _ in range(polls):
            try:
                data = self._request_json("GET", endpoint, _tolerate_404=True)
            except MineruError:
                # A result endpoint may briefly return 404 while the batch is
                # still being created; otherwise propagate on timeout.
                data = {}
            results = (
                data.get("extract_result")
                or data.get("extract_results")
                or data.get("results")
                or []
            )
            if isinstance(results, list) and results:
                states = [
                    str(item.get("state") or item.get("status") or "").lower()
                    for item in results
                    if isinstance(item, dict)
                ]
                failures = [
                    item
                    for item, state in zip(results, states)
                    if state in FAILED_STATES
                ]
                if failures:
                    details = "; ".join(
                        f"{item.get('file_name', '?')}: {item.get('err_msg', '解析失败')}"
                        for item in failures
                    )
                    raise MineruError(f"MinerU 解析失败：{details}")
                if len(results) >= expected and all(
                    state in DONE_STATES for state in states
                ):
                    return [item for item in results if isinstance(item, dict)]
            time.sleep(max(1.0, self.settings.mineru_poll_interval))
        raise MineruError("MinerU 轮询超时，请稍后重试")

    def download_zip(self, url: str, target: Path) -> None:
        if not url.lower().startswith(("http://", "https://")):
            raise MineruError("MinerU 返回了无效的结果下载地址")
        target.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        attempts = self.settings.mineru_max_retries + 1
        for attempt in range(attempts):
            try:
                timeout = httpx.Timeout(self.settings.mineru_download_timeout)
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    with client.stream("GET", url) as response:
                        if response.status_code >= 400:
                            raise MineruError(
                                f"下载解析结果失败（HTTP {response.status_code}）"
                            )
                        temporary = target.with_suffix(target.suffix + ".part")
                        with temporary.open("wb") as output:
                            for chunk in response.iter_bytes():
                                output.write(chunk)
                temporary.replace(target)
                return
            except (httpx.HTTPError, OSError, MineruError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    self._backoff(attempt)
        raise MineruError(str(last_error) if last_error else "下载解析结果失败")


def _make_shards(
    pdf_path: Path,
    page_count: int,
    raw_dir: Path,
    doc_id: str,
    settings: Settings,
) -> list[Shard]:
    max_pages = max(1, settings.mineru_max_pages)
    max_bytes = settings.mineru_max_file_mb * 1024 * 1024
    ranges = split_page_ranges(page_count, max_pages)

    if len(ranges) <= 1 and pdf_path.stat().st_size <= max_bytes:
        return [
            Shard(
                index=0,
                path=pdf_path,
                upload_name=_safe_upload_name(doc_id, 0),
                page_offset=0,
                page_count=page_count,
            )
        ]

    shards_dir = raw_dir / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    shards: list[Shard] = []
    try:
        source = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise MineruError(f"无法打开 PDF 进行分片：{exc}") from exc

    try:
        if source.needs_pass:
            raise MineruError("PDF 已加密，无法拆分上传")
        for index, (start, end) in enumerate(ranges):
            part = pymupdf.open()
            try:
                part.insert_pdf(source, from_page=start, to_page=end)
                part_path = shards_dir / f"part_{index + 1:02d}.pdf"
                part.save(str(part_path))
            finally:
                part.close()
            if part_path.stat().st_size > max_bytes:
                raise MineruError(
                    f"分片 {part_path.name} 超过 MinerU 单文件大小限制 "
                    f"({settings.mineru_max_file_mb} MB)"
                )
            shards.append(
                Shard(
                    index=index,
                    path=part_path,
                    upload_name=_safe_upload_name(doc_id, index),
                    page_offset=start,
                    page_count=end - start + 1,
                )
            )
    finally:
        source.close()
    return shards


def _copy_cloud_image(
    source: Path,
    shard: Shard,
    page_no: int,
    images_dir: Path,
    warnings: list[str],
) -> tuple[str, ImageRecord] | None:
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
        source = source.resolve()
        if not source.is_file():
            return None
        filename = (
            f"p{page_no:03d}_s{shard.index + 1}_{sanitize_filename(source.name)}"
        )
        target = images_dir / filename
        counter = 1
        while target.exists() and target.read_bytes() != source.read_bytes():
            counter += 1
            target = images_dir / (
                f"p{page_no:03d}_s{shard.index + 1}_{counter}_{sanitize_filename(source.name)}"
            )
        if not target.exists():
            shutil.copyfile(source, target)
        ext = target.suffix.lower().lstrip(".") or "png"
        return filename, ImageRecord(
            file=filename,
            page=page_no,
            xref=0,
            ext=ext,
        )
    except OSError as exc:
        warnings.append(f"云端图片复制失败：{source.name}（{exc}）")
        return None


def _content_list_to_pages(
    content_list: list[dict[str, Any]],
    shard: Shard,
    shard_dir: Path,
    images_dir: Path,
    image_link_prefix: str,
    page_count: int,
    warnings: list[str],
) -> tuple[dict[int, str], list[ImageRecord]]:
    page_blocks: dict[int, list[str]] = {}
    images: list[ImageRecord] = []

    for block in content_list:
        try:
            page_idx = int(block.get("page_idx"))
        except (TypeError, ValueError):
            warning_key = "缺少 page_idx 的版面块"
            if warning_key not in warnings:
                warnings.append(warning_key)
            continue
        page_no = shard.page_offset + page_idx + 1
        if page_no < 1 or page_no > page_count:
            continue

        block_type = str(block.get("type") or "").lower()
        text = ""
        if block_type == "table":
            captions = _text_list(block.get("table_caption"))
            body = block.get("table_body") or block.get("text") or ""
            if isinstance(body, list):
                body = "\n".join(str(item) for item in body)
            body = html_table_to_text(str(body)).strip()
            text = "\n\n".join(part for part in (captions, body) if part)
        elif block_type == "image":
            captions = _text_list(block.get("img_caption"))
            img_path = (
                block.get("img_path")
                or block.get("image_path")
                or block.get("path")
            )
            link = ""
            if isinstance(img_path, str) and img_path.strip():
                relative = PurePosixPath(img_path.replace("\\", "/").lstrip("/"))
                if ".." in relative.parts:
                    warnings.append(f"忽略越界的图片路径：{img_path}")
                else:
                    copied = _copy_cloud_image(
                        shard_dir / Path(*relative.parts),
                        shard,
                        page_no,
                        images_dir,
                        warnings,
                    )
                    if copied:
                        filename, record = copied
                        images.append(record)
                        link = f"{image_link_prefix.rstrip('/')}/{filename}"
            if link:
                alt = captions.replace("]", "）").replace("[", "（")
                text = f"![{alt}]({link})"
            else:
                text = captions
        elif block_type == "equation":
            text = str(
                block.get("text")
                or block.get("latex")
                or block.get("equation")
                or ""
            ).strip()
        else:
            value = block.get("text")
            if isinstance(value, list):
                text = "\n".join(str(item) for item in value).strip()
            elif value is not None:
                text = str(value).strip()

        if text:
            page_blocks.setdefault(page_no, []).append(text)

    pages = {
        page_no: "\n\n".join(parts).strip()
        for page_no, parts in page_blocks.items()
        if "\n\n".join(parts).strip()
    }
    return pages, images


def _cloud_markdown_fallback(
    shard_dirs: list[Path], warnings: list[str]
) -> str:
    parts: list[str] = []
    for directory in shard_dirs:
        markdown_path = _find_markdown(directory)
        if markdown_path is None:
            continue
        try:
            parts.append(markdown_path.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"读取云端 Markdown 失败：{markdown_path.name}（{exc}）")
    warnings.append(
        "无法从版面结果还原页码，已按分片顺序拼接，章节边界可能偏移"
    )
    return "\n\n".join(part for part in parts if part).strip()


def parse_cloud(
    pdf_path: Path,
    settings: Settings,
    doc_id: str,
    raw_dir: Path,
    images_dir: Path,
    artifacts_dir: Path,
) -> ParseResult:
    """Parse the PDF with MinerU, merging shards back onto original pages."""
    started = time.perf_counter()
    client = MineruClient(settings)
    warnings: list[str] = []

    try:
        source = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise MineruError(f"无法打开 PDF：{exc}") from exc
    try:
        if source.needs_pass:
            raise MineruError("PDF 已加密，无法解析")
        page_count = source.page_count
        if page_count <= 0:
            raise MineruError("PDF 页数为 0")
        toc = read_toc(source, page_count)
        page_texts = {
            page_no: clean_page_text(page.get_text("text", sort=True))
            for page_no, page in enumerate(source, start=1)
        }
    finally:
        source.close()

    toc_pages = detect_toc_pages(page_texts)
    shards = _make_shards(pdf_path, page_count, raw_dir, doc_id, settings)
    batch_id, urls = client.create_batch(shards)
    for shard, url in zip(shards, urls):
        client.upload_shard(url, shard.path)
    results = client.wait_for_batch(batch_id, expected=len(shards))

    by_name = {
        Path(str(item.get("file_name") or item.get("name") or "")).name: item
        for item in results
    }
    ordered_results: list[dict[str, Any] | None] = []
    for index, shard in enumerate(shards):
        item = by_name.get(shard.upload_name)
        if item is None and index < len(results):
            item = results[index]
        ordered_results.append(item)

    mineru_dir = raw_dir / "mineru"
    mineru_dir.mkdir(parents=True, exist_ok=True)
    shard_dirs: list[Path] = []
    content_lists: list[list[dict[str, Any]]] = []
    used_content_list = True

    try:
        image_link_prefix = os.path.relpath(images_dir, start=artifacts_dir).replace(
            os.sep, "/"
        )
    except ValueError:
        image_link_prefix = "../../images/" + doc_id

    for shard, item in zip(shards, ordered_results):
        if not item:
            used_content_list = False
            continue
        zip_url = item.get("full_zip_url") or item.get("zip_url")
        if not zip_url:
            used_content_list = False
            continue
        zip_path = mineru_dir / f"part_{shard.index + 1:02d}.zip"
        shard_dir = mineru_dir / f"part_{shard.index + 1:02d}"
        if shard_dir.exists():
            shutil.rmtree(shard_dir, ignore_errors=True)
        try:
            client.download_zip(str(zip_url), zip_path)
            _extract_zip(zip_path, shard_dir)
        except MineruError:
            raise
        shard_dirs.append(shard_dir)
        content_list_path = _find_content_list(shard_dir)
        if content_list_path is None:
            used_content_list = False
            content_lists.append([])
            continue
        content_lists.append(_read_content_list(content_list_path))

    pages: dict[int, str] = {}
    images: list[ImageRecord] = []
    if used_content_list and content_lists and all(content_lists):
        for shard, shard_dir, content_list in zip(shards, shard_dirs, content_lists):
            shard_pages, shard_images = _content_list_to_pages(
                content_list,
                shard,
                shard_dir,
                images_dir,
                image_link_prefix,
                page_count,
                warnings,
            )
            pages.update(shard_pages)
            images.extend(shard_images)

    if pages:
        assigned, uncovered = assign_pages_to_entries(toc, pages)
        markdown, sections = compose_sections(toc, assigned, uncovered)
        structure_source = "toc" if toc else "page_order"
        degraded = False
    else:
        markdown = _cloud_markdown_fallback(shard_dirs, warnings)
        if not markdown:
            raise MineruError("云端结果中未找到可用的 Markdown 或 content_list")
        sections = []
        structure_source = "markdown_order"
        degraded = True
        used_content_list = False

    if not toc:
        warnings.append("PDF 无书签，章节结构无法还原")
    if not used_content_list and structure_source != "markdown_order":
        warnings.append(
            "无法从版面结果还原页码，已按分片顺序拼接，章节边界可能偏移"
        )

    elapsed = time.perf_counter() - started
    parser_meta = {
        "structure_source": structure_source,
        "degraded": degraded,
        "cloud": True,
        "batch_id": batch_id,
        "shard_count": len(shards),
        "content_list_used": used_content_list,
        "toc_entries": len(toc),
        "toc_pages": toc_pages,
        "model_version": settings.mineru_model_version,
        "image_count": len(images),
        "elapsed_seconds": round(elapsed, 3),
    }
    images.sort(key=lambda item: (item.page, item.file))
    return ParseResult(
        parser="mineru",
        page_count=page_count,
        toc=toc,
        sections=sections,
        markdown=markdown,
        images=images,
        warnings=warnings,
        toc_pages=toc_pages,
        parser_meta=parser_meta,
    )
