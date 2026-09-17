"""提示词模板库（Prompt as Code）。

把提示词当成受版本管理的代码资产，而不是散落在脚本里的字符串：
- 每条提示词固定「角色 / 任务 / 证据 / 输出结构 / 硬约束」五段式；
- **硬约束**：只能引用证据包里出现的数字、缺失就写"证据不足"、不得编造站点名与人名、
  必须给出责任方与建议时间点、输出 Markdown 且小节标题固定（便于机器校验与人工快速扫读）；
- 每条提示词都与 ``workflows.build_evidence`` 的证据包字段一一对应，
  改模板即可跑回归对比（见 tests/test_ai.py 的小节断言）。
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "1.2"

#: 所有提示词共用的角色设定与硬约束
SYSTEM_ANALYST = (
    "你是一名资深的一站式到家服务运营分析师（家电家居安装、充电桩、家庭维修、3C 服务、搬家），"
    "擅长把履约数据翻译成运营动作。你的读者是一线运营与站点负责人：他们不看长文，"
    "只看结论、责任方、动作和截止时间。"
)

HARD_RULES = """
硬约束（必须遵守）：
1. 只能使用「证据包」中出现的数字与名称，不得编造；证据不足时写「证据不足，需补充数据」。
2. 每个结论必须能被证据包中的指标或异常条目支撑，并在括号里标注依据（如：站点S03 派单时长P90 9.2h）。
3. 必须区分「事实」（数据说了什么）与「推断」（可能的原因），推断部分用「推测：」开头。
4. 改善动作要写到可执行粒度：动作 + 责任方（岗位）+ 建议完成时间。
5. 输出 Markdown，严格使用给定的小节标题，不要增删小节；总长度不超过 900 字。
""".strip()

#: 证据包字段说明（同时作为提示词的一部分，降低模型"自由发挥"的空间）
EVIDENCE_SCHEMA = {
    "meta": "数据口径：截止时间、覆盖天数、工单量、站点数",
    "overview": "全局指标：指标名 + 当前值 + 展示值",
    "scorecard": "履约质量综合得分与分项得分（时效/质量/体验）",
    "dimensions": "按站点/品类/城市的分层指标（含工单量）",
    "trend": "按周的趋势（最近 8 周）",
    "anomalies.dimension": "统计预警命中的维度级异常（含基线、当前值、偏离度、严重度、建议）",
    "anomalies.attributions": "对 Top 异常的归因下钻结果（贡献度排序 + 结论句）",
    "anomalies.rule": "工单级规则扫描结果（异常类型、命中量与严重度分布）",
    "feedback": "用户原声的主题分布、情感占比、典型原话",
    "sites": "站点档案：类型（直营/加盟）、城市、工程师数",
}


def evidence_digest(evidence: dict[str, Any], max_anomalies: int = 6, max_dimension_rows: int = 6, max_weeks: int = 8) -> str:
    """把证据包压缩成紧凑 JSON：只保留报告需要的部分，控制 token 与噪声。"""
    anomalies = evidence.get("anomalies", {})
    compact = {
        "meta": evidence.get("meta", {}),
        "overview": evidence.get("overview", {}),
        "scorecard": evidence.get("scorecard", {}),
        "dimensions": {
            dim: rows[:max_dimension_rows] for dim, rows in (evidence.get("dimensions") or {}).items()
        },
        "trend": (evidence.get("trend") or [])[-max_weeks:],
        "anomalies": {
            "dimension": (anomalies.get("dimension") or [])[:max_anomalies],
            "attributions": (anomalies.get("attributions") or [])[:max_anomalies],
            "rule": {
                "follow_up_rate": (anomalies.get("rule") or {}).get("follow_up_rate"),
                "by_type": (anomalies.get("rule") or {}).get("by_type"),
                "by_severity": (anomalies.get("rule") or {}).get("by_severity"),
            },
        },
        "feedback": evidence.get("feedback", {}),
        "sites": evidence.get("sites", []),
    }
    return json.dumps(compact, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------- 周报


WEEKLY_SECTIONS = ["一、本周结论", "二、异常与归因", "三、用户声音", "四、下周动作"]

WEEKLY_TEMPLATE = """请基于下面的证据包，写一份**服务履约质量周报**。

写作要求：
- 第一段用 3 句话讲清楚：整体履约质量处于什么水平、最严重的问题是什么、影响面多大。
- 异常与归因部分：只讲证据包里严重度为"高"的异常，每条按「现象 → 数据依据 → 归因（下钻结论）→ 责任方与动作」四段写。
- 用户声音部分：引用证据包里的典型原话（原文照抄，不要改写），并给出主题占比。
- 下周动作部分：输出一个 Markdown 表格，列固定为 `动作 | 责任方 | 度量指标 | 完成时间`，不超过 5 行。

输出结构（小节标题必须完全一致）：
### {sections}

证据包（JSON）：
```json
{digest}
```
"""


def build_weekly_prompt(evidence: dict[str, Any]) -> tuple[str, str]:
    user = WEEKLY_TEMPLATE.format(sections=" / ".join(WEEKLY_SECTIONS), digest=evidence_digest(evidence))
    return SYSTEM_ANALYST + "\n" + HARD_RULES, user


# ---------------------------------------------------------------- 异常专报


ANOMALY_SECTIONS = ["一、异常清单", "二、归因分析", "三、处置建议"]

ANOMALY_TEMPLATE = """请基于下面的证据包，写一份**履约异常专报**（给运营值班同学和站点负责人）。

