"""Tests for tolerant extraction normalization, merging and grounding."""

from __future__ import annotations

from bidding.extraction.errors import ExtractError
from bidding.extraction.llm import parse_json_object
from bidding.extraction.schema import (
    empty_extraction,
    ground_extraction,
    infer_evaluation_method,
    merge_extractions,
    normalize_extraction,
)
from bidding.extraction.prompts import route_focus


def test_normalize_flattens_nested_aliases_and_drops_unknown_fields() -> None:
    raw = {
        "basic_info": {
            "project_name": "密云区供水工程",
            "tender_number": "T-001",
            "not_a_field": "ignored",
        },
        "evaluation": {
            "method": "综合评估法",
            "factors": [
                {
                    "name": "投标报价",
                    "score": 40,
                    "standard": "按公式计算",
                    "quote": "投标报价得分",
                }
            ],
        },
        "mystery": {"anything": True},
    }
    result = normalize_extraction(raw)
    assert result["project_name"] == "密云区供水工程"
    assert result["tender_number"] == "T-001"
    assert result["evaluation_method"] == "综合评估法"
    assert result["evaluation_factors"] == [
        {
            "category": "报价",
            "factor": "投标报价",
            "max_score": 40.0,
            "criteria": "按公式计算",
            "source_quote": "投标报价得分",
        }
    ]
    assert "mystery" not in result
    assert result["bid_format"] == {"items": []}


def test_duplicate_factors_merge_criteria_scores_and_quotes() -> None:
    raw = {
        "evaluation_factors": [
            {
                "factor": "施工组织设计",
                "category": "技术",
                "max_score": 10,
                "criteria": "内容完整得 6 分",
                "source_quote": "施工组织设计内容完整",
            },
            {
                "name": "施工组织设计",
                "max_score": 15,
                "criteria": "针对性强加 9 分",
                "source_quote": "施工组织设计针对性强",
            },
        ]
    }
    factors = normalize_extraction(raw)["evaluation_factors"]
    assert len(factors) == 1
    factor = factors[0]
    assert factor["max_score"] == 15.0
    assert "内容完整得 6 分" in factor["criteria"]
    assert "针对性强加 9 分" in factor["criteria"]
    assert factor["source_quote"].splitlines() == [
        "施工组织设计内容完整",
        "施工组织设计针对性强",
    ]


def test_blank_tender_templates_and_slash_thresholds_are_dropped() -> None:
    raw = {
        "qualifications": [
            {"requirement": "近/年须至少具有/项已完成/施工业绩"},
            {"requirement": "施工资质", "thresholds": ["/", "三级及以上"]},
            {"requirement": "投标人应提供近 3 年经审计财务会计报表"},
        ]
    }
    items = normalize_extraction(raw)["qualifications"]
    assert [item["requirement"] for item in items] == [
        "施工资质",
        "投标人应提供近 3 年经审计财务会计报表",
    ]
    assert items[0]["thresholds"] == ["三级及以上"]


def test_unscored_procedure_rows_are_not_evaluation_factors() -> None:
    raw = {
        "evaluation_factors": [
            {"factor": "评标方法", "criteria": "按第三章规定评审"},
            {"factor": "施工组织设计", "max_score": 30, "criteria": "暗标评审"},
            {"name": "投标报价", "score": "50分"},
        ]
    }
    factors = normalize_extraction(raw)["evaluation_factors"]
    assert [factor["factor"] for factor in factors] == ["施工组织设计", "投标报价"]


def test_bare_qualification_labels_are_dropped() -> None:
    raw = {
        "qualifications": [
            {"requirement": "财务要求"},
            {"requirement": "项目经理资格", "thresholds": []},
            {"requirement": "财务要求", "thresholds": ["近3年均盈利"]},
        ]
    }
    items = normalize_extraction(raw)["qualifications"]
    assert [item["requirement"] for item in items] == ["财务要求"]
    assert items[0]["thresholds"] == ["近3年均盈利"]


def test_key_dates_require_a_usable_time() -> None:
    raw = {
        "key_dates": [
            {"event": "投标截止时间", "time": "本章第4.2.1项规定"},
            {"event": "开标时间", "time": ""},
            {"event": "签订合同", "time": "接到本通知书后的   日内"},
            {"event": "退还保证金", "time": "收到撤回通知之日起5日内"},
            {"event": "开标", "time": "2023-11-14 13:30"},
        ]
    }
    items = normalize_extraction(raw)["key_dates"]
    assert [item["event"] for item in items] == ["退还保证金", "开标"]


def test_numbered_bid_format_titles_merge_with_unnumbered() -> None:
    raw = {
        "bid_format_items": [
            {"name": "投标函及投标函附录", "requirements": "加盖单位电子印章"},
            {"title": "一、投标函及投标函附录", "requirements": "按第八章格式编写"},
            {"title": "十、原件的扫描件"},
        ]
    }
    items = normalize_extraction(raw)["bid_format"]["items"]
    assert [item["title"] for item in items] == [
        "投标函及投标函附录",
        "十、原件的扫描件",
    ]
    assert "加盖单位电子印章" in items[0]["requirements"]
    assert "按第八章格式编写" in items[0]["requirements"]


def test_duplicate_qualifications_merge_thresholds() -> None:
    raw = {
        "qualification_items": [
            {
                "name": "施工资质",
                "category": "资质",
                "thresholds": "水利水电工程施工总承包三级",
                "source_quote": "具备水利水电工程施工总承包三级及以上资质",
            },
            {
                "requirement": "施工资质",
                "thresholds": ["具备有效的安全生产许可证"],
                "source_quote": "具备有效的安全生产许可证",
            },
        ]
    }
    items = normalize_extraction(raw)["qualifications"]
    assert len(items) == 1
    assert items[0]["category"] == "资质"
    assert items[0]["thresholds"] == [
        "水利水电工程施工总承包三级",
        "具备有效的安全生产许可证",
    ]


