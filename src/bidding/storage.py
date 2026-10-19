"""Durable MinIO mirror for uploads and pipeline artifacts.

``upload/`` stays the local working cache: the parsers, extractor, indexer and
writers keep reading and writing ordinary files.  Every completed stage is
also copied to MinIO, and missing local artifacts are restored on demand.
That keeps the existing offline pipeline intact while making documents and bid
outputs durable and shareable across machines.

Object layout inside ``MINIO_BUCKET``::

    documents/<doc_id>/source/<filename>
    documents/<doc_id>/parsed/...
    documents/<doc_id>/images/...
    documents/<doc_id>/bids/...

MinIO is optional.  Mirroring is best-effort by default: a missing or
misconfigured server logs a warning but never fails parsing, extraction,
indexing or bid writing.  ``bidding doctor`` reports connectivity.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

DOCUMENT_PREFIX = "documents"
COPY_CHUNK_SIZE = 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
# After a connection failure, skip further remote calls briefly.  On Windows a
# refused localhost connect can take ~2s, and a listing makes several calls.
_RETRY_AFTER_SECONDS = 60.0
_unavailable_until = 0.0
_unavailable_lock = threading.Lock()
_CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class StorageError(RuntimeError):
    """MinIO is disabled, misconfigured, unreachable or rejected a request."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size: int
    last_modified: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "size": self.size,
            "last_modified": self.last_modified,
        }


_WINDOWS_INVALID = set('<>:"|?*')


def _safe_component(value: str) -> bool:
    """True for one path component that is safe on every platform."""
    return (
        bool(value)
        and value not in {".", ".."}
        and value.strip(".") != ""
        and not value.endswith((" ", "."))
        and not any(
            char in _WINDOWS_INVALID or ord(char) < 32 for char in value
        )
    )


def _validate_doc_id(doc_id: str) -> str:
    if not isinstance(doc_id, str) or not _safe_component(doc_id):
        raise ValueError(f"非法的 doc_id：{doc_id!r}")
    return doc_id


def _relative_key(value: str) -> str:
    normalized = str(value).replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or not all(_safe_component(part) for part in parts):
        raise ValueError(f"非法的对象路径：{value!r}")
    return "/".join(parts)


def document_prefix(doc_id: str) -> str:
    return f"{DOCUMENT_PREFIX}/{_validate_doc_id(doc_id)}/"


def parsed_prefix(doc_id: str) -> str:
    return f"{document_prefix(doc_id)}parsed/"


def images_prefix(doc_id: str) -> str:
    return f"{document_prefix(doc_id)}images/"


def bids_prefix(doc_id: str) -> str:
    return f"{document_prefix(doc_id)}bids/"


def source_prefix(doc_id: str) -> str:
    return f"{document_prefix(doc_id)}source/"


def parsed_key(doc_id: str, relative: str) -> str:
    return f"{parsed_prefix(doc_id)}{_relative_key(relative)}"


def _plain_filename(filename: str) -> str:
    if (
        not isinstance(filename, str)
        or "/" in filename
        or "\\" in filename
        or not _safe_component(filename)
    ):
        raise ValueError(f"非法的文件名：{filename!r}")
    return filename


def bid_key(doc_id: str, filename: str) -> str:
    return f"{bids_prefix(doc_id)}{_plain_filename(filename)}"


def source_key(doc_id: str, filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    if not name:
        raise ValueError(f"非法的源文件名：{filename!r}")
    return f"{source_prefix(doc_id)}{_plain_filename(name)}"


def active(settings: Settings | None = None) -> bool:
    """True only when MinIO is enabled and credentials are present."""
    settings = settings or get_settings()
    return settings.minio_enabled and settings.minio_configured


@lru_cache(maxsize=8)
def _make_client(
    endpoint: str, access_key: str, secret_key: str, secure: bool
):
    from minio import Minio
    import urllib3

    # A stopped MinIO server must fail fast: the SDK default retries can keep
    # a list request busy for tens of seconds.  A short-lived breaker in
    # _ensure_bucket absorbs repeated calls; read timeouts stay generous for
    # large files.
    http_client = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=3.0, read=60.0),
        retries=urllib3.Retry(
            total=0,
            backoff_factor=0.2,
            status_forcelist=(500, 502, 503, 504),
            raise_on_status=False,
        ),
    )
    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        http_client=http_client,
    )


