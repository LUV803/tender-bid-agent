"""Company (bidder profile) storage in PostgreSQL.

The bidder profile is a nested JSON object, so the durable record keeps the
whole profile in one JSON column while mirroring the company name for listing
and search.  PostgreSQL is optional for the rest of the application: company
endpoints fail with a clear error when ``DATABASE_URL`` is missing or the
database is unreachable, and the table is created lazily on first use.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, String, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from .config import Settings

logger = logging.getLogger(__name__)

COMPANY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
UNNAMED_COMPANY = "未命名公司"
DATABASE_HINT = (
    "无法使用 PostgreSQL 公司资料库：请确认数据库服务已启动，"
    "并在 .env 中配置可用的 DATABASE_URL"
)


class CompanyStoreError(RuntimeError):
    """The company store is not configured or currently unreachable."""


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


_engine: AsyncEngine | None = None
_engine_url = ""
_session_factory: async_sessionmaker[AsyncSession] | None = None
_schema_ready: set[str] = set()


def valid_company_id(company_id: str) -> bool:
    return bool(COMPANY_ID_RE.fullmatch(company_id or ""))


def _database_url(settings: Settings) -> str:
    url = (settings.database_url or "").strip()
    if not url:
        raise CompanyStoreError(
            "未配置 DATABASE_URL，无法使用 PostgreSQL 公司资料库"
        )
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    raise CompanyStoreError("DATABASE_URL 目前仅支持 PostgreSQL（postgresql+asyncpg://）")


def _engine_for(settings: Settings) -> tuple[AsyncEngine, str]:
    global _engine, _engine_url, _session_factory
    url = _database_url(settings)
    if _engine is None or _engine_url != url:
        _engine = create_async_engine(
            url,
            poolclass=NullPool,
            pool_pre_ping=True,
            connect_args={"timeout": 5, "command_timeout": 15},
        )
        _session_factory = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession
        )
        _engine_url = url
    assert _session_factory is not None
    return _engine, url


async def _ensure_schema(engine: AsyncEngine, url: str) -> None:
    if url in _schema_ready:
        return
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    _schema_ready.add(url)


def _session(settings: Settings) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine, str]:
    engine, url = _engine_for(settings)
    assert _session_factory is not None
    return _session_factory, engine, url


def _clean_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Detached, JSON-serialisable copy of the submitted profile."""
    try:
        data = json.loads(json.dumps(profile, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise CompanyStoreError(f"公司资料必须是可序列化的 JSON 对象：{exc}") from exc
    if not isinstance(data, dict):
        raise CompanyStoreError("公司资料顶层必须是 JSON 对象")
    return data


def company_name(profile: dict[str, Any]) -> str:
    company = profile.get("company")
    if not isinstance(company, dict):
        # Also accept a flat profile produced by hand-written JSON.
        company = profile if "name" in profile else {}
    value = company.get("name")
    name = str(value).strip() if value is not None else ""
    return name[:200] or UNNAMED_COMPANY


def _summary(profile: dict[str, Any]) -> dict[str, Any]:
    company = profile.get("company") if isinstance(profile.get("company"), dict) else {}
    company = company or {}

    def count(key: str) -> int:
        value = profile.get(key)
        return len(value) if isinstance(value, list) else 0

    def text(key: str) -> str:
        value = company.get(key)
        return str(value).strip() if value is not None else ""

    return {
        "name": text("name"),
        "unified_social_credit_code": text("unified_social_credit_code"),
        "contact_person": text("contact_person"),
        "phone": text("phone"),
        "qualification_count": count("qualifications"),
        "project_count": count("similar_projects"),
        "personnel_count": count("personnel"),
    }


def _record(company: Company) -> dict[str, Any]:
    profile = company.profile if isinstance(company.profile, dict) else {}
    return {
        "id": company.id,
        "name": company.name,
        "profile": profile,
        "summary": _summary(profile),
        "created_at": company.created_at.isoformat(timespec="seconds"),
        "updated_at": company.updated_at.isoformat(timespec="seconds"),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _run(settings: Settings, operation):
    factory, engine, url = _session(settings)
    try:
        await _ensure_schema(engine, url)
        async with factory() as session:
            return await operation(session)
    except CompanyStoreError:
        raise
    except Exception as exc:
        logger.warning("PostgreSQL 公司资料库操作失败：%s", exc)
        raise CompanyStoreError(DATABASE_HINT) from exc


async def list_companies(settings: Settings) -> list[dict[str, Any]]:
    async def operation(session: AsyncSession) -> list[dict[str, Any]]:
        result = await session.execute(
            select(Company).order_by(Company.updated_at.desc(), Company.name)
        )
        return [_record(company) for company in result.scalars().all()]

    return await _run(settings, operation)


async def get_company(settings: Settings, company_id: str) -> dict[str, Any] | None:
    if not valid_company_id(company_id):
        return None

    async def operation(session: AsyncSession) -> dict[str, Any] | None:
        company = await session.get(Company, company_id)
        return _record(company) if company is not None else None

    return await _run(settings, operation)


async def create_company(
    settings: Settings, profile: dict[str, Any]
) -> dict[str, Any]:
    data = _clean_profile(profile)
    now = _now()
    company = Company(
        id=uuid.uuid4().hex,
        name=company_name(data),
        profile=data,
        created_at=now,
        updated_at=now,
    )

    async def operation(session: AsyncSession) -> dict[str, Any]:
        session.add(company)
        await session.commit()
        return _record(company)

    return await _run(settings, operation)


async def update_company(
    settings: Settings, company_id: str, profile: dict[str, Any]
) -> dict[str, Any] | None:
    if not valid_company_id(company_id):
        return None
    data = _clean_profile(profile)

    async def operation(session: AsyncSession) -> dict[str, Any] | None:
        company = await session.get(Company, company_id)
        if company is None:
            return None
        company.profile = data
        company.name = company_name(data)
        company.updated_at = _now()
        await session.commit()
        return _record(company)

    return await _run(settings, operation)


async def delete_company(settings: Settings, company_id: str) -> bool:
    if not valid_company_id(company_id):
        return False

    async def operation(session: AsyncSession) -> bool:
        company = await session.get(Company, company_id)
        if company is None:
            return False
        await session.delete(company)
        await session.commit()
        return True

    return await _run(settings, operation)


def merge_profiles(
    stored: dict[str, Any], override: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge a saved company profile with explicit request overrides.

    Nested objects merge key by key; lists and scalar values from ``override``
    replace the stored value.  Empty strings still override so a caller can
    deliberately clear a field.
    """
    base = copy.deepcopy(stored) if isinstance(stored, dict) else {}
    for key, value in (override or {}).items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            base[key] = merge_profiles(current, value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def reset_engine_for_tests() -> None:
    """Drop cached engine/schema state (used by tests with a temporary DB)."""
    global _engine, _engine_url, _session_factory
    _engine = None
    _engine_url = ""
    _session_factory = None
    _schema_ready.clear()
