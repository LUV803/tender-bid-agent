"""Shared offline test helpers (not collected by pytest)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from bidding.config import Settings, get_settings


class FakeRedis:
    """Small in-memory stand-in for the Redis commands the job store uses."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.closed = False

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def hset(
        self,
        key: str,
        name: str | None = None,
        value: str | None = None,
        *,
        mapping: dict[str, str] | None = None,
    ) -> int:
        data = self.hashes.setdefault(key, {})
        if mapping:
            data.update({str(k): str(v) for k, v in mapping.items()})
        if name is not None:
            data[str(name)] = str(value)
        return 1

    def hget(self, key: str, name: str) -> str | None:
        return self.hashes.get(key, {}).get(name)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, seconds: int) -> bool:
        if key in self.hashes or key in self.lists or key in self.strings:
            self.expirations[key] = seconds
            return True
        return False

    def rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.lists.get(key, [])
        length = len(items)
        if start < 0:
            start = max(length + start, 0)
        if end < 0:
            end = length + end
        return items[start : end + 1]

    def ltrim(self, key: str, start: int, end: int) -> bool:
        items = self.lists.get(key, [])
        length = len(items)
        if start < 0:
            start = max(length + start, 0)
        if end < 0:
            end = length + end
        self.lists[key] = items[start : end + 1]
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            for store in (self.hashes, self.lists, self.strings, self.expirations):
                if key in store:
                    del store[key]
                    removed += 1
        return removed

    def close(self) -> None:
        self.closed = True


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    upload = tmp_path / "upload"
    values = {
        "upload_dir": upload,
        "files_dir": upload / "files",
        "parsed_dir": upload / "parsed",
        "images_dir": upload / "images",
        "bids_dir": upload / "bids",
        "api_max_upload_mb": 1,
        "api_job_workers": 1,
        "api_job_ttl_seconds": 3600,
        "api_max_jobs": 10,
        **overrides,
    }
    return replace(get_settings(), **values)
