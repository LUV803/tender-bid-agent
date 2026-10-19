"""Prompts and focus routing for structured tender extraction."""

from __future__ import annotations

PROMPT_VERSION = "2026-10-19.1"

FOCUS_LABELS = {
    "scoring": "评标办法与评分标准",
    "format": "投标文件格式",
    "qualification": "资格条件与投标人须知",
    "contract": "合同条款",
    "technical": "技术要求与采购需求",
    "general": "项目基本信息",
}

FOCUS_DESCRIPTIONS = {
    "scoring": (
        "本单元属于评标/评审内容。优先抽取：评标方法（综合评估法/经评审的最低投标价法等）、"
        "初步评审（形式评审、资格评审、响应性评审）的全部评审因素与标准、"
        "详细评审的评分因素、类别、分值上限、评分标准。评分表要逐行拆分，不要把整张表合并成一条。"
        "max_score 填该因素的分值（数字），无法确定填 null。同时记录关键时间节点。"
    ),
    "format": (
        "本单元属于投标文件格式内容。优先抽取：投标文件需要提供的全部格式文件条目"
        "（投标函、报价表、法定代表人身份证明、授权委托书、承诺函、各类附表等）。"
        "每个条目的 title 用原文标题，requirements 写该格式的填写/签章/份数要求；"
        "空白模板也要列出，不要遗漏。"
    ),
    "qualification": (
        "本单元属于投标人须知/资格条件内容。优先抽取：投标人资格要求（资质、财务、业绩、信誉、人员）、"
        "是否接受联合体、投标保证金、关键时间节点（获取文件、答疑、递交、开标）、"
        "以及项目名称、招标编号、预算、标段等基本信息。资格条件要逐条拆分，"
        "thresholds 列出明确门槛，source_quote 保留原文依据。"
    ),
    "contract": (
        "本单元属于合同条款内容。优先抽取：付款方式与节点、履约担保、质量/工期/服务期要求、"
        "违约责任、验收、争议解决等合同要点，并记录关键时间节点。"
    ),
    "technical": (
        "本单元属于技术要求/采购需求内容。优先抽取：技术参数、服务范围、质量标准、"
        "工期或交付期、工程量清单/采购清单中的关键项目，并记录关键时间节点。"
    ),
    "general": (
        "优先抽取项目名称、招标编号、招标人、预算/最高限价、标段、关键时间节点与地点；"
        "如果原文出现资格、评分、格式或合同要求，也可以一并抽取。"
    ),
}

_SCORING_HINTS = ("评标", "评审", "评分", "打分")
_FORMAT_HINTS = ("投标文件格式",)
_TECHNICAL_HINTS = (
    "技术标准",
    "技术条款",
    "技术需求",
    "技术要求",
    "采购需求",
    "工程量清单",
    "施工组织设计",
    "设计任务书",
)
_CONTRACT_HINTS = ("合同",)
_QUALIFICATION_HINTS = ("投标人须知", "资格")


def route_focus(path: str) -> str:
    """Choose the extraction focus for a section breadcrumb."""
    text = path or ""
    if any(hint in text for hint in _SCORING_HINTS):
        return "scoring"
    if any(hint in text for hint in _FORMAT_HINTS):
        return "format"
    if any(hint in text for hint in _TECHNICAL_HINTS):
        return "technical"
    if any(hint in text for hint in _CONTRACT_HINTS):
        return "contract"
    if any(hint in text for hint in _QUALIFICATION_HINTS):
        return "qualification"
    return "general"


SYSTEM_PROMPT = """你是投标文件结构化抽取助手。你的任务是从招标文件章节原文中抽取可核查的结构化信息。

规则：
1. 只输出一个合法 JSON 对象，不要输出 Markdown 代码块、解释或思考过程。
2. 所有内容必须来自给定原文，严禁根据常识补充或编造。
3. source_quote 必须逐字复制原文中一段连续文字（建议 20-200 字），不要改写、不要加省略号。原文中的 <!-- page: N --> 是页码标记，不要写进 source_quote。
4. 原文没有的信息就省略该字段或填空字符串/空数组，不要猜测。
5. 表格内容按行拆分；同一评分因素的不同档位写在同一项的 criteria 中。
6. 金额、日期、分值等保留原文表述；max_score 只填数字（无法确定用 null）。
7. 字段名必须使用给定 JSON 结构中的英文键名。"""


SCHEMA_GUIDE = """{
  "project_name": "项目名称",
  "tender_number": "招标编号",
  "project_scale": "建设规模/采购规模",
  "bid_section": "标段名称",
  "budget_amount_text": "预算/最高限价（原文表述）",
  "bid_deadline": "投标截止时间（原文表述）",
  "bid_opening_time": "开标时间（原文表述）",
  "bid_opening_place": "开标地点",
  "evaluation_method": "评标方法",
  "evaluation_factors": [
    {"category": "商务|技术|报价|其他", "factor": "评分因素", "max_score": 0, "criteria": "评分标准", "source_quote": "原文"}
  ],
  "preliminary_review": [
    {"stage": "形式评审|资格评审|响应性评审|其他", "item": "评审因素", "criteria": "评审标准", "source_quote": "原文"}
  ],
  "qualifications": [
    {"category": "资质|财务|业绩|信誉|人员|其他", "requirement": "资格要求", "thresholds": ["明确门槛"], "source_quote": "原文"}
  ],
  "bid_format": {
    "items": [{"title": "格式文件名称", "requirements": "填写/签章/份数等要求"}]
  },
  "key_dates": [
    {"event": "事项", "time": "时间（原文表述）", "source_quote": "原文"}
  ],
  "contract_points": [
    {"topic": "合同要点", "detail": "内容", "source_quote": "原文"}
  ],
  "technical_requirements": [
    {"topic": "需求项", "requirement": "技术/服务要求", "source_quote": "原文"}
  ],
  "other_requirements": ["其他明确要求"]
}"""


def build_user_prompt(
    *,
    title: str,
    path: list[str],
    text: str,
    focus: str,
) -> str:
    breadcrumb = " > ".join([*path, title]) if title else " > ".join(path)
    focus_description = FOCUS_DESCRIPTIONS.get(focus, FOCUS_DESCRIPTIONS["general"])
    return f"""章节：{breadcrumb or "全文"}
抽取重点：{focus_description}

输出 JSON 结构（只输出对象本身；没有依据的字段可省略）：
{SCHEMA_GUIDE}

章节原文：
<<<
{text}
>>>"""


def build_repair_prompt(raw_content: str) -> str:
    """Ask for a JSON-only repair pass when the first response is malformed."""
    return f"""下面的内容本应是 JSON 对象，但无法解析。请在保留原意和原文依据的前提下，修复为合法 JSON。
只输出 JSON 对象，不要输出解释或代码块。

待修复内容：
<<<
{raw_content[:12000]}
>>>"""