def _client(settings: Settings):
    """Return the cached MinIO client; monkeypatched by offline tests."""
    if not settings.minio_configured:
        raise StorageError("MinIO 未配置访问密钥")
    return _make_client(
        settings.minio_endpoint,
        settings.minio_access_key,
        settings.minio_secret_key,
        settings.minio_secure,
    )


def _mark_unavailable() -> None:
    global _unavailable_until
    with _unavailable_lock:
        _unavailable_until = time.monotonic() + _RETRY_AFTER_SECONDS


def _mark_available() -> None:
    global _unavailable_until
    with _unavailable_lock:
        _unavailable_until = 0.0


def _ensure_bucket(
    settings: Settings, client: Any | None = None, *, force: bool = False
) -> Any:
    if not force:
        with _unavailable_lock:
            blocked = time.monotonic() < _unavailable_until
        if blocked:
            raise StorageError(
                "MinIO 最近连接失败，暂时跳过远端操作（稍后自动重试）"
            )
    client = client or _client(settings)
    try:
        if not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)
    except Exception as exc:  # noqa: BLE001 - normalized for callers
        _mark_unavailable()
        raise StorageError(f"检查 MinIO bucket 失败：{exc}") from exc
    _mark_available()
    return client


def _iso(value: Any) -> str:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:  # noqa: BLE001 - only for display
            return ""
    return str(value or "")


def upload_file(
    settings: Settings,
    local_path: Path,
    key: str,
    *,
    content_type: str | None = None,
) -> None:
    """Upload one local file, replacing any existing object with that key."""
    if not active(settings):
        return
    if not local_path.is_file():
        raise StorageError(f"待上传文件不存在：{local_path}")
    client = _client(settings)
    _ensure_bucket(settings, client)
    guessed = content_type or _CONTENT_TYPES.get(
        local_path.suffix.lower()
    )
    if guessed is None:
        guessed = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    try:
        client.fput_object(
            settings.minio_bucket,
            key,
            str(local_path),
            content_type=guessed,
        )
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"上传对象 {key} 失败：{exc}") from exc


def list_objects(settings: Settings, prefix: str = "") -> list[StoredObject]:
    """List every object under ``prefix`` (recursive), sorted by key."""
    if not active(settings):
        return []
    client = _client(settings)
    _ensure_bucket(settings, client)
    try:
        objects = [
            StoredObject(
                key=str(item.object_name),
                size=int(getattr(item, "size", 0) or 0),
                last_modified=_iso(getattr(item, "last_modified", None)),
            )
            for item in client.list_objects(
                settings.minio_bucket, prefix=prefix, recursive=True
            )
            if not getattr(item, "is_dir", False)
        ]
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"列出对象 {prefix} 失败：{exc}") from exc
    objects.sort(key=lambda item: item.key)
    return objects


def object_exists(settings: Settings, key: str) -> bool:
    if not active(settings):
        return False
    client = _client(settings)
    _ensure_bucket(settings, client)
    try:
        client.stat_object(settings.minio_bucket, key)
    except Exception as exc:  # noqa: BLE001
        code = str(getattr(exc, "code", "") or "")
        if code in {
            "NoSuchKey",
            "NoSuchObject",
            "NoSuchBucket",
            "NoSuchVersion",
        } or isinstance(exc, KeyError):
            return False
        raise StorageError(f"检查对象 {key} 失败：{exc}") from exc
    return True


def read_bytes(
    settings: Settings, key: str, *, max_bytes: int = MAX_JSON_BYTES
) -> bytes:
    if not active(settings):
        raise StorageError("MinIO 未启用")
    client = _client(settings)
    _ensure_bucket(settings, client)
    response = None
    try:
        response = client.get_object(settings.minio_bucket, key)
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise StorageError(f"对象 {key} 超过大小上限 {max_bytes} 字节")
        return data
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"读取对象 {key} 失败：{exc}") from exc
    finally:
        _release(response)


