"""Company-profile CRUD and bid integration without a live PostgreSQL."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.app import create_app
from api.jobs import JobRunner, JobStore
from bidding import companies
from bidding.companies import CompanyStoreError, merge_profiles
from bidding.config import Settings, get_settings
from bidding.job_pipeline import execute_pipeline


class FakeCompanyBackend:
    """In-memory stand-in for the PostgreSQL functions used by the API."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.counter = 0

    def _record(self, profile: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        company_id = f"{self.counter:032x}"
        now = "2026-10-19T12:00:00+08:00"
        record = {
            "id": company_id,
            "name": companies.company_name(profile),
            "profile": profile,
            "summary": companies._summary(profile),
            "created_at": now,
            "updated_at": now,
        }
        self.records[company_id] = record
        return record

    async def list_companies(self, _settings: Settings) -> list[dict[str, Any]]:
        return list(self.records.values())

    async def get_company(
        self, _settings: Settings, company_id: str
    ) -> dict[str, Any] | None:
        return self.records.get(company_id)

    async def create_company(
        self, _settings: Settings, profile: dict[str, Any]
    ) -> dict[str, Any]:
        return self._record(profile)

    async def update_company(
        self, _settings: Settings, company_id: str, profile: dict[str, Any]
    ) -> dict[str, Any] | None:
        record = self.records.get(company_id)
        if record is None:
            return None
        record = {**record, "profile": profile, "name": companies.company_name(profile)}
        self.records[company_id] = record
        return record

    async def delete_company(self, _settings: Settings, company_id: str) -> bool:
        return self.records.pop(company_id, None) is not None


class _InlineExecutor:
    def submit(self, fn, /, *args, **kwargs) -> None:
        fn(*args, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


def _settings(tmp_path: Path) -> Settings:
    upload = tmp_path / "upload"
    return replace(
        get_settings(),
        upload_dir=upload,
        files_dir=upload / "files",
        parsed_dir=upload / "parsed",
        images_dir=upload / "images",
        bids_dir=upload / "bids",
        minio_enabled=False,
    )


def _runner(works: dict[str, Any] | None = None) -> JobRunner:
    return JobRunner(
        JobStore(max_jobs=10, ttl_seconds=3600),
        execute_pipeline,
        executor=_InlineExecutor(),
        works={"bid": lambda job, store: None, **(works or {})},
    )


def _patch_backend(monkeypatch, backend: FakeCompanyBackend) -> None:
    for name in (
        "list_companies",
        "get_company",
        "create_company",
        "update_company",
        "delete_company",
    ):
        monkeypatch.setattr(companies, name, getattr(backend, name))


def test_company_crud_endpoints(tmp_path: Path, monkeypatch) -> None:
    backend = FakeCompanyBackend()
    _patch_backend(monkeypatch, backend)
    client = TestClient(create_app(settings=_settings(tmp_path), runner=_runner()))
    profile = {
        "company": {
            "name": "北京示例工程有限公司",
            "unified_social_credit_code": "91110000EXAMPLE001",
        },
        "qualifications": [{"name": "水利水电工程施工总承包", "level": "二级"}],
    }

    with client:
        created = client.post("/api/companies", json={"profile": profile})
        assert created.status_code == 201
        record = created.json()
        assert record["name"] == "北京示例工程有限公司"
        assert record["profile"] == profile
        company_id = record["id"]
        assert len(company_id) == 32

        listed = client.get("/api/companies")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [company_id]

        detail = client.get(f"/api/companies/{company_id}")
        assert detail.status_code == 200
        assert detail.json()["summary"]["name"] == "北京示例工程有限公司"

        updated_profile = {
            "company": {
                "name": "北京示例工程有限公司",
                "contact_person": "张三",
            }
        }
        updated = client.put(
            f"/api/companies/{company_id}", json={"profile": updated_profile}
        )
        assert updated.status_code == 200
        assert updated.json()["profile"] == updated_profile

        invalid = client.get("/api/companies/not-a-company-id")
        assert invalid.status_code == 422

        missing = client.get(f"/api/companies/{'0' * 32}")
        assert missing.status_code == 404

        deleted = client.delete(f"/api/companies/{company_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/companies/{company_id}").status_code == 404


def test_company_store_unavailable_returns_503(tmp_path: Path, monkeypatch) -> None:
    async def broken(_settings: Settings) -> list[dict[str, Any]]:
        raise CompanyStoreError("数据库未启动")

    monkeypatch.setattr(companies, "list_companies", broken)
    client = TestClient(create_app(settings=_settings(tmp_path), runner=_runner()))

    with client:
        response = client.get("/api/companies")

    assert response.status_code == 503
    assert "数据库未启动" in response.json()["detail"]


def _prepare_bid_artifacts(settings: Settings, doc_id: str) -> None:
    parsed = settings.parsed_dir / doc_id
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / "manifest.json").write_text("{}", encoding="utf-8")
    (parsed / "extraction.json").write_text("{}", encoding="utf-8")


def test_bid_uses_saved_company_profile_with_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    backend = FakeCompanyBackend()
    _patch_backend(monkeypatch, backend)
    settings = _settings(tmp_path)
    doc_id = "公司写标"
    _prepare_bid_artifacts(settings, doc_id)
    stored = backend._record(
        {
            "company": {
                "name": "已保存的公司",
                "phone": "010-00000000",
                "email": "saved@example.com",
            }
        }
    )
    seen: list[dict[str, Any]] = []

    def work(job, store) -> None:
        try:
            seen.append(json.loads(job.profile_path.read_text(encoding="utf-8")))
            store.set_result(job.id, "bid", {"doc_id": job.doc_id})
        finally:
            shutil.rmtree(job.upload_dir, ignore_errors=True)

    client = TestClient(
        create_app(settings=settings, runner=_runner({"bid": work}))
    )

    with client:
        response = client.post(
            "/api/bids",
            json={
                "doc_id": doc_id,
                "company_id": stored["id"],
                "profile": {"company": {"phone": "13800000000"}},
            },
        )
        assert response.status_code == 202
        job = client.get(response.json()["status_url"]).json()

    assert job["status"] == "succeeded"
    assert seen[0]["company"]["name"] == "已保存的公司"
    assert seen[0]["company"]["phone"] == "13800000000"
    assert seen[0]["company"]["email"] == "saved@example.com"


def test_bid_rejects_unknown_company(tmp_path: Path, monkeypatch) -> None:
    _patch_backend(monkeypatch, FakeCompanyBackend())
    settings = _settings(tmp_path)
    doc_id = "未知公司"
    _prepare_bid_artifacts(settings, doc_id)
    client = TestClient(create_app(settings=settings, runner=_runner({"bid": lambda job, store: None})))

    with client:
        response = client.post(
            "/api/bids", json={"doc_id": doc_id, "company_id": "1" * 32}
        )

    assert response.status_code == 404
    assert "公司资料不存在" in response.json()["detail"]


def test_merge_profiles_is_deep_and_lists_replace() -> None:
    stored = {
        "company": {"name": "甲方", "phone": "1"},
        "qualifications": [{"name": "旧资质"}],
    }
    merged = merge_profiles(
        stored,
        {"company": {"phone": "2"}, "qualifications": [{"name": "新资质"}]},
    )
    assert merged == {
        "company": {"name": "甲方", "phone": "2"},
        "qualifications": [{"name": "新资质"}],
    }
    assert stored["company"]["phone"] == "1"
