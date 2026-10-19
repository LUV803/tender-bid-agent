"""Bidder profile loading and the ``【待填写】`` placeholder policy.

The bidder profile is the only source of company-specific facts.  Every field
that is missing from the JSON is materialised as a visible placeholder instead
of being guessed, and every placeholder is recorded in ``BidderProfile.gaps``
so reviewers and the generated checklist can point back to the missing input.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BidWriteError

PLACEHOLDER_PREFIX = "【待填写："
PLACEHOLDER_RE = re.compile(r"【待填写(?:[：:][^】]*)?】")
MAX_PROFILE_BYTES = 2 * 1024 * 1024

COMPANY_FIELDS: dict[str, str] = {
    "name": "公司名称",
    "unified_social_credit_code": "统一社会信用代码",
    "legal_representative": "法定代表人",
    "registered_capital": "注册资本",
    "established_date": "成立日期",
    "address": "注册地址",
    "contact_person": "投标联系人",
    "phone": "联系电话",
    "email": "电子邮箱",
}

QUALIFICATION_FIELDS: dict[str, str] = {
    "name": "资质名称",
    "level": "资质等级",
    "certificate_no": "证书编号",
    "issuer": "发证机关",
    "valid_until": "有效期至",
    "note": "补充说明",
}

PROJECT_FIELDS: dict[str, str] = {
    "project_name": "项目名称",
    "owner": "发包人",
    "contract_amount": "合同金额",
    "start_date": "开工日期",
    "end_date": "竣工日期",
    "role": "担任角色",
    "description": "主要工作内容",
    "proof": "证明材料",
}

PERSONNEL_FIELDS: dict[str, str] = {
    "name": "人员姓名",
    "role": "拟任岗位",
    "title": "职称/职务",
    "certificate": "执业资格",
    "certificate_no": "证书编号",
    "experience_years": "从业年限",
    "projects": "类似项目经历",
    "note": "补充说明",
}

FINANCIAL_FIELDS: dict[str, str] = {
    "year": "年度",
    "revenue": "营业收入",
    "net_profit": "净利润",
    "total_assets": "总资产",
    "total_liabilities": "总负债",
    "audit_status": "审计情况",
}

MANAGEMENT_SYSTEM_FIELDS: dict[str, str] = {
    "name": "体系名称",
    "standard": "认证标准",
    "certificate_no": "证书编号",
    "valid_until": "有效期至",
}

CREDIT_FIELDS: dict[str, str] = {
    "rating": "信用等级",
    "rating_agency": "评级机构",
    "bank_credit": "银行信用等级",
    "tax_rating": "纳税信用等级",
}

SAFETY_FIELDS: dict[str, str] = {
    "production_license_no": "安全生产许可证编号",
    "valid_until": "有效期至",
    "accident_record": "安全事故记录",
}

SECTION_HINTS: dict[str, str] = {
    "company": "投标人基本情况",
    "qualifications": "资质证书",
    "similar_projects": "类似项目业绩",
    "personnel": "项目管理机构",
    "financials": "财务状况",
    "management_systems": "管理体系认证",
    "credit": "信用状况",
    "safety": "安全生产",
}


def placeholder(label: str) -> str:
    """Return the single placeholder spelling used throughout the project."""
    return f"{PLACEHOLDER_PREFIX}{label}】"


def has_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_RE.search(value or ""))


@dataclass(slots=True, frozen=True)
class ProfileGap:
    """A bidder fact that was not supplied and must be filled by a human."""

    path: str
    label: str
    placeholder: str
    section: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "label": self.label,
            "placeholder": self.placeholder,
            "section": self.section,
        }


@dataclass(slots=True)
class BidderProfile:
    source_path: Path | None = None
    sha256: str = ""
    company: dict[str, str] = field(default_factory=dict)
    qualifications: list[dict[str, str]] = field(default_factory=list)
    similar_projects: list[dict[str, str]] = field(default_factory=list)
    personnel: list[dict[str, str]] = field(default_factory=list)
    financials: list[dict[str, str]] = field(default_factory=list)
    credit: dict[str, str] = field(default_factory=dict)
    management_systems: list[dict[str, str]] = field(default_factory=list)
    safety: dict[str, str] = field(default_factory=dict)
    commitments: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    notes: str = ""
    gaps: list[ProfileGap] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def company_name(self) -> str:
        return self.company.get("name") or placeholder(COMPANY_FIELDS["name"])

    @property
    def provided_field_count(self) -> int:
        """Count values that came from the user rather than a placeholder."""
        count = 0
        for value in self.company.values():
            if value and not has_placeholder(value):
                count += 1
        for rows, fields in (
            (self.qualifications, QUALIFICATION_FIELDS),
            (self.similar_projects, PROJECT_FIELDS),
            (self.personnel, PERSONNEL_FIELDS),
            (self.financials, FINANCIAL_FIELDS),
            (self.management_systems, MANAGEMENT_SYSTEM_FIELDS),
        ):
            for row in rows:
                for key in fields:
                    if row.get(key) and not has_placeholder(row[key]):
                        count += 1
        for value in self.credit.values():
            if value and not has_placeholder(value):
                count += 1
        for value in self.safety.values():
            if value and not has_placeholder(value):
                count += 1
        count += len(self.commitments) + len(self.capabilities)
        return count

    def as_dict(self) -> dict[str, Any]:
        """Normalised view used for prompts and the internal review report.

        Collections and nested mappings are copied because ``prompt_text`` clips
        values in place after calling this method; the profile seen by the
        deterministic tables must stay untouched.
        """
        return {
            "company": dict(self.company),
            "qualifications": [dict(row) for row in self.qualifications],
            "similar_projects": [dict(row) for row in self.similar_projects],
            "personnel": [dict(row) for row in self.personnel],
            "financials": [dict(row) for row in self.financials],
            "credit": dict(self.credit),
            "management_systems": [dict(row) for row in self.management_systems],
            "safety": dict(self.safety),
            "commitments": list(self.commitments),
            "capabilities": list(self.capabilities),
            "notes": self.notes,
        }

    def prompt_text(self, max_chars: int = 16000) -> str:
        """Compact JSON for an LLM prompt.

        Long collections are trimmed only for the prompt; tables rendered into
        the bid still use the complete profile.  Placeholders survive trimming
        because they live inside each row.
        """
        data = self.as_dict()
        for key in ("qualifications", "similar_projects", "personnel", "financials", "management_systems"):
            data[key] = _trim_rows(data[key], 12 if key != "financials" else 6)
        data["commitments"] = data["commitments"][:12]
        data["capabilities"] = data["capabilities"][:12]
        for key, value in list(data["company"].items()):
            data["company"][key] = _clip(value, 300)
        for row_key in ("qualifications", "similar_projects", "personnel", "financials", "management_systems"):
            for row in data[row_key]:
                for key, value in list(row.items()):
                    row[key] = _clip(value, 500)
        text = json.dumps(data, ensure_ascii=False, indent=1)
        if len(text) > max_chars:
            text = text[:max_chars]
            marker = "\n...（投标人资料过长，提示词中已截断；仍须遵守：缺失事实用【待填写：...】）"
            text = text[: max(0, max_chars - len(marker))] + marker
        return text

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path) if self.source_path else "",
            "sha256": self.sha256,
            "provided_field_count": self.provided_field_count,
            "data": self.as_dict(),
            "gaps": [gap.to_dict() for gap in self.gaps],
            "warnings": list(self.warnings),
        }


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _trim_rows(rows: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    if len(rows) <= limit:
        return [dict(row) for row in rows]
    trimmed = [dict(row) for row in rows[:limit]]
    trimmed.append(
        {
            key: placeholder(f"其余{limit}条以后的资料（完整资料请查投标人资料文件）")
            for key in rows[0]
        }
    )
    return trimmed


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, list):
        return "；".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _get(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return default


def _add_gap(
    gaps: list[ProfileGap],
    path: str,
    label: str,
    section: str,
    seen: set[str],
) -> str:
    marker = placeholder(label)
    key = f"{path}\0{label}"
    if key not in seen:
        gaps.append(ProfileGap(path=path, label=label, placeholder=marker, section=section))
        seen.add(key)
    return marker


def _record(
    raw: Any,
    fields: dict[str, str],
    *,
    path: str,
    section: str,
    gaps: list[ProfileGap],
    seen: set[str],
    first_key: str | None = None,
) -> dict[str, str]:
    if isinstance(raw, str) or isinstance(raw, (int, float)):
        raw = {first_key or next(iter(fields)): raw}
    if not isinstance(raw, dict):
        raw = {}
    result: dict[str, str] = {}
    for key, label in fields.items():
        value = _text(raw.get(key))
        if value:
            result[key] = value
        else:
            result[key] = _add_gap(
                gaps, f"{path}.{key}", label, section, seen
            )
    return result


def _rows(
    raw: Any,
    fields: dict[str, str],
    *,
    path: str,
    section: str,
    gaps: list[ProfileGap],
    seen: set[str],
    empty: bool = True,
) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        # Financials are often keyed by year: {"2023": {...}}.
        if "year" in fields and raw and all(isinstance(value, dict) for value in raw.values()):
            items: list[Any] = [
                {"year": year, **value}
                for year, value in raw.items()
                if isinstance(value, dict)
            ]
        else:
            items = [raw]
    elif isinstance(raw, list):
        items = raw
    elif raw:
        items = [raw]
    else:
        items = []

    if not items and not empty:
        return []

    if not items:
        items = [{}]

    rows: list[dict[str, str]] = []
    for index, item in enumerate(items):
        rows.append(
            _record(
                item,
                fields,
                path=f"{path}[{index}]",
                section=section,
                gaps=gaps,
                seen=seen,
            )
        )
    return rows


def _company_from(raw: dict[str, Any]) -> Any:
    nested = _get(raw, "company", "company_info", "basic_info", "basic_information", "bidder")
    if nested is not None:
        return nested
    if any(key in raw for key in COMPANY_FIELDS):
        return raw
    return {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for text in (_text(item) for item in value) if text]
    text = _text(value)
    return [text] if text else []


def normalize_profile(data: dict[str, Any], *, source_path: Path | None = None, sha256: str = "") -> BidderProfile:
    if not isinstance(data, dict):
        raise BidWriteError("投标人资料 JSON 顶层必须是对象")
    gaps: list[ProfileGap] = []
    seen: set[str] = set()
    warnings: list[str] = []

    company = _record(
        _company_from(data),
        COMPANY_FIELDS,
        path="company",
        section=SECTION_HINTS["company"],
        gaps=gaps,
        seen=seen,
    )
    qualifications = _rows(
        _get(data, "qualifications", "qualification", "certificates", "qualification_certificates"),
        QUALIFICATION_FIELDS,
        path="qualifications",
        section=SECTION_HINTS["qualifications"],
        gaps=gaps,
        seen=seen,
    )
    similar_projects = _rows(
        _get(data, "similar_projects", "projects", "performance", "similar_performance"),
        PROJECT_FIELDS,
        path="similar_projects",
        section=SECTION_HINTS["similar_projects"],
        gaps=gaps,
        seen=seen,
    )
    personnel = _rows(
        _get(data, "personnel", "staff", "team", "project_team"),
        PERSONNEL_FIELDS,
        path="personnel",
        section=SECTION_HINTS["personnel"],
        gaps=gaps,
        seen=seen,
    )
    financials = _rows(
        _get(data, "financials", "financial", "finance", "financial_statements"),
        FINANCIAL_FIELDS,
        path="financials",
        section=SECTION_HINTS["financials"],
        gaps=gaps,
        seen=seen,
    )
    credit = _record(
        _get(data, "credit", "credit_info", "credit_rating", default={}),
        CREDIT_FIELDS,
        path="credit",
        section=SECTION_HINTS["credit"],
        gaps=gaps,
        seen=seen,
    )
    management_systems = _rows(
        _get(data, "management_systems", "certifications", "system_certifications"),
        MANAGEMENT_SYSTEM_FIELDS,
        path="management_systems",
        section=SECTION_HINTS["management_systems"],
        gaps=gaps,
        seen=seen,
    )
    safety = _record(
        _get(data, "safety", "safety_info", "production_safety", default={}),
        SAFETY_FIELDS,
        path="safety",
        section=SECTION_HINTS["safety"],
        gaps=gaps,
        seen=seen,
    )
    commitments = _string_list(_get(data, "commitments", "commitment", default=[]))
    capabilities = _string_list(_get(data, "capabilities", "strengths", "advantages", default=[]))
    notes = _text(_get(data, "notes", "note", default=""))

    if len(qualifications) > 50:
        warnings.append("资质条目超过 50 条，标书表格会完整输出，提示词仅取前 12 条")
    if len(similar_projects) > 50:
        warnings.append("业绩条目超过 50 条，标书表格会完整输出，提示词仅取前 12 条")

    return BidderProfile(
        source_path=source_path,
        sha256=sha256,
        company=company,
        qualifications=qualifications,
        similar_projects=similar_projects,
        personnel=personnel,
        financials=financials,
        credit=credit,
        management_systems=management_systems,
        safety=safety,
        commitments=commitments,
        capabilities=capabilities,
        notes=notes,
        gaps=gaps,
        warnings=warnings,
    )


def load_profile(path: str | Path) -> BidderProfile:
    path = Path(path).expanduser()
    if not path.is_file():
        raise BidWriteError(
            f"找不到投标人资料 {path}。请复制 bidder_profile.example.json "
            "为 upload/bidder_profile.json 并填写；未提供的事实会生成【待填写】占位符。"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BidWriteError(f"无法读取投标人资料：{exc}") from exc
    if size > MAX_PROFILE_BYTES:
        raise BidWriteError(
            f"投标人资料超过 {MAX_PROFILE_BYTES // 1024 // 1024} MB 上限：{path}"
        )
    try:
        raw_bytes = path.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BidWriteError(f"投标人资料不是合法的 UTF-8 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise BidWriteError("投标人资料 JSON 顶层必须是对象")
    return normalize_profile(
        data,
        source_path=path.resolve(),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