def read_json_object(
    settings: Settings, key: str, *, max_bytes: int = MAX_JSON_BYTES
) -> dict[str, Any] | None:
    data = read_bytes(settings, key, max_bytes=max_bytes)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _release(response: Any) -> None:
    if response is None:
        return
    for method in ("close", "release_conn"):
        action = getattr(response, method, None)
        if callable(action):
            try:
                action()
            except Exception:  # noqa: BLE001 - cleanup must not mask errors
                pass


def _download_stream(
    settings: Settings, client: Any, key: str, destination: Path
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.download")
    response = None
    try:
        response = client.get_object(settings.minio_bucket, key)
        with temporary.open("wb") as output:
            while chunk := response.read(COPY_CHUNK_SIZE):
                output.write(chunk)
        temporary.replace(destination)
    except Exception as exc:  # noqa: BLE001
        temporary.unlink(missing_ok=True)
        raise StorageError(f"下载对象 {key} 失败：{exc}") from exc
    finally:
        _release(response)


def download_object(
    settings: Settings,
    key: str,
    destination: Path,
    *,
    overwrite: bool = False,
) -> bool:
    """Download one object; returns False when the local file already exists."""
    if not active(settings):
        return False
    if destination.is_file() and not overwrite:
        return False
    client = _client(settings)
    _ensure_bucket(settings, client)
    _download_stream(settings, client, key, destination)
    return True


def download_prefix(
    settings: Settings,
    prefix: str,
    destination_dir: Path,
    *,
    overwrite: bool = False,
) -> int:
    """Restore every object under ``prefix`` into ``destination_dir``."""
    if not active(settings):
        return 0
    objects = list_objects(settings, prefix)
    if not objects:
        return 0
    root = destination_dir.resolve()
    client = _client(settings)
    _ensure_bucket(settings, client)
    downloaded = 0
    for item in objects:
        relative = _relative_key(item.key[len(prefix) :] if item.key.startswith(prefix) else item.key)
        target = (root / relative).resolve()
        if root not in target.parents:
            raise StorageError(f"拒绝写入越界路径：{target}")
        if target.is_file() and not overwrite:
            continue
        _download_stream(settings, client, item.key, target)
        downloaded += 1
    return downloaded


def upload_tree(
    settings: Settings,
    local_dir: Path,
    prefix: str,
    *,
    clean: bool = False,
) -> int:
    """Upload every file under ``local_dir``; optionally delete stale objects."""
    if not active(settings):
        return 0
    client = _client(settings)
    _ensure_bucket(settings, client)
    root = local_dir.resolve()
    files: list[tuple[Path, str]] = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            relative = path.relative_to(root).as_posix()
            files.append((path, f"{prefix}{relative}"))
    try:
        for path, key in files:
            guessed = _CONTENT_TYPES.get(path.suffix.lower()) or (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            client.fput_object(
                settings.minio_bucket,
                key,
                str(path),
                content_type=guessed,
            )
        if clean:
            keep = {key for _, key in files}
            for item in list_objects(settings, prefix):
                if item.key not in keep:
                    client.remove_object(settings.minio_bucket, item.key)
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"同步目录 {local_dir} 到 {prefix} 失败：{exc}") from exc
    return len(files)


def delete_prefix(settings: Settings, prefix: str) -> int:
    if not active(settings):
        return 0
    objects = list_objects(settings, prefix)
    if not objects:
        return 0
    client = _client(settings)
    _ensure_bucket(settings, client)
    try:
        for item in objects:
            client.remove_object(settings.minio_bucket, item.key)
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"清理对象前缀 {prefix} 失败：{exc}") from exc
    return len(objects)


def remote_document_ids(
    settings: Settings | None = None, *, kind: str = "parsed"
) -> list[str]:
    """Doc ids with a remote parse manifest or bid manifest."""
    settings = settings or get_settings()
    if kind == "parsed":
        suffix = "parsed/manifest.json"
    elif kind == "bids":
        suffix = "bids/bid_manifest.json"
    else:
        raise ValueError(f"未知的对象类型：{kind}")
    suffix = f"/{suffix}"
    doc_ids: set[str] = set()
    for item in list_objects(settings, f"{DOCUMENT_PREFIX}/"):
        if not item.key.endswith(suffix):
            continue
        parts = item.key.split("/")
        if len(parts) != 4:
            continue
        doc_id = parts[1]
        try:
            _validate_doc_id(doc_id)
        except ValueError:
            continue
        doc_ids.add(doc_id)
    return sorted(doc_ids)