写作要求：
- 异常清单：Markdown 表格，列固定为 `严重度 | 维度 | 对象 | 指标 | 基线 → 当前 | 偏离`，按严重度排序，最多 6 行。
- 归因分析：对最严重的 2 条异常，引用证据包中的归因下钻结论，说明"是谁/哪个环节把指标拖下去的"，
  并明确区分事实与推测。
- 处置建议：每条异常给出「立即动作（24h 内）」与「机制动作（2 周内）」，写清责任方。
- 如果证据包中同一问题在不同维度重复出现（例如站点异常与城市异常同源），要合并说明，不要重复计数。

输出结构（小节标题必须完全一致）：
### {sections}

证据包（JSON）：
```json
{digest}
```
"""


def build_anomaly_prompt(evidence: dict[str, Any]) -> tuple[str, str]:
    user = ANOMALY_TEMPLATE.format(sections=" / ".join(ANOMALY_SECTIONS), digest=evidence_digest(evidence))
    return SYSTEM_ANALYST + "\n" + HARD_RULES, user


# ---------------------------------------------------------------- 用户反馈


FEEDBACK_SECTIONS = ["一、声音概览", "二、高频问题", "三、改善机会"]

FEEDBACK_TEMPLATE = """请基于下面的证据包中的 `feedback` 字段，写一份**用户反馈洞察**。

写作要求：
- 声音概览：负面占比、主题分布（按占比排序，保留 1 位小数）。
- 高频问题：每个主题用一句话概括用户到底在抱怨什么，并**原文引用** 1 条典型原话。
- 改善机会：为占比最高的 3 个主题各给 1 条可落地的改善建议（动作 + 责任方）。
- 注意：不要臆造主题，只使用证据包中出现的主题名称。

输出结构（小节标题必须完全一致）：
### {sections}

证据包（JSON）：
```json
{digest}
```
"""


def build_feedback_prompt(evidence: dict[str, Any]) -> tuple[str, str]:
    user = FEEDBACK_TEMPLATE.format(sections=" / ".join(FEEDBACK_SECTIONS), digest=evidence_digest(evidence))
    return SYSTEM_ANALYST + "\n" + HARD_RULES, user


# ---------------------------------------------------------------- 需求文档（BRD）


BRD_SECTIONS = [
    "一、背景与问题",
    "二、问题量化（数据依据）",
    "三、目标与衡量指标",
    "四、功能需求",
    "五、流程与角色",
    "六、验收标准",
    "七、风险与依赖",
]

BRD_TEMPLATE = """请基于下面的证据包，撰写一份**业务需求文档（BRD）草稿**，主题是：{topic}。

写作要求：
- 背景与问题：从履约数据出发说明为什么要做这个系统能力，不要写空话。
- 问题量化：必须引用证据包中的真实数字（指标名 + 数值 + 影响工单量）。
- 目标与衡量指标：给出「目标值 + 当前值 + 衡量口径」，指标必须来自证据包中已有的指标。
- 功能需求：用 `FR-1 / FR-2 ...` 编号，每条写「用户角色 + 场景 + 期望行为 + 优先级（P0/P1/P2）」。
- 流程与角色：用有序列表描述端到端流程，标出每个环节的责任角色与系统触点。
- 验收标准：可测试的判定条件（例如"上线后 4 周内该项指标从 X 改善到 Y"）。
- 风险与依赖：至少 3 条，区分「数据/系统/组织」三类。
- 整体口吻像一份可以直接进需求评审会的草稿，不留 TODO 占位符。

输出结构（小节标题必须完全一致）：
### {sections}

证据包（JSON）：
```json
{digest}
```
"""


def build_brd_prompt(evidence: dict[str, Any], topic: str) -> tuple[str, str]:
    user = BRD_TEMPLATE.format(topic=topic, sections=" / ".join(BRD_SECTIONS), digest=evidence_digest(evidence))
    return SYSTEM_ANALYST + "\n" + HARD_RULES, user


# ---------------------------------------------------------------- 智能派单解释


DISPATCH_SECTIONS = ["一、派单策略建议", "二、规则解释", "三、风险与兜底"]

DISPATCH_TEMPLATE = """请基于证据包与下面的**派单评分卡结果**，写一份「智能派单优化建议」。

评分卡结果（程序按规则算出，字段含义：站点、综合得分、当前派单时长P90、上门准时率、一次完工率、工程师数、直营/加盟）：
```json
{scorecard_rows}
```

写作要求：
- 派单策略建议：说明应按什么顺序、什么权重把工单分给站点/工程师（要能落到规则引擎可实现的形式）。
- 规则解释：解释每条规则的业务动机，并引用证据包里的指标作为依据。
- 风险与兜底：至少 3 条（例如技能标签不准、跨站派单导致成本上升、旺季产能不足），每条给出监控指标。

输出结构（小节标题必须完全一致）：
### {sections}

证据包（JSON）：
```json
{digest}
```
"""


def build_dispatch_prompt(evidence: dict[str, Any], scorecard_rows: list[dict[str, Any]]) -> tuple[str, str]:
    user = DISPATCH_TEMPLATE.format(
        scorecard_rows=json.dumps(scorecard_rows, ensure_ascii=False, indent=2, default=str),
        sections=" / ".join(DISPATCH_SECTIONS),
        digest=evidence_digest(evidence),
    )
    return SYSTEM_ANALYST + "\n" + HARD_RULES, user
