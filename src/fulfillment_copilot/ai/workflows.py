"""AI 工作流：证据包 → 运营报告。

设计思路（也是我对"AI 该怎么用在运营里"的答案）
------------------------------------------------
**把事实交给程序，把叙述交给模型。**

1. 程序负责"算得准"：指标口径统一、异常检测可复现、归因有贡献度排序；
2. 模型负责"讲得清"：把证据包写成有观点、有动作、有责任方的报告；
3. 两者之间用**结构化证据包**（evidence pack）解耦——换模型、换模板都不影响计算，
   模型不可用时自动回退到确定性模板，报告永远不会开天窗。

对应 JD「AI 应用探索」：数据分析、异常归因、流程诊断、需求文档撰写四个场景都有落地。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Sequence

from ..anomaly import detect_all
from ..config import Config
from ..dispatch import MIN_ENGINEER_ORDERS, build_dispatch_view
from ..feedback import analyze as analyze_feedback
from ..metrics import (
    METRICS,
    format_metric,
    overall_metrics,
    scorecard as build_scorecard,
    slice_by,
    weekly_trend,
)
from ..models import Order, Site
from ..report import (
    anomaly_table,
    attribution_block,
    dimension_table,
    feedback_block,
    md_table,
    overview_block,
    rule_table,
    scorecard_block,
    sparkline,
    trend_table,
)
from . import prompts
from .provider import LLMError, LLMProvider, get_provider, provider_banner

#: 周报核心指标
CORE_KEYS: tuple[str, ...] = (
    "order_count",
    "arrive_ontime_rate",
    "sla_achieve_rate",
    "first_fix_rate",
    "complaint_rate",
    "bad_review_rate",
    "dispatch_p90",
    "total_p90",
)

#: 分层对比使用的指标
DIMENSION_KEYS: tuple[str, ...] = (
    "dispatch_p90",
    "arrive_ontime_rate",
    "first_fix_rate",
    "complaint_rate",
    "bad_review_rate",
)

#: 异常指标 → 改善动作剧本（动作, 责任方, 度量指标）
ACTION_PLAYBOOK: dict[str, tuple[str, str, str]] = {
    "dispatch_p90": ("核查派单规则与工程师排班缺口，恢复派单时效", "站点运营负责人 / 调度组", "派单时长P90"),
    "arrive_ontime_rate": ("收窄预约时段承诺，增加上门前 2 小时触达提醒", "预约履约组", "上门准时率"),
    "first_fix_rate": ("组织品类技能培训与备件前置，提升一次上门解决率", "质量与培训组", "一次完工率"),
    "rework_rate": ("返工工单专项复盘，纳入工程师质量档案", "质量与培训组", "返工率"),
    "complaint_rate": ("按客诉主题分派责任岗位，明确首响与升级时效", "客户体验组", "客诉率"),
    "bad_review_rate": ("差评工单 24 小时内回访，定位体验断点", "客户体验组", "差评率"),
    "avg_cost": ("复核结算单价与运力采购成本，排查费用漏洞", "结算与成本组", "单均成本"),
    "gross_margin": ("排查低价促销与异常补贴，复核成本项", "结算与成本组", "毛利率"),
    "finish_p90": ("推进现场作业标准化，复盘长尾工序", "服务标准组", "完工时长P90"),
    "total_p90": ("按环节定位链路瓶颈，逐单复盘超长工单", "流程优化组", "全链路时长P90"),
    "sla_achieve_rate": ("修订时效承诺与超时升级规则", "流程优化组", "全链路SLA达成率"),
}

#: BRD 主题预设：标题 + 关联指标 + 功能需求骨架
BRD_TOPICS: dict[str, dict[str, Any]] = {
    "dispatch": {
        "title": "智能派单优化：从「能派出去」到「派得准、派得快」",
        "metrics": ["dispatch_p90", "dispatch_ontime_rate", "arrive_ontime_rate", "first_fix_rate"],
        "features": [
            ("调度员 / 值班长", "高峰期大量工单同时进线，凭经验手工派单耗时长且不公平", "按「技能标签 + 距离 + 当前负载 + 历史一次完工率」自动打分排序，给出 Top3 推荐工程师并支持一键派单", "P0"),
            ("调度员", "工程师技能与工单品类不匹配，导致返工与二次上门", "建立工程师技能标签体系，派单时硬校验品类资质，标签缺失的工单进入人工池并提醒补录", "P0"),
            ("站点负责人", "站点产能不足时无人知晓，直到超时集中爆发", "按站点实时计算「待派工单 / 可用工程师」负载率，超过阈值自动预警并提示跨站支援", "P1"),
            ("运营分析师", "派单规则调整后无法评估效果", "记录派单策略版本与命中工单，支持按策略版本对比派单时长 P90 与准时率", "P1"),
        ],
        "process": [
            "用户下单 → 系统按品类/地址生成候选站点与工程师池（系统）",
            "派单引擎打分排序，输出 Top3 推荐（系统）",
            "调度员确认或改派，超时未确认则自动派给首位（调度员 / 系统）",
            "工程师接单，接单超时自动升级到值班长（工程师 / 值班长）",
            "站点负责人监控负载率，触发跨站支援（站点负责人）",
            "运营分析师按策略版本复盘效果，迭代权重（运营分析师）",
        ],
        "risks": [
            ("数据", "工程师技能标签历史缺失、准确率低，直接影响推荐质量", "先做标签补录专项，标签覆盖率纳入站点考核"),
            ("系统", "跨站派单会拉高上门时长与差旅成本，可能得不偿失", "设置跨站距离阈值，并监控单均成本与准时率的联动"),
            ("组织", "调度员担心被系统替代而消极使用", "定位为「推荐 + 可改派」，把调度员考核从派单量转为派单质量"),
        ],
    },
    "alert": {
        "title": "履约异常预警与闭环管理：从「事后救火」到「事前拦截」",
        "metrics": ["complaint_rate", "sla_achieve_rate", "bad_review_rate", "total_p90"],
        "features": [
            ("运营值班", "异常靠人工翻报表发现，平均发现时延以天计", "按站点/品类/指标做自动扫描，命中阈值即生成预警工单并推送责任人", "P0"),
            ("运营值班", "预警无分级，重要问题被淹没", "按「严重度 + 影响工单量」自动分级（高/中/低），高严重度强制 2 小时内响应", "P0"),
            ("站点负责人", "异常处理过程无记录，无法复盘", "异常工单全流程留痕：认领、处理动作、根因、验证结果、关闭", "P1"),
            ("运营分析师", "同类异常反复发生，缺乏机制沉淀", "异常闭环后自动生成复盘卡片，按月统计重复发生率并推动规则修订", "P2"),
        ],
        "process": [
            "系统按维度扫描指标，命中阈值生成预警（系统）",
            "按严重度分派到责任岗位并计时（系统 → 运营值班）",
            "责任人认领、执行处置动作并上传证据（站点负责人）",
            "验证指标是否回归基线，未回归则升级（运营值班 → 运营负责人）",
            "闭环后沉淀复盘卡片，修订运营规则或系统需求（运营分析师）",
        ],
        "risks": [
            ("数据", "指标口径不统一会导致误报，消耗一线信任", "口径集中定义在配置文件并版本化，变更需评审"),
            ("系统", "预警过多形成「预警疲劳」", "设置最小样本量与显著性门槛，并跟踪预警准确率"),
            ("组织", "责任人响应不及时", "把预警首响时效纳入站点服务质量考核"),
        ],
    },
    "appointment": {
        "title": "预约履约治理：降低改约率、提升上门准时率",
        "metrics": ["arrive_ontime_rate", "reschedule_rate", "complaint_rate", "dispatch_p90"],
        "features": [
            ("用户", "预约时段过宽（如「上午」），用户等待焦虑高", "支持更细的预约时段（2 小时粒度）并展示工程师预计到达时间", "P0"),
            ("用户 / 工程师", "工程师临时改约，用户被动接受", "改约需填写原因并触发用户侧补偿券与短信告知，改约次数纳入工程师考核", "P0"),
            ("调度员", "派单晚于预约时段才被发现", "预约时段前 T-4 小时校验派单状态，未派单自动预警", "P1"),
            ("运营分析师", "改约原因无结构化沉淀", "改约原因标准化枚举（人力/技能/备件/用户原因），按月输出帕累托分析", "P2"),
        ],
        "process": [
            "用户下单选择预约时段（用户）",
            "系统校验时段产能，满额则推荐邻近时段（系统）",
            "T-4 小时校验派单状态，未派单触发预警（系统 → 调度员）",
            "工程师上门前 2 小时触达用户确认（工程师）",
            "改约发生时填写原因并同步用户权益（工程师 / 系统）",
        ],
        "risks": [
            ("数据", "改约原因依赖一线填写，存在随意填写风险", "下拉枚举 + 抽查机制，异常集中的原因重点核查"),
            ("系统", "更细时段会降低单次上门效率", "按时段容量动态分配，避免过度碎片化"),
            ("组织", "压缩改约率可能导致工程师带病上门", "设置安全与合理的改约豁免场景"),
        ],
    },
}


# ---------------------------------------------------------------- 证据包


def build_evidence(
    orders: Sequence[Order],
    cfg: Config,
    sites: Optional[Sequence[Site]] = None,
    anchor: Optional[datetime] = None,
    ground_truth: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """构建报告与提示词共用的证据包。

    这是整个项目的中枢数据结构：所有报告（周报/专报/BRD）与 AI 提示词都只依赖它，
    因此"换模型不改计算、改计算不改模板"。
    """
    anchor = anchor or (max(o.order_time for o in orders) if orders else datetime.now())
    window_days = 84
    if orders:
        earliest = min(o.order_time for o in orders)
        window_days = max(1, (anchor - earliest).days)
    sites = list(sites or [])
    overview = overall_metrics(orders, cfg)
    dimension_metrics = {
        dim: slice_by(orders, dim, cfg, keys=DIMENSION_KEYS)
        for dim in ("站点", "品类", "城市", "站点类型")
    }
    trend = weekly_trend(orders, cfg, keys=("arrive_ontime_rate", "sla_achieve_rate", "dispatch_p90", "complaint_rate"))
    anomalies = detect_all(orders, cfg, anchor=anchor)
    feedback = analyze_feedback(orders)
    dispatch_view = build_dispatch_view(orders, cfg, sites) if orders else {"sites": [], "engineers": [], "rules": [], "engineer_count": 0}

    return {
        "meta": {
            "anchor": anchor.isoformat(timespec="minutes"),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window_days": window_days,
            "orders": len(orders),
            "sites": len(sites),
            "direct_sites": sum(1 for s in sites if s.is_direct),
            "categories": sorted({o.category for o in orders}),
            "prompt_version": prompts.PROMPT_VERSION,
        },
        "overview": {
            key: {
                "name": METRICS[key].name,
                "value": overview.get(key),
                "display": format_metric(key, overview.get(key)),
                "unit": METRICS[key].unit,
                "direction": METRICS[key].direction,
            }
            for key in METRICS
        },
        "scorecard": build_scorecard(overview, cfg),
        "dimensions": dimension_metrics,
        "trend": trend,
        "anomalies": anomalies,
        "feedback": feedback.to_dict(),
        "dispatch": dispatch_view,
        "sites": [
            {"site_id": s.site_id, "site_name": s.site_name, "city": s.city, "site_type": s.site_type, "engineers": s.engineers}
            for s in sites
        ],
        "ground_truth": ground_truth or [],
    }


def next_actions(anomalies: Sequence[dict[str, Any]], anchor: datetime, limit: int = 5) -> list[dict[str, Any]]:
    """把 Top 异常翻译成「动作 / 责任方 / 度量指标 / 完成时间」——异常管理要能落地。"""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in anomalies:
        metric = item.get("metric")
        if metric in seen or metric not in ACTION_PLAYBOOK:
            continue
        seen.add(metric)
        action, owner, measure = ACTION_PLAYBOOK[metric]
        due = anchor + timedelta(days=7 if item.get("severity") == "高" else 14)
        rows.append(
            {
                "动作": f"{action}（对象：{item.get('dimension')}「{item.get('label') or item.get('value')}」）",
                "责任方": owner,
                "度量指标": f"{measure} 当前 {format_metric(metric, item.get('current_value'))}",
                "完成时间": due.date().isoformat(),
            }
        )
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------- 模型调用封装


def _compose(
    provider: Optional[LLMProvider],
    prompt_pair: tuple[str, str],
    draft: str,
    use_ai: bool = True,
) -> tuple[str, str]:
    """有模型就用模型改写，没有或失败就用确定性草稿（并注明原因）。"""
    provider = provider or get_provider()
    if not use_ai or not provider.available:
        return draft, "offline-template"
    system, user = prompt_pair
    user = f"{user}\n\n以下是程序按同一份证据包生成的初稿，请在此基础上改进（保持小节标题不变，不要删减小节）：\n\n{draft}"
    try:
        return provider.complete(system, user), provider.describe()
    except LLMError as error:
        return f"{draft}\n\n> ⚠️ 大模型生成失败（{error}），已自动回退到确定性模板版本。", "offline-fallback"


# ---------------------------------------------------------------- 周报


def weekly_report(
    evidence: dict[str, Any],
    provider: Optional[LLMProvider] = None,
    use_ai: bool = True,
) -> str:
    """服务履约质量周报（Markdown）。"""
    meta = evidence["meta"]
    anchor = datetime.fromisoformat(meta["anchor"])
    anomalies = evidence["anomalies"]["dimension"]
    actions = next_actions(anomalies, anchor)
    rule = evidence["anomalies"]["rule"]
    scorecard = evidence["scorecard"]

    top = anomalies[0] if anomalies else None
    weekly_counts = [row.get("orders") for row in evidence["trend"]]
    complaints_trend = sparkline([row.get("complaint_rate") for row in evidence["trend"]])
    ontiml_trend = sparkline([row.get("arrive_ontime_rate") for row in evidence["trend"]])

    conclusion_lines = [
        f"- 综合履约质量得分 **{scorecard.get('total')}（{scorecard.get('grade')} 级）**，"
        f"覆盖 {meta['orders']} 单、{meta['sites']} 个服务站点（直营 {meta['direct_sites']} 个）。",
        f"- 上门准时率 {format_metric('arrive_ontime_rate', evidence['overview']['arrive_ontime_rate']['value'])}，"
        f"全链路 SLA 达成率 {format_metric('sla_achieve_rate', evidence['overview']['sla_achieve_rate']['value'])}，"
        f"客诉率 {format_metric('complaint_rate', evidence['overview']['complaint_rate']['value'])}。",
        f"- 规则扫描出需跟进工单 {rule.get('follow_up_orders')} 单（占 {(rule.get('follow_up_rate') or 0) * 100:.1f}%）；"
        f"统计预警命中 **{len(anomalies)}** 项指标异常，其中高严重度 {sum(1 for a in anomalies if a.get('severity') == '高')} 项。",
    ]
    if top:
        conclusion_lines.append(
            f"- **本周最需关注**：{top.get('dimension')}「{top.get('label')}」{top.get('metric_name')} "
            f"由 {format_metric(top.get('metric'), top.get('baseline_value'))} 恶化到 "
            f"{format_metric(top.get('metric'), top.get('current_value'))}（{top.get('deviation', 0) * 100:+.1f}%），"
            f"涉及 {top.get('recent_orders')} 单。"
        )

    parts: list[str] = [
        f"# 服务履约质量周报（截至 {anchor.date().isoformat()}）",
        "",
        f"> 数据口径：最近 {meta['window_days']} 天 · {meta['orders']} 单 · "
        f"{'、'.join(meta['categories'])} · 站点 {meta['sites']} 个。",
        f"> {provider_banner(provider or get_provider())}",
        "",
        "## 一、本周结论",
        "",
        *conclusion_lines,
        "",
        scorecard_block(scorecard),
        "",
        "## 二、异常与归因",
        "",
        f"近 {meta['window_days']} 天的指标异常（按严重度排序）：",
        "",
        anomaly_table(anomalies, limit=8),
        "",
    ]

    attributions = evidence["anomalies"]["attributions"]
    if attributions:
        parts.append(f"### 重点异常归因（Top {min(3, len(attributions))}）")
        parts.append("")
        for item in attributions[:3]:
            parts.append(f"**{item.get('dimension')}「{item.get('value')}」· {item.get('metric_name')}**")
            parts.append("")
            parts.append(attribution_block(item))
            parts.append("")

    parts.extend(
        [
            "## 三、用户声音",
            "",
            feedback_block(evidence["feedback"]),
            "",
            "## 四、下周动作",
            "",
            md_table(["动作", "责任方", "度量指标", "完成时间"], [[a[k] for k in ("动作", "责任方", "度量指标", "完成时间")] for a in actions])
            if actions
            else "_（本周无需专项动作）_",
            "",
            "---",
            "",
            "### 附：核心指标与趋势",
            "",
            overview_block({k: evidence["overview"][k]["value"] for k in evidence["overview"]}, CORE_KEYS),
            "",
            f"周趋势（上门准时率 {ontiml_trend} / 客诉率 {complaints_trend}，共 {len(weekly_counts)} 周）：",
            "",
            trend_table(evidence["trend"], ("orders", "arrive_ontime_rate", "dispatch_p90", "complaint_rate")),
            "",
            "### 附：分层对比",
            "",
            "**站点**",
            "",
            dimension_table(
                evidence["dimensions"]["站点"],
                ("dispatch_p90", "arrive_ontime_rate", "first_fix_rate", "complaint_rate"),
                "站点",
            ),
            "",
            "**品类**",
            "",
            dimension_table(
                evidence["dimensions"]["品类"],
                ("dispatch_p90", "arrive_ontime_rate", "first_fix_rate", "complaint_rate"),
                "品类",
            ),
            "",
            "### 附：异常工单分类",
            "",
            rule_table(rule),
            "",
        ]
    )

    draft = "\n".join(parts)
    text, mode = _compose(provider, prompts.build_weekly_prompt(evidence), draft, use_ai)
    return text if mode == "offline-template" else text


# ---------------------------------------------------------------- 异常专报


def anomaly_briefing(
    evidence: dict[str, Any],
    provider: Optional[LLMProvider] = None,
    use_ai: bool = True,
) -> str:
    """异常专报：给值班同学和站点负责人的行动清单。"""
    meta = evidence["meta"]
    anomalies = evidence["anomalies"]["dimension"]
    anchor = datetime.fromisoformat(meta["anchor"])
    parts = [
        f"# 履约异常专报（{anchor.date().isoformat()}）",
        "",
        f"> {provider_banner(provider or get_provider())}",
        "",
        "## 一、异常清单",
        "",
        anomaly_table(anomalies, limit=12) if anomalies else "_（本期无命中异常）_",
        "",
        "## 二、归因分析",
        "",
    ]
    for item in evidence["anomalies"]["attributions"][:3]:
        parts.append(f"### {item.get('dimension')}「{item.get('value')}」· {item.get('metric_name')}")
        parts.append("")
        parts.append(attribution_block(item))
        suggestion = next(
            (a.get("suggestion") for a in anomalies if a.get("metric") == item.get("metric")),
            "",
        )
        if suggestion:
            parts.append(f"**建议动作**：{suggestion}")
            parts.append("")

    parts.extend(
        [
            "## 三、处置建议",
            "",
            md_table(
                ["动作", "责任方", "度量指标", "完成时间"],
                [[a[k] for k in ("动作", "责任方", "度量指标", "完成时间")] for a in next_actions(anomalies, anchor)],
            ),
            "",
            "### 附：需跟进工单分布",
            "",
            rule_table(evidence["anomalies"]["rule"]),
            "",
            f"严重度分布：高 {evidence['anomalies']['rule']['by_severity'].get('高')} 单 · "
            f"中 {evidence['anomalies']['rule']['by_severity'].get('中')} 单 · "
            f"低 {evidence['anomalies']['rule']['by_severity'].get('低')} 单（按工单最高严重度去重）",
            "",
        ]
    )
    draft = "\n".join(parts)
    text, _ = _compose(provider, prompts.build_anomaly_prompt(evidence), draft, use_ai)
    return text


# ---------------------------------------------------------------- 反馈洞察


def feedback_insight(
    evidence: dict[str, Any],
    provider: Optional[LLMProvider] = None,
    use_ai: bool = True,
) -> str:
    """用户反馈洞察报告。"""
    feedback = evidence["feedback"]
    parts = [
        f"# 用户反馈洞察（截至 {evidence['meta']['anchor'][:10]}）",
        "",
        f"> {provider_banner(provider or get_provider())}",
        "",
        "## 一、声音概览",
        "",
        feedback_block(feedback),
        "",
        "## 二、高频问题",
        "",
    ]
    for theme in (feedback.get("themes") or [])[:5]:
        examples = (feedback.get("examples") or {}).get(theme["theme"]) or []
        quote = f"　典型原声：{examples[0]}" if examples else ""
        parts.append(f"- **{theme['theme']}**：{theme['count']} 条（占负面 {theme['share'] * 100:.1f}%）。{quote}")
    parts.extend(["", "## 三、改善机会", ""])
    rows = []
    for theme in (feedback.get("themes") or [])[:3]:
        action, owner, _ = _theme_action(theme["theme"])
        rows.append([theme["theme"], action, owner])
    parts.append(md_table(["主题", "改善动作", "责任方"], rows))
    parts.append("")
    draft = "\n".join(parts)
    text, _ = _compose(provider, prompts.build_feedback_prompt(evidence), draft, use_ai)
    return text


def _theme_action(theme: str) -> tuple[str, str, str]:
    mapping = {
        "履约时效": ("优化预约时段容量与派单时效，增加上门前触达提醒", "预约履约组", "准时率"),
        "服务技能": ("按品类组织技能培训与备件前置，返工纳入质量档案", "质量与培训组", "一次完工率"),
        "费用争议": ("费用项前置公示，现场加价需系统留痕与用户确认", "结算与成本组", "费用类客诉率"),
        "服务态度": ("服务规范与服务话术培训，纳入质检抽检", "服务标准组", "态度类客诉率"),
        "改约派单": ("改约原因结构化，压缩改约率并同步用户权益", "调度组 / 客户体验组", "改约率"),
        "物品损坏": ("上门前防护提示与损坏理赔提速", "服务标准组 / 售后组", "损坏类客诉率"),
    }
    return mapping.get(theme, ("定位责任环节并制定专项改善", "运营负责人", "相关指标"))


# ---------------------------------------------------------------- 需求文档


def brd_draft(
    evidence: dict[str, Any],
    topic: str = "dispatch",
    provider: Optional[LLMProvider] = None,
    use_ai: bool = True,
) -> str:
    """由数据证据自动生成 BRD 草稿（对应 JD「参与业务需求文档撰写」）。"""
    preset = BRD_TOPICS.get(topic) or _generic_topic(evidence)
    meta = evidence["meta"]
    overview = evidence["overview"]
    anomalies = evidence["anomalies"]["dimension"]

    metric_rows = []
    for key in preset["metrics"]:
        item = overview.get(key, {})
        anomaly = next((a for a in anomalies if a.get("metric") == key), None)
        metric_rows.append(
            [
                item.get("name", key),
                item.get("display", "—"),
                item.get("direction") == "down_better" and "越小越好" or "越大越好",
                f"{anomaly.get('deviation', 0) * 100:+.1f}%" if anomaly else "—",
                anomaly.get("label") or "—" if anomaly else "—",
            ]
        )

    feature_rows = [
        [f"FR-{index + 1}", role, scenario, expectation, priority]
        for index, (role, scenario, expectation, priority) in enumerate(preset["features"])
    ]
    risk_rows = [[kind, risk, response] for kind, risk, response in preset["risks"]]
    process_rows = [[index + 1, step] for index, step in enumerate(preset["process"])]

    parts = [
        f"# 业务需求文档（BRD 草稿）：{preset['title']}",
        "",
        f"> 数据依据：最近 {meta['window_days']} 天 · {meta['orders']} 单 · "
        f"{meta['sites']} 个站点；生成时间 {meta['generated_at']}。",
        f"> {provider_banner(provider or get_provider())}",
        "",
        "## 一、背景与问题",
        "",
        f"服务履约覆盖 {'、'.join(meta['categories'])} 等场景，全链路包含下单、预约、派单、上门、完工、评价六个环节。"
        f"当前抽样 {meta['orders']} 单中，需跟进异常工单 "
        f"{evidence['anomalies']['rule'].get('follow_up_orders')} 单"
        f"（占 {(evidence['anomalies']['rule'].get('follow_up_rate') or 0) * 100:.1f}%），"
        f"统计预警命中 {len(anomalies)} 项指标异常。"
        "异常集中在少数维度上，说明问题并非随机波动，而是流程与系统能力存在缺口。",
        "",
        "## 二、问题量化（数据依据）",
        "",
        md_table(["关联指标", "当前值", "优化方向", "偏离基线", "异常集中对象"], metric_rows),
        "",
    ]
    if anomalies:
        top = anomalies[0]
        parts.append(
            f"最典型的证据：{top.get('message')}（涉及 {top.get('recent_orders')} 单，"
            f"建议动作：{top.get('suggestion')}）。"
        )
        parts.append("")

    parts.extend(
        [
            "## 三、目标与衡量指标",
            "",
            md_table(
                ["目标", "衡量指标", "当前值", "目标值", "评估周期"],
                _goal_rows(preset["metrics"], overview),
            ),
            "",
            "## 四、功能需求",
            "",
            md_table(["编号", "角色", "场景", "期望行为", "优先级"], feature_rows),
            "",
            "## 五、流程与角色",
            "",
            md_table(["序号", "环节（责任角色）"], process_rows),
            "",
            "## 六、验收标准",
            "",
        ]
    )
    for key in preset["metrics"][:3]:
        item = overview.get(key, {})
        target = _target_for(key)
        parts.append(
            f"- 「{item.get('name', key)}」：上线后 4 周内由当前 {item.get('display', '—')} 改善到 {target}；"
            "按站点与品类分别验收，达标口径与该指标在监控体系中的定义一致。"
        )
    parts.extend(
        [
            "- 需求上线后，相关改善动作的责任方与完成时间必须在异常工单系统中留痕，可追溯。",
            "- 预警类功能的准确率不低于 80%（人工判定口径），误报率过高需回退阈值并复盘。",
            "",
            "## 七、风险与依赖",
            "",
            md_table(["类型", "风险", "应对"], risk_rows),
            "",
            "---",
            "",
            "本文件由 `fulfillment_copilot.ai.workflows.brd_draft` 依据真实指标自动生成草稿，"
            "供需求评审会使用；正式立项前需补充成本测算与排期。",
            "",
        ]
    )

    draft = "\n".join(parts)
    text, _ = _compose(provider, prompts.build_brd_prompt(evidence, preset["title"]), draft, use_ai)
    return text


def _generic_topic(evidence: dict[str, Any]) -> dict[str, Any]:
    """没有预设主题时，用当前最严重的异常自动拼一个主题。"""
    anomalies = evidence["anomalies"]["dimension"]
    if not anomalies:
        return BRD_TOPICS["alert"]
    top = anomalies[0]
    metric = top.get("metric")
    key = top.get("dimension")
    return {
        "title": f"{key}「{top.get('label')}」{top.get('metric_name')}专项治理",
        "metrics": [metric],
        "features": [
            ("运营分析师", f"{key}维度指标无法及时发现异常", "按维度自动扫描并分级预警，命中阈值生成工单", "P0"),
            ("站点负责人", "异常处理过程无法追溯", "异常工单全流程留痕并强制时限", "P1"),
        ],
        "process": ["系统扫描生成预警", "分派责任岗位并计时", "处置并验证指标回归", "复盘沉淀规则"],
        "risks": [
            ("数据", "口径不一致导致误报", "口径集中定义并版本化"),
            ("系统", "预警过多形成疲劳", "设置最小样本量与显著性门槛"),
            ("组织", "责任人响应不及时", "纳入站点服务考核"),
        ],
    }


def _target_for(key: str) -> str:
    from ..metrics import DEFAULT_TARGETS

    target = DEFAULT_TARGETS.get(key)
    if target is None:
        return "较基线改善 20%"
    return format_metric(key, target)


def _goal_rows(keys: Sequence[str], overview: dict[str, Any]) -> list[list[str]]:
    rows = []
    for key in keys:
        item = overview.get(key, {})
        rows.append(
            [
                f"改善 {item.get('name', key)}",
                item.get("name", key),
                item.get("display", "—"),
                _target_for(key),
                "上线后 4 周",
            ]
        )
    return rows


# ---------------------------------------------------------------- 智能派单建议


#: 派单优化的兜底风险清单（含监控指标与兜底动作）
DISPATCH_RISKS: tuple[tuple[str, str, str], ...] = (
    ("工程师技能标签历史缺失或不准，推荐质量不可控", "标签覆盖率、返工率、一次完工率", "标签缺失工单强制进入人工调度池，并推送补录提醒"),
    ("跨站派单拉高上门时长与差旅成本，得不偿失", "单均成本、上门准时率", "设置跨站距离与成本阈值，跨城派单需值班长审批"),
    ("优先池工程师被高复杂度工单占满，普通工单反而排队", "优先池当日负载、待派工单量", "为优先池保留产能上限，超出后自动降级到常规池"),
    ("旺季产能缺口集中爆发，规则失效", "待派工单/在线工程师比值", "提前 7 天预警并锁定支援运力，必要时放宽时段承诺并同步用户"),
)


def dispatch_advice(
    evidence: dict[str, Any],
    provider: Optional[LLMProvider] = None,
    use_ai: bool = True,
) -> str:
    """智能派单优化建议：可解释评分卡 + 规则建议（+ 可选的大模型叙述）。"""
    view = evidence.get("dispatch") or {}
    site_rows = view.get("sites") or []
    engineer_rows = view.get("engineers") or []
    rules = view.get("rules") or []
    anchor = evidence["meta"]["anchor"][:10]

    def pct(key: str, value: Any) -> str:
        return format_metric(key, value)

    site_table_rows = [
        [
            row["site_name"],
            row["site_type"],
            row["city"],
            row["engineers"],
            row["orders"],
            row["orders_per_engineer_day"],
            f"{row['score']}（{row['grade']}）" if row.get("score") is not None else "—",
            pct("arrive_ontime_rate", row.get("arrive_ontime_rate")),
            pct("first_fix_rate", row.get("first_fix_rate")),
            pct("complaint_rate", row.get("complaint_rate")),
            row["recommendation"],
        ]
        for row in site_rows
    ]
    engineer_table_rows = [
        [
            row["engineer_id"],
            row["site_id"],
            row["orders"],
            pct("arrive_ontime_rate", row.get("arrive_ontime_rate")),
            pct("first_fix_rate", row.get("first_fix_rate")),
            pct("rework_rate", row.get("rework_rate")),
            pct("complaint_rate", row.get("complaint_rate")),
            row.get("quality_score"),
            row["advice"],
        ]
        for row in engineer_rows
    ]

    parts: list[str] = [
        f"# 智能派单优化建议（截至 {anchor}）",
        "",
        f"> {provider_banner(provider or get_provider())}",
        "> 说明：派单建议采用**可解释评分卡 + 规则化建议**，不使用黑盒模型——"
        "上线后的规则必须能被调度员理解、申诉与审计。",
        "",
        "## 一、派单策略建议",
        "",
        "**站点派单评分卡**（得分口径与履约质量综合得分一致，越高越适合优先承接）",
        "",
        md_table(
            ["站点", "类型", "城市", "工程师", "工单量", "单/人/天", "综合得分", "上门准时率", "一次完工率", "客诉率", "派单建议"],
            site_table_rows,
        ),
        "",
        f"**工程师画像**（样本量 ≥ {MIN_ENGINEER_ORDERS} 单；共 {view.get('engineer_count', 0)} 名工程师参与评估，展示前 {len(engineer_rows)} 名）",
        "",
        md_table(
            ["工程师", "站点", "工单量", "上门准时率", "一次完工率", "返工率", "客诉率", "质量得分", "派单建议"],
            engineer_table_rows,
        ),
        "",
        "## 二、规则解释",
        "",
        md_table(
            ["优先级", "规则", "触发条件", "动作", "数据依据"],
            [[r.get("priority"), r.get("name"), r.get("trigger"), r.get("action"), r.get("evidence")] for r in rules],
        ),
        "",
        "## 三、风险与兜底",
        "",
        md_table(["风险", "监控指标", "兜底动作"], [list(row) for row in DISPATCH_RISKS]),
        "",
    ]

    draft = "\n".join(parts)
    text, _ = _compose(provider, prompts.build_dispatch_prompt(evidence, site_rows), draft, use_ai)
    return text