def remote_bid_files(settings: Settings, doc_id: str) -> dict[str, int]:
    """Direct artifact filenames and sizes for one remote bid."""
    prefix = bids_prefix(doc_id)
    files: dict[str, int] = {}
    for item in list_objects(settings, prefix):
        relative = item.key[len(prefix) :]
        if relative and "/" not in relative:
            files[relative] = item.size
    return files


def remote_parsed_files(settings: Settings, doc_id: str) -> set[str]:
    """Relative filenames available for one remote parse."""
    prefix = parsed_prefix(doc_id)
    return {
        item.key[len(prefix) :]
        for item in list_objects(settings, prefix)
        if item.key.startswith(prefix)
    }


def mirror_parse_artifacts(
    doc_id: str,
    settings: Settings | None = None,
    *,
    clean: bool = True,
) -> list[str]:
    """Mirror parse manifests, sections, images and the source PDF.

    ``clean`` removes remote parse objects that no longer exist locally.  It
    must stay off for a cache hit: extraction/index objects may be remote-only
    even though the local parse manifest is still valid.
    """
    settings = settings or get_settings()
    if not active(settings):
        return []
    warnings: list[str] = []
    try:
        upload_tree(
            settings,
            settings.parsed_dir / _validate_doc_id(doc_id),
            parsed_prefix(doc_id),
            clean=clean,
        )
    except (StorageError, ValueError) as exc:
        warnings.append(_warn(doc_id, "解析产物", exc))
    try:
        upload_tree(
            settings,
            settings.images_dir / doc_id,
            images_prefix(doc_id),
            clean=clean,
        )
    except (StorageError, ValueError) as exc:
        warnings.append(_warn(doc_id, "图片", exc))

    source = _source_path(settings, doc_id)
    if source is not None:
        try:
            prefix = source_prefix(doc_id)
            key = source_key(doc_id, source.name)
            upload_file(settings, source, key)
            for item in list_objects(settings, prefix):
                if item.key != key:
                    client = _client(settings)
                    client.remove_object(settings.minio_bucket, item.key)
        except (StorageError, ValueError) as exc:
            warnings.append(_warn(doc_id, "源文件", exc))
    return warnings


def _source_path(settings: Settings, doc_id: str) -> Path | None:
    path = settings.parsed_dir / doc_id / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    value = str(manifest.get("source_path") or "")
    if not value:
        return None
    source = Path(value)
    if not source.is_absolute():
        source = settings.project_root / source
    return source if source.is_file() else None


def mirror_extraction(doc_id: str, settings: Settings | None = None) -> list[str]:
    """Mirror ``extraction.json`` and its resumable per-unit cache."""
    settings = settings or get_settings()
    if not active(settings):
        return []
    base = settings.parsed_dir / _validate_doc_id(doc_id)
    warnings: list[str] = []
    try:
        extraction = base / "extraction.json"
        if extraction.is_file():
            upload_file(
                settings,
                extraction,
                parsed_key(doc_id, "extraction.json"),
            )
    except (StorageError, ValueError) as exc:
        warnings.append(_warn(doc_id, "抽取结果", exc))
    try:
        upload_tree(
            settings,
            base / "extraction_parts",
            parsed_key(doc_id, "extraction_parts"),
        )
    except (StorageError, ValueError) as exc:
        warnings.append(_warn(doc_id, "抽取缓存", exc))
    return warnings


def mirror_index(doc_id: str, settings: Settings | None = None) -> list[str]:
    """Mirror the Chroma index manifest."""
    settings = settings or get_settings()
    if not active(settings):
        return []
    path = settings.parsed_dir / _validate_doc_id(doc_id) / "index_manifest.json"
    if not path.is_file():
        return []
    try:
        upload_file(settings, path, parsed_key(doc_id, "index_manifest.json"))
    except (StorageError, ValueError) as exc:
        return [_warn(doc_id, "索引清单", exc)]
    return []


