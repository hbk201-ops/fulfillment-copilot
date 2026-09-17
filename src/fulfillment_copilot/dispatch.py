"""智能派单建议：可解释的评分卡 + 规则化建议。

对应 JD「探索 AI 在智能调度、异常预警等场景的应用」。这里刻意**不训练黑盒模型**，
而是先做"可解释的评分卡 + 规则化建议"，原因很实际：

1. 派单规则一旦上线，调度员和站点负责人必须能理解、能申诉、能被审计；
2. 冷启动阶段没有足够的"正确派单"标签，监督学习无从谈起；
3. 规则化方案可以立即上线，同时**把每次派单结果沉淀成训练数据**，
   等积累够了再演进到模型排序（这条路在项目 README 的 Roadmap 里写明）。

输出分三块：站点派单评分卡、工程师产能与质量画像、可落地的派单规则建议。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from .config import Config
from .metrics import (
    METRICS,
    evaluate,
    format_metric,
    overall_metrics,
    scorecard as build_scorecard,
    slice_by,
)
from .models import Order, Site

#: 派单评分卡参与打分的指标（与 metrics.scorecard 的分项保持一致）
SCORECARD_KEYS: tuple[str, ...] = (
    "arrive_ontime_rate",
    "sla_achieve_rate",
    "dispatch_ontime_rate",
    "first_fix_rate",
    "rework_rate",
    "complaint_rate",
    "bad_review_rate",
    "good_review_rate",
    "gross_margin",
)

#: 工程师画像最少样本量（样本太少不参与推荐，避免"运气好"被当成能力）
MIN_ENGINEER_ORDERS = 30


def _advice_for(quality_score: Optional[float], orders: int, capacity_limit: float) -> str:
    if quality_score is None:
        return "样本不足，暂不参与推荐"
    if quality_score >= 88:
        return "优先派单（质量稳定，可承接高复杂度品类）"
    if quality_score < 78:
        return "质量帮扶（限制高复杂度品类，安排带教与抽检）"
    if orders > capacity_limit:
        return "负载偏高（建议分流，避免超时风险）"
    return "常规派单"


def site_dispatch_scorecard(
    orders: Sequence[Order],
    cfg: Config,
    sites: Sequence[Site],
    days: Optional[int] = None,
) -> list[dict[str, Any]]:
    """站点派单评分卡：把"派给谁"从经验判断变成可比较的分数。"""
    if not orders:
        return []
    days = days or max(1, (max(o.order_time for o in orders) - min(o.order_time for o in orders)).days)
    site_index = {site.site_id: site for site in sites}
    rows = slice_by(orders, "站点", cfg, keys=SCORECARD_KEYS)
    result: list[dict[str, Any]] = []
    for row in rows:
        site = site_index.get(row["value"])
        engineers = site.engineers if site else 0
        metrics = {key: row.get(key) for key in SCORECARD_KEYS}
        card = build_scorecard(metrics, cfg)
        score = card.get("total")
        orders_per_engineer_day = round(row["orders"] / engineers / max(days, 1), 2) if engineers else None
        if score is None:
            recommendation = "样本不足，暂不倾斜派单"
        elif score >= 88:
            recommendation = "优先承接（综合得分高）"
        elif score < 78:
            recommendation = "需改善后再倾斜派单"
        else:
            recommendation = "常规承接"
        result.append(
            {
                "site_id": row["value"],
                "site_name": row["label"],
                "site_type": site.site_type if site else "未知",
                "city": site.city if site else "",
                "engineers": engineers,
                "orders": row["orders"],
                "orders_per_engineer_day": orders_per_engineer_day,
                "score": score,
                "grade": card.get("grade"),
                "dispatch_p90": row.get("dispatch_ontime_rate"),
                "arrive_ontime_rate": row.get("arrive_ontime_rate"),
                "first_fix_rate": row.get("first_fix_rate"),
                "complaint_rate": row.get("complaint_rate"),
                "gross_margin": row.get("gross_margin"),
                "recommendation": recommendation,
            }
        )
    result.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    return result


def engineer_profile(orders: Sequence[Order], cfg: Config, min_orders: int = MIN_ENGINEER_ORDERS) -> list[dict[str, Any]]:
    """工程师产能与质量画像：谁值得多派单、谁需要帮扶。"""
    groups: dict[str, list[Order]] = {}
    for order in orders:
        groups.setdefault(order.engineer_id, []).append(order)

    rows: list[dict[str, Any]] = []
    for engineer_id, bucket in groups.items():
        if len(bucket) < min_orders:
            continue
        metrics = overall_metrics(bucket, cfg, keys=SCORECARD_KEYS)
        card = build_scorecard(metrics, cfg)
        rows.append(
            {
                "engineer_id": engineer_id,
                "site_id": bucket[0].site_id,
                "orders": len(bucket),
                "dispatch_p90": metrics.get("dispatch_ontime_rate"),
                "arrive_ontime_rate": metrics.get("arrive_ontime_rate"),
                "first_fix_rate": metrics.get("first_fix_rate"),
                "rework_rate": metrics.get("rework_rate"),
                "complaint_rate": metrics.get("complaint_rate"),
                "quality_score": card.get("total"),
                "advice": _advice_for(card.get("total"), len(bucket), capacity_limit=min_orders * 4),
            }
        )
    rows.sort(key=lambda r: (r["quality_score"] is None, -(r["quality_score"] or 0)))
    return rows


def dispatch_rules(
    site_rows: Sequence[dict[str, Any]],
    engineer_rows: Sequence[dict[str, Any]],
    orders: Sequence[Order],
    cfg: Config,
) -> list[dict[str, Any]]:
    """按数据现状生成派单规则建议（规则 + 触发条件 + 依据）。"""
    category_quality = slice_by(orders, "品类", cfg, keys=("first_fix_rate", "rework_rate", "complaint_rate"))
    worst_category = min(
        (row for row in category_quality if row.get("first_fix_rate") is not None),
        key=lambda row: row["first_fix_rate"],
        default=None,
    )
    slow_sites = [row for row in site_rows if (row.get("score") or 100) < 80]
    slow_site_names = "、".join(row["site_name"] for row in slow_sites[:3]) or "暂无"
    weak_engineers = [row for row in engineer_rows if (row.get("quality_score") or 100) < 78]
    top_engineers = [row for row in engineer_rows if (row.get("quality_score") or 0) >= 88]

    rules = [
        {
            "priority": 1,
            "name": "技能资质硬校验",
            "trigger": "工单品类与工程师技能标签不匹配时，禁止直接派单",
            "action": "进入人工调度池，并提示补录技能标签或改派有资质工程师",
            "evidence": (
                f"{worst_category['value']}一次完工率仅 {format_metric('first_fix_rate', worst_category['first_fix_rate'])}，"
                f"低于其他品类，返工率 {format_metric('rework_rate', worst_category['rework_rate'])}"
                if worst_category
                else "按品类质量差异设置校验"
            ),
        },
        {
            "priority": 2,
            "name": "站点负载均衡",
            "trigger": "站点「待派工单 / 在线工程师」超过站点基线的 1.3 倍",
            "action": "开放同城邻近站点支援派单，跨城需值班长审批",
            "evidence": f"综合得分偏低的站点：{slow_site_names}；异常预警中站点维度命中派单时长与客诉指标",
        },
        {
            "priority": 3,
            "name": "质量优先派单",
            "trigger": "工单为高复杂度/高价值品类（如充电桩、搬家）时",
            "action": "优先派给质量得分 ≥88 的工程师，并保留其当日产能余量",
            "evidence": (
                f"当前有 {len(top_engineers)} 名工程师质量得分 ≥88，可作为优先池；"
                f"{len(weak_engineers)} 名工程师质量得分 <78，建议限制高复杂度品类"
            ),
        },
        {
            "priority": 4,
            "name": "时效红线兜底",
            "trigger": "派单后超过品类承诺派单时长（如家电 4 小时）仍未接单",
            "action": "自动升级到值班长并强制改派，同时记录未接单原因",
            "evidence": "规则扫描中「派单超时」是命中量最高的异常类型，说明接单响应是当前主要瓶颈",
        },
        {
            "priority": 5,
            "name": "改约与准时联动",
            "trigger": "工程师当日已有改约记录，或历史改约率高于站点均值",
            "action": "降低其当日新增派单优先级，并对已有工单提前触达用户",
            "evidence": "改约与上门迟到高度相关（迟到工单的客诉率显著更高），需在派单阶段前置控制",
        },
    ]
    return rules


def build_dispatch_view(
    orders: Sequence[Order],
    cfg: Config,
    sites: Sequence[Site],
    engineer_limit: int = 15,
) -> dict[str, Any]:
    """汇总派单视图，作为证据包的一部分供报告与提示词使用。"""
    site_rows = site_dispatch_scorecard(orders, cfg, sites)
    engineer_rows = engineer_profile(orders, cfg)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sites": site_rows,
        "engineers": engineer_rows[:engineer_limit],
        "engineer_count": len(engineer_rows),
        "rules": dispatch_rules(site_rows, engineer_rows, orders, cfg),
    }
