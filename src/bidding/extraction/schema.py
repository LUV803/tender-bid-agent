"""Tolerant normalization, merging and grounding for extraction results.

LLM output is treated as untrusted JSON: fields are normalized one by one,
unknown keys are dropped, lists are de-duplicated and source quotes are
checked against the authoritative Markdown before being reported.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

from ..parsing.structure import PAGE_MARKER_RE, normalize_ws

SCALAR_FIELDS = (
    "project_name",
    "tender_number",
    "project_scale",
    "bid_section",
    "budget_amount_text",
    "bid_deadline",
    "bid_opening_time",
    "bid_opening_place",
    "evaluation_method",
)

LIST_FIELDS = (
    "evaluation_factors",
    "preliminary_review",
    "qualifications",
    "key_dates",
    "contract_points",
    "technical_requirements",
    "other_requirements",
)

LIST_ALIASES: dict[str, tuple[str, ...]] = {
    "evaluation_factors": (
        "evaluation_factors",
        "scoring_factors",
        "scoring_items",
        "score_items",
    ),
    "preliminary_review": (
        "preliminary_review",
        "preliminary_review_items",
        "preliminary_items",
    ),
    "qualifications": (
        "qualifications",
        "qualification_requirements",
        "qualification_conditions",
        "eligibility_requirements",
        "qualification_items",
    ),
    "key_dates": ("key_dates", "timeline", "important_dates", "key_dates_times"),
    "contract_points": (
        "contract_points",
        "contract_requirements",
        "contract_terms",
    ),
    "technical_requirements": (
        "technical_requirements",
        "technical_specs",
        "technical_specifications",
        "procurement_requirements",
    ),
    "other_requirements": ("other_requirements", "other_notes"),
}

QUALIFICATION_CATEGORIES = ("资质", "财务", "业绩", "信誉", "人员", "其他")
REVIEW_STAGES = ("形式评审", "资格评审", "响应性评审", "其他")
FACTOR_CATEGORIES = ("商务", "技术", "报价", "其他")

_CATEGORY_HINTS = {
    "qualifications": QUALIFICATION_CATEGORIES,
    "preliminary_review": REVIEW_STAGES,
    "evaluation_factors": FACTOR_CATEGORIES,
}

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "；".join(_as_str(item) for item in value if _as_str(item))
    return str(value).strip()


def _as_str_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        text = _as_str(item)
        if text:
            result.append(text)
    return result


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _category(value: Any, field: str, source_text: str = "") -> str:
    allowed = _CATEGORY_HINTS.get(field, ())
    text = _as_str(value)
    for name in allowed:
        if name and name in text:
            return name
    for name in allowed:
        if name and name in source_text:
            return name
    return "其他" if allowed else text


def _first(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _as_str(item.get(key))
        if value:
            return value
    return ""


def _dedupe(items: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = key_fn(item)
        if not key:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _dedupe_key(field: str, item: dict[str, Any]) -> str:
    if field == "evaluation_factors":
        return normalize_ws(_first(item, "factor", "name", "title"))
    if field == "preliminary_review":
        return normalize_ws(_first(item, "item", "name") + _first(item, "criteria"))
    if field == "qualifications":
        return normalize_ws(_first(item, "requirement", "name", "title"))
    if field == "key_dates":
        return normalize_ws(_first(item, "event", "name") + _first(item, "time", "date"))
    if field in {"contract_points", "technical_requirements"}:
        return normalize_ws(
            _first(item, "topic", "name") + _first(item, "detail", "requirement")
        )
    if field == "other_requirements":
        return normalize_ws(item.get("text", ""))
    return normalize_ws(json.dumps(item, ensure_ascii=False, sort_keys=True))


def _combine_text(first: str, second: str) -> str:
    first = (first or "").strip()
    second = (second or "").strip()
    if not second:
        return first
    if not first:
        return second
    if second in first:
        return first
    if first in second:
        return second
    return f"{first}\n{second}"


def _merge_factors(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        factor = item.get("factor", "")
        key = normalize_ws(factor) or json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
            continue
        target = merged[key]
        if target.get("category") == "其他" and item.get("category") != "其他":
            target["category"] = item.get("category")
        incoming_score = item.get("max_score")
        if incoming_score is not None:
            current_score = target.get("max_score")
            target["max_score"] = (
                incoming_score
                if current_score is None
                else max(float(current_score), float(incoming_score))
            )
        target["criteria"] = _combine_text(
            target.get("criteria", ""), item.get("criteria", "")
        )
        target["source_quote"] = _combine_text(
            target.get("source_quote", ""), item.get("source_quote", "")
        )
    return [merged[key] for key in order]


def _merge_qualifications(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        requirement = item.get("requirement", "")
        key = normalize_ws(requirement) or json.dumps(
            item, ensure_ascii=False, sort_keys=True
        )
        if key not in merged:
            merged[key] = dict(item)
            merged[key]["thresholds"] = list(item.get("thresholds") or [])
            order.append(key)
            continue
        target = merged[key]
        if target.get("category") == "其他" and item.get("category") != "其他":
            target["category"] = item.get("category")
        target["thresholds"] = list(
            dict.fromkeys([*(target.get("thresholds") or []), *(item.get("thresholds") or [])])
        )
        target["source_quote"] = _combine_text(
            target.get("source_quote", ""), item.get("source_quote", "")
        )
    return [merged[key] for key in order]


def _merge_bid_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        title = item.get("title", "")
        key = normalize_ws(title) or json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
            continue
        merged[key]["requirements"] = _combine_text(
            merged[key].get("requirements", ""), item.get("requirements", "")
        )
    return [merged[key] for key in order]


def _normalize_factor(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        item = {"factor": item}
    if not isinstance(item, dict):
        return None
    factor = _first(item, "factor", "name", "title", "评分因素", "item")
    if not factor:
        return None
    criteria = _first(item, "criteria", "standard", "评分标准", "requirement", "description")
    raw_score = item.get("max_score")
    if raw_score is None or raw_score == "":
        raw_score = item.get("score", item.get("分值"))
    return {
        "category": _category(item.get("category"), "evaluation_factors", factor + criteria),
        "factor": factor,
        "max_score": _as_number(raw_score),
        "criteria": criteria,
        "source_quote": _first(item, "source_quote", "quote", "原文", "evidence"),
    }


def _normalize_review(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        item = {"item": item}
    if not isinstance(item, dict):
        return None
    name = _first(item, "item", "name", "title", "评审因素")
    if not name:
        return None
    criteria = _first(item, "criteria", "评审标准", "standard", "requirement", "description")
    return {
        "stage": _category(item.get("stage"), "preliminary_review", name + criteria),
        "item": name,
        "criteria": criteria,
        "source_quote": _first(item, "source_quote", "quote", "原文", "evidence"),
    }


def _normalize_qualification(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        item = {"requirement": item}
    if not isinstance(item, dict):
        return None
    requirement = _first(item, "requirement", "name", "title", "条件", "description")
    if not requirement:
        return None
    thresholds = _as_str_list(
        item.get("thresholds", item.get("threshold", item.get("门槛")))
    )
    return {
        "category": _category(item.get("category"), "qualifications", requirement),
        "requirement": requirement,
        "thresholds": thresholds,
        "source_quote": _first(item, "source_quote", "quote", "原文", "evidence"),
    }


def _normalize_key_date(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        item = {"event": item}
    if not isinstance(item, dict):
        return None
    event = _first(item, "event", "name", "title", "事项")
    time = _first(item, "time", "date", "deadline", "时间")
    if not event and not time:
        return None
    return {
        "event": event,
        "time": time,
        "source_quote": _first(item, "source_quote", "quote", "原文", "evidence"),
    }


def _normalize_contract_point(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        item = {"detail": item}
    if not isinstance(item, dict):
        return None
    topic = _first(item, "topic", "name", "title", "要点")
    detail = _first(item, "detail", "requirement", "content", "description")
    if not topic and not detail:
        return None
    return {
        "topic": topic,
        "detail": detail or topic,
        "source_quote": _first(item, "source_quote", "quote", "原文", "evidence"),
    }


def _normalize_technical(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        item = {"requirement": item}
    if not isinstance(item, dict):
        return None
    topic = _first(item, "topic", "name", "title", "需求项")
    requirement = _first(
        item, "requirement", "detail", "content", "description", "技术参数"
    )
    if not topic and not requirement:
        return None
    return {
        "topic": topic,
        "requirement": requirement or topic,
        "source_quote": _first(item, "source_quote", "quote", "原文", "evidence"),
    }


def _normalize_bid_format(value: Any) -> dict[str, list[dict[str, str]]]:
    if isinstance(value, dict):
        raw_items = value.get("items") or value.get("list") or []
    else:
        raw_items = value or []
    items: list[dict[str, str]] = []
    for raw in _as_list(raw_items):
        if isinstance(raw, str):
            title = raw.strip()
            requirements = ""
        elif isinstance(raw, dict):
            title = _first(raw, "title", "name", "item", "名称")
            requirements = _first(
                raw, "requirements", "description", "content", "notes", "要求"
            )
        else:
            continue
        if not title:
            continue
        items.append({"title": title, "requirements": requirements})
    return {"items": _merge_bid_items(items)}


def _raw_item_list(raw: dict[str, Any], field: str) -> list[Any]:
    for alias in LIST_ALIASES[field]:
        value = raw.get(alias)
        if value:
            if field == "bid_format":
                continue
            return _as_list(value)
    return []


def normalize_extraction(raw: Any) -> dict[str, Any]:
    """Normalize one LLM result into the declared extraction schema."""
    if not isinstance(raw, dict):
        return empty_extraction()

    flattened = copy.deepcopy(raw)
    for nested_key in ("basic_info", "project_info", "info", "project"):
        nested = flattened.get(nested_key)
        if isinstance(nested, dict):
            for field in SCALAR_FIELDS:
                if not flattened.get(field) and nested.get(field):
                    flattened[field] = nested[field]
    evaluation = flattened.get("evaluation")
    if isinstance(evaluation, dict):
        if not flattened.get("evaluation_method"):
            flattened["evaluation_method"] = evaluation.get("method") or evaluation.get("name")
        if not flattened.get("evaluation_factors"):
            flattened["evaluation_factors"] = (
                evaluation.get("factors") or evaluation.get("evaluation_factors")
            )
        if not flattened.get("preliminary_review"):
            flattened["preliminary_review"] = (
                evaluation.get("preliminary_review")
                or evaluation.get("preliminary_items")
            )

    result = empty_extraction()
    for field in SCALAR_FIELDS:
        result[field] = _as_str(flattened.get(field))

    for field in (
        "evaluation_factors",
        "preliminary_review",
        "qualifications",
        "key_dates",
        "contract_points",
        "technical_requirements",
    ):
        normalizer = {
            "evaluation_factors": _normalize_factor,
            "preliminary_review": _normalize_review,
            "qualifications": _normalize_qualification,
            "key_dates": _normalize_key_date,
            "contract_points": _normalize_contract_point,
            "technical_requirements": _normalize_technical,
        }[field]
        items: list[dict[str, Any]] = []
        for raw_item in _raw_item_list(flattened, field):
            normalized = normalizer(raw_item)
            if normalized:
                items.append(normalized)
        if field == "evaluation_factors":
            result[field] = _merge_factors(items)
        elif field == "qualifications":
            result[field] = _merge_qualifications(items)
        else:
            result[field] = _dedupe(items, lambda item, f=field: _dedupe_key(f, item))

    result["other_requirements"] = _as_str_list(flattened.get("other_requirements"))
    bid_format_value = (
        flattened.get("bid_format")
        or flattened.get("bid_format_items")
        or flattened.get("response_format_items")
    )
    result["bid_format"] = _normalize_bid_format(bid_format_value)
    return result


def empty_extraction() -> dict[str, Any]:
    result: dict[str, Any] = {field: "" for field in SCALAR_FIELDS}
    for field in (
        "evaluation_factors",
        "preliminary_review",
        "qualifications",
        "key_dates",
        "contract_points",
        "technical_requirements",
        "other_requirements",
    ):
        result[field] = []
    result["bid_format"] = {"items": []}
    return result


def merge_extractions(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge normalized results: scalars keep first value; lists de-duplicate."""
    for field in SCALAR_FIELDS:
        value = extra.get(field)
        if value and not base.get(field):
            base[field] = value

    for field in (
        "evaluation_factors",
        "preliminary_review",
        "qualifications",
        "key_dates",
        "contract_points",
        "technical_requirements",
    ):
        combined = [*(base.get(field) or []), *[item for item in (extra.get(field) or []) if isinstance(item, dict)]]
        if field == "evaluation_factors":
            base[field] = _merge_factors(combined)
        elif field == "qualifications":
            base[field] = _merge_qualifications(combined)
        else:
            base[field] = _dedupe(
                combined,
                lambda item, f=field: _dedupe_key(f, item),
            )

    combined_other = [
        *(base.get("other_requirements") or []),
        *[item for item in (extra.get("other_requirements") or []) if item],
    ]
    base["other_requirements"] = list(dict.fromkeys(combined_other))

    extra_items = (extra.get("bid_format") or {}).get("items") or []
    base_items = (base.get("bid_format") or {}).get("items") or []
    base["bid_format"] = {
        "items": _merge_bid_items(
            [*base_items, *[item for item in extra_items if isinstance(item, dict)]]
        )
    }
    return base