def mirror_bid_artifacts(
    doc_id: str,
    settings: Settings | None = None,
    *,
    output_dir: Path | None = None,
) -> list[str]:
    """Mirror the four bid artifacts (Markdown, Word, review, manifest)."""
    settings = settings or get_settings()
    if not active(settings):
        return []
    source = output_dir or (settings.bids_dir / _validate_doc_id(doc_id))
    try:
        upload_tree(settings, source, bids_prefix(doc_id), clean=True)
    except (StorageError, ValueError) as exc:
        return [_warn(doc_id, "投标产物", exc)]
    return []


def hydrate_document(
    doc_id: str,
    settings: Settings | None = None,
    *,
    include_images: bool = False,
    overwrite: bool = False,
) -> list[str]:
    """Restore parsed artifacts (and optionally images) into ``upload/``."""
    settings = settings or get_settings()
    if not active(settings):
        return []
    try:
        doc_id = _validate_doc_id(doc_id)
    except ValueError as exc:
        return [_warn(str(doc_id), "解析产物", exc)]
    warnings: list[str] = []
    targets = [
        (parsed_prefix(doc_id), settings.parsed_dir / doc_id, "解析产物"),
    ]
    if include_images:
        targets.append(
            (images_prefix(doc_id), settings.images_dir / doc_id, "图片")
        )
    for prefix, destination, label in targets:
        try:
            download_prefix(
                settings, prefix, destination, overwrite=overwrite
            )
        except (StorageError, ValueError) as exc:
            warnings.append(_warn(doc_id, label, exc))
    return warnings


def hydrate_source(
    doc_id: str, settings: Settings | None = None
) -> Path | None:
    """Restore the original PDF into ``upload/files`` and return its path."""
    settings = settings or get_settings()
    if not active(settings):
        return None
    try:
        doc_id = _validate_doc_id(doc_id)
    except ValueError as exc:
        _warn(str(doc_id), "源文件", exc)
        return None
    try:
        objects = list_objects(settings, source_prefix(doc_id))
    except (StorageError, ValueError) as exc:
        _warn(doc_id, "源文件", exc)
        return None
    if not objects:
        return None
    item = objects[0]
    name = Path(item.key).name or f"{doc_id}.pdf"
    destination = settings.files_dir / f"{doc_id}_{name}"
    try:
        download_object(settings, item.key, destination)
    except StorageError as exc:
        _warn(doc_id, "源文件", exc)
        return None
    return destination


def hydrate_bid(
    doc_id: str, settings: Settings | None = None, *, overwrite: bool = False
) -> list[str]:
    """Restore all bid artifacts into ``upload/bids/<doc_id>/``."""
    settings = settings or get_settings()
    if not active(settings):
        return []
    try:
        doc_id = _validate_doc_id(doc_id)
    except ValueError as exc:
        return [_warn(str(doc_id), "投标产物", exc)]
    try:
        download_prefix(
            settings,
            bids_prefix(doc_id),
            settings.bids_dir / doc_id,
            overwrite=overwrite,
        )
    except (StorageError, ValueError) as exc:
        return [_warn(doc_id, "投标产物", exc)]
    return []


def hydrate_bid_file(
    doc_id: str, filename: str, settings: Settings | None = None
) -> bool:
    """Restore one bid artifact on demand; callers enforce the allow-list."""
    settings = settings or get_settings()
    if not active(settings):
        return False
    try:
        doc_id = _validate_doc_id(doc_id)
        destination = settings.bids_dir / doc_id / filename
        return download_object(
            settings, bid_key(doc_id, filename), destination
        )
    except (StorageError, ValueError) as exc:
        _warn(str(doc_id), f"产物 {filename}", exc)
        return False


def check_connection(settings: Settings | None = None) -> str:
    """Ensure the endpoint is reachable and the bucket exists."""
    settings = settings or get_settings()
    if not settings.minio_enabled:
        raise StorageError("MINIO_ENABLED=false")
    if not settings.minio_configured:
        raise StorageError("MINIO_ACCESS_KEY / MINIO_SECRET_KEY 未配置")
    client = _client(settings)
    _ensure_bucket(settings, client, force=True)
    return f"{settings.minio_endpoint}/{settings.minio_bucket}"


def _warn(doc_id: str, label: str, exc: BaseException) -> str:
    message = f"MinIO 同步失败（{doc_id}/{label}）：{exc}"
    logger.warning(message)
    return message
