"""Proposal outline, section roles and the evaluation response matrix.

The canonical outline is deterministic so a failed or over-creative analyst can
never remove mandatory bid sections.  The analyst agent may add a small number
of tender-specific sections and enrich section instructions, but it cannot
delete or rename the base structure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..parsing.structure import normalize_ws
from .tender import TenderContext, tender_placeholder

ROLE_LABELS: dict[str, str] = {
    "admin": "格式与资格文件",
    "commercial": "商务文件",
    "technical": "技术文件",
}
ROLES = tuple(ROLE_LABELS)

_EXTRA_TITLE_RE = re.compile(r"[【】\r\n\t]+")


@dataclass(slots=True)
class BidSection:
    id: str
    title: str
    role: str
    purpose: str
    instructions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    word_target: int = 1000
    tables: list[str] = field(default_factory=list)
    origin: str = "base"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "role": self.role,
            "purpose": self.purpose,
            "instructions": list(self.instructions),
            "keywords": list(self.keywords),
            "word_target": self.word_target,
            "tables": list(self.tables),
            "origin": self.origin,
        }


@dataclass(slots=True)
class BidOutline:
    sections: list[BidSection]
    risks: list[str] = field(default_factory=list)
    strategy: list[dict[str, str]] = field(default_factory=list)
    project_summary: str = ""
    warnings: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, BidSection]:
        return {section.id: section for section in self.sections}

    def for_role(self, role: str) -> list[BidSection]:
        return [section for section in self.sections if section.role == role]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_summary": self.project_summary,
            "risks": list(self.risks),
            "strategy": list(self.strategy),
            "sections": [section.to_dict() for section in self.sections],
            "warnings": list(self.warnings),
        }


def canonical_sections() -> list[BidSection]:
    """The minimum complete bid structure for an engineering tender."""
    return [
        BidSection(
            id="bid_letter",
            title="投标函及投标函附录",
            role="admin",
            purpose="按招标文件第八章的格式填写投标函及投标函附录，明确报价、工期、质量、投标有效期、保证金和项目经理等关键承诺。",
            instructions=[
                "优先参照招标文件“投标文件格式”中的投标函原文结构和顺序。",
                "投标总报价、工期、投标有效期、投标保证金、违约金等必须由投标人决策的字段一律写占位符，不得用合同估算价或预算金额代替报价。",
                "投标函附录采用表格列出填写项，已能从投标人资料确认的事实原样引用，未知项写【待填写：...】。",
            ],
            keywords=[
                "投标函",
                "投标函附录",
                "投标总报价",
                "投标有效期",
                "投标保证金",
                "工期",
                "工程质量",
                "项目经理",
                "招标人",
            ],
            word_target=1200,
            tables=["bid_letter_appendix"],
        ),
        BidSection(
            id="authorization",
            title="法定代表人身份证明、授权委托书及资格声明",
            role="admin",
            purpose="提供法定代表人身份证明、授权委托书、资格审查自审及廉政声明等签署类文件，列明需签字盖章和扫描上传的材料。",
            instructions=[
                "保留法定代表人、委托代理人、身份证号、委托期限、签署日期等占位符，不得生成身份证号、签名、印章或社保信息。",
                "按招标文件格式要求说明电子印章、签字和原件扫描件的提交方式。",
                "资格声明只作程序性响应，不替投标人作出未经资料支持的事实陈述。",
            ],
            keywords=[
                "法定代表人身份证明",
                "授权委托书",
                "委托代理人",
                "委托期限",
                "资格审查自审表",
                "合格性",
                "廉政声明",
                "电子印章",
                "原件扫描件",
            ],
            word_target=1100,
            tables=["company_identity"],
        ),
        BidSection(
            id="company_qualification",
            title="投标人基本情况、资质与信用",
            role="commercial",
            purpose="证明投标人主体资格、施工资质、信用状况、管理体系认证和安全生产许可满足招标文件要求。",
            instructions=[
                "逐项对照资格条件说明满足情况，引用招标文件资格条款；不得推断资质等级、证书编号、信用等级或有效期。",
                "投标人资料中没有的事实写【待填写：...】，不得用行业常见值补全。",
                "说明证明材料扫描件在投标文件中的位置，附表由系统按照投标人资料自动生成。",
            ],
            keywords=[
                "投标人基本情况",
                "营业执照",
                "施工资质",
                "统一社会信用代码",
                "法定代表人",
                "信用等级",
                "信用中国",
                "失信被执行人",
                "质量管理体系",
                "环境管理体系",
                "职业健康安全管理体系",
                "安全生产许可证",
            ],
            word_target=1200,
            tables=["company_basic", "qualifications", "credit", "management_systems", "safety"],
        ),
        BidSection(
            id="finance_performance_personnel",
            title="财务状况、类似项目业绩与项目管理机构",
            role="commercial",
            purpose="响应财务、业绩和人员评分因素，说明近三年财务、类似项目经历以及项目管理团队和关键人员的资格。",
            instructions=[
                "财务数据、合同金额、开竣工日期、人员姓名、证书编号必须逐字引用投标人资料；资料缺失时写占位符。",
                "只列与招标文件类似项目定义相符或投标人已提供的业绩，不编造项目名称、发包人或验收情况。",
                "项目管理机构说明岗位设置、职责和到岗承诺，并与招标文件资格要求逐项对应。",
            ],
            keywords=[
                "财务状况",
                "审计报告",
                "营业收入",
                "净利润",
                "类似项目",
                "业绩",
                "合同协议书",
                "竣工验收",
                "项目管理机构",
                "项目经理",
                "建造师",
                "安全生产考核合格证书",
                "团队人员",
            ],
            word_target=1600,
            tables=["financials", "similar_projects", "personnel"],
        ),
        BidSection(
            id="price_commitments",
            title="投标报价说明与承诺事项",
            role="commercial",
            purpose="说明报价口径、报价文件组成和承诺事项，确保报价与招标文件、工程量清单及评标办法一致。",
            instructions=[
                "不得生成投标总报价、费率、下浮率、暂列金额或任何报价数字；这些字段全部用占位符。",
                "说明已标价工程量清单、造价工程师签章和电子印章要求，引用招标文件依据。",
                "承诺事项只覆盖招标文件明确要求的工期、质量、安全、环保、农民工工资、廉政、分包等事项；涉及具体金额或期限时写占位符。",
            ],
            keywords=[
                "投标报价",
                "工程量清单",
                "已标价",
                "报价",
                "合同估算价",
                "承诺",
                "农民工工资",
                "安全",
                "质量",
                "工期",
                "分包",
                "违约金",
                "预付款",
                "质量保证金",
            ],
            word_target=1400,
            tables=["price_summary"],
        ),
        BidSection(
            id="technical_overview",
            title="工程概况、编制依据与施工总体部署",
            role="technical",
            purpose="准确理解工程范围、现场条件和合同目标，形成总体施工部署，回应“内容完整性和编制水平”评分因素。",
            instructions=[
                "工程概况只使用招标文件、图纸说明和工程量清单中的事实；现场条件、地质、水电接引等未提供时写【待核对：...】。",
                "编制依据列明招标文件、设计图纸、现行标准规范及企业管理制度；没有依据的具体规范编号不得编造。",
                "总体部署包括组织机构、施工区段划分、总体流程、工期/质量/安全/环保目标和接口安排。",
            ],
            keywords=[
                "工程概况",
                "建设规模",
                "编制依据",
                "施工部署",
                "组织机构",
                "施工目标",
                "总体流程",
                "现场条件",
                "图纸",
                "工程量清单",
                "技术标准",
            ],
            word_target=1700,
        ),
        BidSection(
            id="technical_methods",
            title="施工方案与技术措施（含重点难点）",
            role="technical",
            purpose="针对本工程主要工序和复杂环节提出可实施的施工方法、技术措施和质量控制点，回应施工方案与技术措施评分因素。",
            instructions=[
                "围绕招标文件所列工程内容展开，优先覆盖基岩井打井、井房、高位水池、管网、道路、电气和工艺安装等实际范围。",
                "每个关键工序写施工顺序、主要方法、机械设备、质量控制、安全注意事项和验收要求；缺少设计参数时用【待核对：...】。",
                "单列重点难点及针对性措施，不得空泛堆砌；不得虚构地质条件、井深、管径、构筑物尺寸或设备型号。",
            ],
            keywords=[
                "施工方案",
                "技术措施",
                "施工方法",
                "关键工序",
                "复杂环节",
                "重点难点",
                "基岩井",
                "打井",
                "井房",
                "高位水池",
                "管网",
                "道路",
                "电气",
                "工艺安装",
                "测量放线",
                "试验检验",
            ],
            word_target=2400,
        ),
        BidSection(
            id="quality_management",
            title="质量管理体系与措施",
            role="technical",
            purpose="建立覆盖材料、工序、验收和缺陷整改的质量管理体系，回应质量管理体系与措施评分因素。",
            instructions=[
                "写明质量目标、组织机构与职责、材料设备进场检验、工序三检、隐蔽验收、试验检测、质量记录和缺陷整改闭环。",
                "质量目标若招标文件未明确，写【待核对：质量目标】或按招标文件要求引用，不得自行降低。",
                "措施应与本工程工序对应，避免只有制度名称没有具体做法。",
            ],
            keywords=[
                "质量管理体系",
                "质量目标",
                "质量保证",
                "质量控制",
                "材料",
                "试验",
                "检验",
                "隐蔽工程",
                "验收",
                "质量缺陷",
                "质量记录",
            ],
            word_target=1700,
        ),
        BidSection(
            id="safety_environment",
            title="安全管理、环境保护与文明施工",
            role="technical",
            purpose="建立安全生产和环境保护管理体系，落实危险源管控、文明施工、扬尘治理和水土保持措施，回应安全与环境评分因素。",
            instructions=[
                "安全管理覆盖责任体系、教育培训、危险源辨识、临时用电、机械设备、有限空间/高处/吊装等适用作业、应急响应和事故报告。",
                "环境保护覆盖扬尘、噪声、废水、固废、油污、水土保持和周边生态保护；结合水利工程和村庄周边环境提出具体措施。",
                "不得编造安全生产许可证编号、事故记录或认证证书编号；需要时使用投标人资料或占位符。",
            ],
            keywords=[
                "安全管理体系",
                "安全生产",
                "安全措施",
                "危险源",
                "临时用电",
                "机械设备",
                "环境保护",
                "文明施工",
                "扬尘",
                "噪声",
                "废水",
                "固废",
                "水土保持",
                "职业健康",
            ],
            word_target=2000,
        ),
        BidSection(
            id="schedule_resources",
            title="施工进度计划、资源配置与季节施工",
            role="technical",
            purpose="给出工期安排、关键线路、劳动力/设备/材料配置以及雨季、冬季和防汛度汛措施，回应进度、资源和季节施工评分因素。",
            instructions=[
                "以招标文件工期和关键时间为基础编排总进度、阶段里程碑和关键线路；工期缺失或无法确定时写【待核对：...】。",
                "资源配置用表格或清单说明岗位、工种、主要设备和试验检测资源，不虚构具体数量时可用区间或【待填写：...】。",
                "季节施工结合北京地区气候和水利工程特点，覆盖雨季、冬季、高温和大风天气以及防汛度汛。",
            ],
            keywords=[
                "施工进度",
                "进度计划",
                "工期",
                "里程碑",
                "关键线路",
                "资源配置",
                "劳动力",
                "设备",
                "材料",
                "机械",
                "季节施工",
                "雨季",
                "冬季",
                "防汛度汛",
            ],
            word_target=2000,
        ),
        BidSection(
            id="coordination_service",
            title="配合协调、防汛度汛与服务承诺",
            role="technical",
            purpose="说明与业主、监理、设计、村民及相邻单位的协调机制，以及防汛度汛、缺陷责任期和回访服务安排。",
            instructions=[
                "协调机制覆盖例会、报验、设计变更、村民沟通、投诉处理和界面移交，措施应可执行。",
                "防汛度汛与工程进度、安全管理衔接，明确组织、物资、值班和应急响应。",
                "服务承诺以招标文件合同条款为基础，涉及具体响应时限、保修年限或赔偿金额时写占位符，不得超出投标人资料擅自承诺。",
            ],
            keywords=[
                "配合",
                "协调",
                "业主",
                "监理",
                "设计",
                "村民",
                "接口",
                "防汛",
                "度汛",
                "缺陷责任",
                "保修",
                "服务承诺",
                "回访",
            ],
            word_target=1500,
        ),
    ]


def _clip_list(values: Any, *, limit: int = 8, chars: int = 300) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[:limit]:
        text = str(value).strip() if isinstance(value, str) else ""
        if text:
            result.append(text[:chars])
    return result


def _extra_sections(
    analysis: dict[str, Any],
    base: list[BidSection],
) -> tuple[list[BidSection], list[str]]:
    warnings: list[str] = []
    raw_extras = analysis.get("extra_sections")
    if not isinstance(raw_extras, list):
        return [], warnings
    existing_titles = {normalize_ws(section.title) for section in base}
    extras: list[BidSection] = []
    for raw in raw_extras[:3]:
        if not isinstance(raw, dict):
            continue
        title = _EXTRA_TITLE_RE.sub("", str(raw.get("title") or "")).strip()
        role = str(raw.get("role") or "").strip().lower()
        if not title or len(title) > 60 or role not in ROLES:
            warnings.append("分析师新增章节缺少有效标题或角色，已忽略")
            continue
        key = normalize_ws(title)
        if key in existing_titles or any(
            key in existing or existing in key for existing in existing_titles
        ):
            warnings.append(f"分析师新增章节“{title}”与现有章节重复，已忽略")
            continue
        existing_titles.add(key)
        try:
            word_target = int(raw.get("word_target") or 1000)
        except (TypeError, ValueError):
            word_target = 1000
        section = BidSection(
            id=f"x{len(extras) + 1}",
            title=title,
            role=role,
            purpose=str(raw.get("purpose") or "响应招标文件新增要求。").strip()[:600],
            instructions=_clip_list(raw.get("instructions"), limit=6),
            keywords=_clip_list(raw.get("keywords"), limit=12, chars=80),
            word_target=max(500, min(word_target, 2200)),
            origin="analyst",
        )
        extras.append(section)
    return extras, warnings


def build_outline(analysis: dict[str, Any], tender: TenderContext) -> BidOutline:
    base = canonical_sections()
    by_id = {section.id: section for section in base}

    briefs = analysis.get("section_briefs")
    if isinstance(briefs, dict):
        for section_id, value in briefs.items():
            section = by_id.get(str(section_id))
            if section is None or not isinstance(value, str):
                continue
            text = value.strip()
            if text:
                section.instructions.append(f"分析师提示：{text[:800]}")

    extras, warnings = _extra_sections(analysis, base)
    sections = list(base)
    for extra in extras:
        # Insert after the last base section with the same role, preserving the
        # conventional 资格 → 商务 → 技术 document order.
        last_index = max(
            (index for index, section in enumerate(sections) if section.role == extra.role),
            default=len(sections) - 1,
        )
        sections.insert(last_index + 1, extra)

    risks = _clip_list(analysis.get("risks"), limit=15, chars=600)
    strategy: list[dict[str, str]] = []
    raw_strategy = analysis.get("evaluation_strategy")
    if isinstance(raw_strategy, list):
        for item in raw_strategy[:80]:
            if not isinstance(item, dict):
                continue
            factor = str(item.get("factor") or "").strip()
            section_id = str(item.get("section_id") or "").strip()
            if factor and section_id in {section.id for section in sections}:
                strategy.append(
                    {
                        "factor": factor[:200],
                        "section_id": section_id,
                        "response_approach": str(item.get("response_approach") or "")[:600],
                    }
                )

    return BidOutline(
        sections=sections,
        risks=risks,
        strategy=strategy,
        project_summary=str(analysis.get("project_summary") or "")[:2000],
        warnings=warnings,
    )


def _default_targets(factor: str, category: str) -> list[str]:
    text = factor or ""
    mappings: list[tuple[tuple[str, ...], list[str]]] = [
        (("投标报价", "投标总价", "报价"), ["price_commitments"]),
        (("项目经理", "团队人员", "项目管理机构", "人员"), ["finance_performance_personnel"]),
        (("业绩",), ["finance_performance_personnel"]),
        (("财务",), ["finance_performance_personnel"]),
        (("信用",), ["company_qualification"]),
        # Quality factors often contain the substring "管理体系"; map them to
        # the technical quality chapter before the generic certification rule.
        (("质量",), ["quality_management"]),
        # Explicit certification factors belong to the qualification chapter,
        # but safety/environment *measures* belong to the technical chapter even
        # though their names also contain "管理体系".
        (("管理体系认证", "体系认证", "认证证书"), ["company_qualification"]),
        (("安全管理", "安全生产", "环境保护", "文明施工", "环保"), ["safety_environment"]),
        (("管理体系", "认证"), ["company_qualification"]),
        (("安全",), ["safety_environment"]),
        (("施工组织设计", "内容完整性", "编制水平"), ["technical_overview"]),
        (("施工方案", "技术措施"), ["technical_methods"]),
        (("进度",), ["schedule_resources"]),
        (("资源", "设备", "劳动力", "其它施工"), ["schedule_resources"]),
        (("季节",), ["schedule_resources"]),
        (("配合", "协调", "村民"), ["coordination_service"]),
    ]
    for keywords, targets in mappings:
        if any(keyword in text for keyword in keywords):
            return targets
    if category == "技术":
        return ["technical_overview", "technical_methods"]
    if category == "商务":
        return ["company_qualification", "finance_performance_personnel"]
    if category == "报价":
        return ["price_commitments"]
    return []


def build_response_matrix(
    outline: BidOutline,
    tender: TenderContext,
) -> list[dict[str, Any]]:
    """Map every extracted scoring factor to response sections.

    Analyst-provided mappings win when they point at a real section; otherwise
    a deterministic keyword mapping is used.  Unmapped factors remain visible
    with a ``【待核对：响应章节】`` marker instead of being hidden.
    """
    by_id = outline.by_id()
    strategy = {
        normalize_ws(item["factor"]): item["section_id"]
        for item in outline.strategy
        if item.get("section_id") in by_id
    }
    rows: list[dict[str, Any]] = []
    for factor in tender.evaluation_factors:
        name = str(factor.get("factor") or "").strip()
        if not name:
            continue
        category = str(factor.get("category") or "其他")
        targets = []
        mapped = strategy.get(normalize_ws(name))
        if mapped:
            targets = [mapped]
        else:
            targets = [section_id for section_id in _default_targets(name, category) if section_id in by_id]
        titles = [by_id[section_id].title for section_id in targets]
        rows.append(
            {
                "factor": name,
                "category": category,
                "max_score": factor.get("max_score"),
                "criteria": str(factor.get("criteria") or ""),
                "section_ids": targets,
                "section_titles": titles,
                "display": "、".join(titles) if titles else tender_placeholder("响应章节"),
            }
        )
    return rows