def test_bid_format_items_merge_by_title_and_keep_aliases() -> None:
    raw = {
        "bid_format_items": [
            {"name": "投标函", "requirements": "加盖单位公章"},
            {"title": "投标函", "requirements": "正本一份、副本四份"},
            "法定代表人身份证明",
        ]
    }
    items = normalize_extraction(raw)["bid_format"]["items"]
    assert [item["title"] for item in items] == ["投标函", "法定代表人身份证明"]
    assert "加盖单位公章" in items[0]["requirements"]
    assert "副本四份" in items[0]["requirements"]


def test_placeholder_scalars_are_dropped() -> None:
    result = normalize_extraction(
        {
            "project_name": "（项目名称）",
            "bid_section": "见投标人须知前附表",
            "bid_opening_time": "年  月  日  时  分",
            "evaluation_method": "未明确提供",
            "bid_opening_place": "（开标记录表中未明确）",
        }
    )
    for field in (
        "project_name",
        "bid_section",
        "bid_opening_time",
        "evaluation_method",
        "bid_opening_place",
    ):
        assert result[field] == ""


def test_merge_prefers_named_evaluation_method_over_cross_references() -> None:
    base = normalize_extraction(
        {"evaluation_method": "电子招标投标交易平台"}
    )
    generic = normalize_extraction(
        {"evaluation_method": "按照第三章“评标办法”规定的方法评审"}
    )
    merged = merge_extractions(base, generic)
    assert merged["evaluation_method"] == "按照第三章“评标办法”规定的方法评审"

    named = normalize_extraction({"evaluation_method": "综合评估法"})
    merged = merge_extractions(merged, named)
    assert merged["evaluation_method"] == "综合评估法"


def test_infer_evaluation_method_from_toc_title() -> None:
    titles = ["第一卷", "第三章 评标办法（综合评估法）"]
    assert infer_evaluation_method(titles, "见投标人须知前附表") == "综合评估法"
    assert (
        infer_evaluation_method(titles, "经评审的最低投标价法")
        == "经评审的最低投标价法"
    )
    assert infer_evaluation_method(["第一章 招标公告"], "") == ""


def test_merge_extractions_keeps_first_scalar_and_merges_lists() -> None:
    first = normalize_extraction(
        {
            "project_name": "项目 A",
            "qualifications": [{"requirement": "资质甲级", "thresholds": ["甲级"]}],
            "key_dates": [{"event": "开标", "time": "2024-01-01"}],
        }
    )
    second = normalize_extraction(
        {
            "project_name": "项目 B",
            "bid_deadline": "2024-02-02",
            "qualifications": [{"requirement": "资质甲级", "thresholds": ["乙级"]}],
            "key_dates": [{"event": "开标", "time": "2024-02-02"}],
        }
    )
    merged = merge_extractions(first, second)
    assert merged["project_name"] == "项目 A"
    assert merged["bid_deadline"] == "2024-02-02"
    assert len(merged["qualifications"]) == 1
    assert merged["qualifications"][0]["thresholds"] == ["甲级", "乙级"]
    # Conflicting times are kept rather than silently discarding one.
    assert len(merged["key_dates"]) == 2


def test_grounding_checks_each_line_of_combined_quotes() -> None:
    markdown = (
        "<!-- page: 1 -->\n"
        "投标人须具备水利水电工程施工总承包三级及以上资质。\n"
        "<!-- page: 2 -->\n"
        "项目负责人须具备二级建造师资格。"
    )
    extraction = empty_extraction()
    extraction["project_name"] = "不存在的项目"
    extraction["qualifications"] = [
        {
            "category": "资质",
            "requirement": "施工资质",
            "thresholds": [],
            "source_quote": (
                "投标人须具备水利水电工程施工总承包三级及以上资质。\n"
                "项目负责人须具备二级建造师资格。"
            ),
        },
        {
            "category": "人员",
            "requirement": "项目经理",
            "thresholds": [],
            "source_quote": "项目经理须具备一级建造师资格。",
        },
    ]
    extraction["preliminary_review"] = [
        {
            "stage": "形式评审",
            "item": "投标人名称",
            "criteria": "与营业执照一致",
            "source_quote": "投标人名称应与营业执照一致。",
        }
    ]
    ungrounded = ground_extraction(extraction, markdown)
    fields = {item["field"] for item in ungrounded}
    assert "project_name" in fields
    assert "qualifications[0].source_quote" not in fields
    assert "qualifications[1].source_quote" in fields
    assert "preliminary_review[0].source_quote" in fields


def test_parse_json_object_accepts_fences_and_chatty_prefixes() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('好的，结果如下：{"a": {"b": 2}} 请查收') == {
        "a": {"b": 2}
    }


def test_parse_json_object_rejects_non_objects_and_empty_output() -> None:
    for content in ("", "[1, 2]", "no json here"):
        try:
            parse_json_object(content)
        except ExtractError:
            pass
        else:  # pragma: no cover - failure path
            raise AssertionError(f"expected ExtractError for {content!r}")


def test_route_focus_priority() -> None:
    assert route_focus("第一卷 > 第三章 评标办法（综合评估法） > 2.评审标准") == "scoring"
    assert route_focus("第八章 投标文件格式") == "format"
    assert route_focus("第五章 技术标准和要求") == "technical"
    assert route_focus("第四章 合同条款及格式") == "contract"
    assert route_focus("第二章 投标人须知") == "qualification"
    assert route_focus("第一章 招标公告") == "general"