def grounding_text(markdown: str) -> str:
    return normalize_ws(PAGE_MARKER_RE.sub(" ", markdown))


def _quote_is_grounded(quote: str, corpus: str) -> bool:
    lines = [line for line in (quote or "").splitlines() if line.strip()]
    if not lines:
        return False
    return all(normalize_ws(line) in corpus for line in lines)


def ground_extraction(extraction: dict[str, Any], markdown: str) -> list[dict[str, Any]]:
    """Report values that cannot be found verbatim in the source Markdown."""
    corpus = grounding_text(markdown)
    ungrounded: list[dict[str, Any]] = []

    def add(field: str, value: str, quote: str = "") -> None:
        if not value:
            return
        if quote:
            if _quote_is_grounded(quote, corpus):
                return
        elif normalize_ws(value) in corpus:
            return
        ungrounded.append({"field": field, "value": value, "source_quote": quote})

    for field in (
        "project_name",
        "tender_number",
        "budget_amount_text",
        "bid_deadline",
        "bid_opening_time",
        "bid_opening_place",
    ):
        add(field, extraction.get(field, ""))

    for index, item in enumerate(extraction.get("evaluation_factors") or []):
        add(
            f"evaluation_factors[{index}].source_quote",
            item.get("factor", ""),
            item.get("source_quote", ""),
        )

    for index, item in enumerate(extraction.get("qualifications") or []):
        add(
            f"qualifications[{index}].source_quote",
            item.get("requirement", ""),
            item.get("source_quote", ""),
        )
        for threshold in item.get("thresholds") or []:
            add(
                f"qualifications[{index}].thresholds",
                str(threshold),
                item.get("source_quote", ""),
            )

    for index, item in enumerate(extraction.get("key_dates") or []):
        add(f"key_dates[{index}].time", item.get("time", ""), item.get("source_quote", ""))

    for index, item in enumerate(extraction.get("contract_points") or []):
        add(
            f"contract_points[{index}].source_quote",
            item.get("detail", ""),
            item.get("source_quote", ""),
        )

    for index, item in enumerate(extraction.get("technical_requirements") or []):
        add(
            f"technical_requirements[{index}].source_quote",
            item.get("requirement", ""),
            item.get("source_quote", ""),
        )

    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in ungrounded:
        key = (item["field"], item["value"], item["source_quote"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped
