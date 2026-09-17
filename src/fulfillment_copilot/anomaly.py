"""异常检测与归因下钻。

两条检测链路（对应 JD「异常管理与指标建设」）
--------------------------------------------
1. **规则引擎（工单级）**：按运营规则给工单打异常标签——派单超时、预约超时、上门迟到、
   完工超时、多次改约、返工、低分差评、客诉、成本偏离。产出可直接派工的「异常工单清单」。
2. **统计预警（维度级）**：对「站点 / 品类 / 城市」近 14 天的指标，与历史基线比较
   （相对偏离 + 稳健 z-score），识别"某个维度最近突然变差"，并给出严重级别与建议动作。

归因下钻
--------
对任一异常做"由粗到细"的维度拆解，输出贡献度排序，回答"到底是谁把指标拖下去的"，
并生成一句可以直接写进日报的结论——把「发现异常」推进到「定位原因」。
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from .config import Config
from .metrics import (
    DIMENSION_KEYS,
    METRICS,
    MetricSpec,
    appoint_breach,
    arrive_ontime,
    dispatch_breach,
    finish_breach,
    format_metric,
    metric_value,
    total_breach,
)
from .models import Order, Site

# ---------------------------------------------------------------- 规则引擎

#: 参与统计预警的指标（有明确好坏方向、且能被运营动作改善）
STAT_METRICS: tuple[str, ...] = (
    "dispatch_p90",
    "arrive_ontime_rate",
    "finish_p90",
    "total_p90",
    "sla_achieve_rate",
    "first_fix_rate",
    "rework_rate",
    "complaint_rate",
    "bad_review_rate",
    "avg_cost",
    "gross_margin",
)

#: 统计预警的默认维度（工程师维度样本分散，默认不参与，需要时可显式传入）
STAT_DIMENSIONS: tuple[str, ...] = ("站点", "品类", "城市")

#: 指标 → 建议动作（异常预警要能落到"谁在什么时候做什么"）
_SEVERITY_RANK = {"高": 0, "中": 1, "低": 2}

SUGGESTIONS: dict[str, str] = {
    "dispatch_p90": "核查派单规则与产能：技能标签匹配度、接单池大小、排班缺口；必要时临时开放跨站派单。",
    "arrive_ontime_rate": "复核预约时段容量与派单时效，收窄时段承诺并增加提前触达（上门前 2 小时提醒）。",
    "finish_p90": "推进现场作业标准化，复盘长尾工单的作业工序与备件准备。",
    "total_p90": "按环节定位瓶颈（派单/上门/作业），对超长链路工单做逐单复盘。",
    "sla_achieve_rate": "梳理超时高发品类与站点，修订时效承诺与升级规则。",
    "first_fix_rate": "组织技能培训与备件前置，推动一次上门解决；返工工单纳入工程师质量档案。",
    "rework_rate": "分析返工原因分布（技能/备件/方案），纳入质检抽检与培训闭环。",
    "complaint_rate": "按客诉主题拆解到责任岗位，明确首响时效与升级路径，做案例复盘。",
    "bad_review_rate": "回访差评工单，定位体验断点（时效/费用/态度），纳入站点服务质量考核。",
    "avg_cost": "复核结算单价与运力采购成本，排查费用规则漏洞、异常补贴与重复赔付。",
    "gross_margin": "结合收入与成本双向核查：是否低价促销过量、成本项是否异常。",
}


@dataclass
class OrderAnomaly:
    """工单级异常（可直接派工处理）。"""

    order_id: str
    anomaly_type: str
    severity: str
    category: str
    site_id: str
    site_name: str
    city: str
    engineer_id: str
    order_time: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _severity_by_ratio(actual: float, limit: float, high: float = 3.0, medium: float = 1.5) -> str:
    """按超时倍数定级：3 倍以上为高（需立即响应），1.5 倍以上为中（需跟进）。"""
    if limit <= 0:
        return "中"
    ratio = actual / limit
    if ratio >= high:
        return "高"
    if ratio >= medium:
        return "中"
    return "低"


def rule_scan(orders: Sequence[Order], cfg: Config, top: int = 60) -> dict[str, Any]:
    """工单级规则扫描，返回异常清单与分类统计。"""
    flagged: list[OrderAnomaly] = []
    per_type: dict[str, int] = {}
    per_site: dict[str, int] = {}
    per_category: dict[str, int] = {}
    flagged_orders: set[str] = set()
    order_severity: dict[str, str] = {}  # 工单级最严重程度（一单命中多条规则时取最高）

    # 成本偏离需要先算出品类均值
    category_cost: dict[str, float] = {}
    for category in {o.category for o in orders}:
        values = [o.cost for o in orders if o.category == category]
        if values:
            category_cost[category] = sum(values) / len(values)
    cost_threshold = cfg.threshold("cost_deviation_threshold", 0.25)

    def add(order: Order, anomaly_type: str, severity: str, detail: str) -> None:
        flagged.append(
            OrderAnomaly(
                order_id=order.order_id,
                anomaly_type=anomaly_type,
                severity=severity,
                category=order.category,
                site_id=order.site_id,
                site_name=order.site_name,
                city=order.city,
                engineer_id=order.engineer_id,
                order_time=order.order_time.isoformat(timespec="minutes"),
                detail=detail,
            )
        )
        per_type[anomaly_type] = per_type.get(anomaly_type, 0) + 1
        per_site[order.site_id] = per_site.get(order.site_id, 0) + 1
        per_category[order.category] = per_category.get(order.category, 0) + 1
        flagged_orders.add(order.order_id)
        worst = order_severity.get(order.order_id)
        if worst is None or _SEVERITY_RANK[severity] < _SEVERITY_RANK[worst]:
            order_severity[order.order_id] = severity

    reschedule_limit = cfg.threshold("reschedule_warn_count", 2)
    bad_review_threshold = cfg.threshold("bad_review_threshold", 2)

    for order in orders:
        sla = cfg.sla_for(order.category)

        if order.is_cancelled:
            add(order, "订单取消", "中", "用户在履约过程中取消，需回访确认原因")
            continue

        if dispatch_breach(order, cfg) is False and order.dispatch_hours is not None:
            add(
                order,
                "派单超时",
                _severity_by_ratio(order.dispatch_hours, sla["dispatch_hours"]),
                f"派单耗时 {order.dispatch_hours:.1f}h，超出 {order.category} 承诺 {sla['dispatch_hours']:.1f}h",
            )
        if appoint_breach(order, cfg) is False and order.appoint_hours is not None:
            add(
                order,
                "预约超时",
                _severity_by_ratio(order.appoint_hours, sla["appoint_hours"]),
                f"预约耗时 {order.appoint_hours:.1f}h，超出承诺 {sla['appoint_hours']:.1f}h",
            )
        if arrive_ontime(order, cfg) is False and order.arrive_delay_hours is not None:
            # 口径必须与 metrics.arrive_ontime 一致：以「约定上门时段」为基准，而不是预约受理时间
            delay = order.arrive_delay_hours
            add(
                order,
                "上门迟到",
                _severity_by_ratio(delay, max(sla["arrive_window_hours"], 0.5)),
                f"晚于约定上门时段 {delay:.1f}h（允许窗口 {sla['arrive_window_hours']:.0f}h）",
            )
        if finish_breach(order, cfg) is False and order.finish_hours is not None:
            add(
                order,
                "完工超时",
                _severity_by_ratio(order.finish_hours, sla["finish_hours"]),
                f"现场作业 {order.finish_hours:.1f}h，超出承诺 {sla['finish_hours']:.1f}h",
            )
        if total_breach(order, cfg) is False and order.total_hours is not None:
            add(
                order,
                "全链路超时",
                _severity_by_ratio(order.total_hours, sla["total_hours"]),
                f"下单到完工 {order.total_hours:.1f}h，超出承诺 {sla['total_hours']:.0f}h",
            )
        if order.reschedule_count >= reschedule_limit:
            add(order, "多次改约", "中", f"累计改约 {order.reschedule_count} 次，体验受损")
        if order.is_rework:
            add(order, "返工", "中", "首次上门未解决，产生二次作业")
        if order.review_score is not None and order.review_score <= bad_review_threshold:
            add(order, "低分差评", "中", f"用户评分 {order.review_score} 分，需回访定位体验断点")
        if order.is_complaint:
            add(order, "客诉", "高", f"客诉主题：{order.complaint_theme or '未分类'}")
        avg_cost = category_cost.get(order.category)
        if avg_cost and order.cost > avg_cost * (1 + cost_threshold):
            add(
                order,
                "成本异常",
                _severity_by_ratio(order.cost, avg_cost * (1 + cost_threshold)),
                f"单均成本 {order.cost:.0f} 元，高于品类均值 {avg_cost:.0f} 元 {(order.cost / avg_cost - 1) * 100:.0f}%",
            )

    severity_rank = _SEVERITY_RANK
    flagged.sort(key=lambda a: (severity_rank.get(a.severity, 3), a.order_time))
    total = len(orders)
    per_severity = {level: sum(1 for s in order_severity.values() if s == level) for level in ("高", "中", "低")}
    follow_up = per_severity["高"] + per_severity["中"]
    return {
        "total_orders": total,
        "flagged_orders": len(flagged_orders),
        "flagged_rate": round(len(flagged_orders) / total, 4) if total else None,
        "follow_up_orders": follow_up,
        "follow_up_rate": round(follow_up / total, 4) if total else None,
        "by_type": {
            name: {"count": count, "rate": round(count / total, 4) if total else None}
            for name, count in sorted(per_type.items(), key=lambda kv: kv[1], reverse=True)
        },
        "by_site": dict(sorted(per_site.items(), key=lambda kv: kv[1], reverse=True)),
        "by_category": dict(sorted(per_category.items(), key=lambda kv: kv[1], reverse=True)),
        "by_severity": {level: per_severity.get(level, 0) for level in ("高", "中", "低")},
        "detail": [a.to_dict() for a in flagged[:top]],
    }


# ---------------------------------------------------------------- 统计预警


@dataclass
class DimensionAnomaly:
    """维度级异常（站点/品类/城市 + 指标）。"""

    dimension: str
    value: str
    label: str
    metric: str
    metric_name: str
    baseline_value: float
    current_value: float
    deviation: float
    robust_z: float
    severity: str
    recent_orders: int
    baseline_orders: int
    message: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _robust_z(series: Sequence[Optional[float]], current: Optional[float]) -> float:
    """稳健 z-score：以中位数为中心、MAD 为尺度，抗离群值。"""
    values = [v for v in series if v is not None]
    if current is None or len(values) < 3:
        return 0.0
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations)
    if mad == 0:
        return 0.0
    return round(0.6745 * (current - median) / mad, 3)


def _weekly_series(orders: Sequence[Order], metric: str, cfg: Config, min_orders: int = 20) -> list[Optional[float]]:
    """取该分组按周的指标序列，剔除样本量过小的周，避免小样本把基线尺度压低。"""
    from .metrics import weekly_trend  # 局部导入避免循环依赖

    rows = weekly_trend(orders, cfg, keys=[metric])
    return [row.get(metric) for row in rows if row.get("orders", 0) >= min_orders]


def detect_dimension_anomalies(
    orders: Sequence[Order],
    cfg: Config,
    recent_days: int = 14,
    baseline_days: int = 56,
    dimensions: Optional[Iterable[str]] = None,
    metrics: Optional[Iterable[str]] = None,
    anchor: Optional[datetime] = None,
    top: int = 20,
) -> list[DimensionAnomaly]:
    """近 14 天 vs 历史基线的维度级异常检测。"""
    if not orders:
        return []
    anchor = anchor or max(o.order_time for o in orders)
    recent_start = anchor - timedelta(days=recent_days)
    baseline_start = recent_start - timedelta(days=baseline_days)

    recent = [o for o in orders if o.order_time > recent_start]
    baseline = [o for o in orders if baseline_start <= o.order_time <= recent_start]
    if not recent or not baseline:
        return []

    z_threshold = cfg.threshold("robust_zscore_threshold", 2.5)
    deviation_threshold = cfg.threshold("mom_change_threshold", 0.30)
    min_sample = int(cfg.threshold("min_sample_size", 20))

    results: list[DimensionAnomaly] = []
    for dimension in dimensions or STAT_DIMENSIONS:
        key_fn = DIMENSION_KEYS[dimension]
        recent_groups: dict[str, list[Order]] = {}
        baseline_groups: dict[str, list[Order]] = {}
        for order in recent:
            recent_groups.setdefault(key_fn(order), []).append(order)
        for order in baseline:
            baseline_groups.setdefault(key_fn(order), []).append(order)

        for value, current_bucket in recent_groups.items():
            if len(current_bucket) < min_sample:
                continue
            previous_bucket = baseline_groups.get(value, [])
            if len(previous_bucket) < min_sample:
                continue
            for metric in metrics or STAT_METRICS:
                spec: Optional[MetricSpec] = METRICS.get(metric)
                if spec is None:
                    continue
                current = metric_value(current_bucket, metric, cfg)
                base = metric_value(previous_bucket, metric, cfg)
                if current is None or base in (None, 0):
                    continue
                deviation = (current - base) / abs(base)
                worsened = deviation < 0 if spec.higher_is_better else deviation > 0
                if not worsened:
                    continue
                z = _robust_z(_weekly_series(previous_bucket, metric, cfg), current)
                # 显著性门槛：相对偏离必须达标，且要么 z 显著、要么偏离足够大（>2 倍阈值），
                # 避免把"样本小导致的正常波动"报成异常。
                if abs(deviation) < deviation_threshold:
                    continue
                if abs(z) < z_threshold and abs(deviation) < 2 * deviation_threshold:
                    continue
                # 严重度分级：让"高"保持稀缺、可行动，避免所有异常都被标成最高级
                # 高 = 偏离 ≥100% 或稳健 z ≥6；中 = 偏离 ≥50% 或 z ≥3.5；其余为低
                if abs(z) >= 6 or abs(deviation) >= 1.0:
                    severity = "高"
                elif abs(z) >= 3.5 or abs(deviation) >= 0.5:
                    severity = "中"
                else:
                    severity = "低"
                label = current_bucket[0].site_name if dimension == "站点" else value
                message = (
                    f"{dimension}「{label}」{spec.name} 由基线 {format_metric(metric, base)} "
                    f"变为 {format_metric(metric, current)}（{deviation:+.1%}，稳健 z={z:.2f}）"
                )
                results.append(
                    DimensionAnomaly(
                        dimension=dimension,
                        value=value,
                        label=label,
                        metric=metric,
                        metric_name=spec.name,
                        baseline_value=round(base, 4),
                        current_value=round(current, 4),
                        deviation=round(deviation, 4),
                        robust_z=z,
                        severity=severity,
                        recent_orders=len(current_bucket),
                        baseline_orders=len(previous_bucket),
                        message=message,
                        suggestion=SUGGESTIONS.get(metric, "定位责任方并制定改善动作。"),
                    )
                )

    severity_rank = {"高": 0, "中": 1, "低": 2}
    results.sort(key=lambda a: (severity_rank.get(a.severity, 3), -abs(a.deviation)))
    return results[:top]


# ---------------------------------------------------------------- 归因下钻


def attribute(
    orders: Sequence[Order],
    cfg: Config,
    dimension: str,
    value: str,
    metric: str,
    drill_dimensions: Sequence[str] = ("品类", "工程师", "城市", "站点"),
    recent_days: int = 14,
    anchor: Optional[datetime] = None,
    top: int = 5,
) -> dict[str, Any]:
    """对某个维度异常做下钻归因，返回贡献度排序与一句结论。"""
    spec = METRICS.get(metric)
    if spec is None or not orders:
        return {}
    anchor = anchor or max(o.order_time for o in orders)
    recent_start = anchor - timedelta(days=recent_days)

    key_fn = DIMENSION_KEYS[dimension]
    recent = [o for o in orders if o.order_time > recent_start]
    parent = [o for o in recent if key_fn(o) == value]
    peers = [o for o in recent if key_fn(o) != value]
    if not parent or not peers:
        return {}

    parent_metric = metric_value(parent, metric, cfg)
    peer_metric = metric_value(peers, metric, cfg)
    if parent_metric is None or peer_metric in (None, 0):
        return {}

    rows: list[dict[str, Any]] = []
    for drill in drill_dimensions:
        if drill == dimension:
            continue
        drill_key = DIMENSION_KEYS[drill]
        groups: dict[str, list[Order]] = {}
        for order in parent:
            groups.setdefault(drill_key(order), []).append(order)
        # 与异常维度 1:1 的维度没有拆解价值（例如站点异常再按城市拆 → 只有一个分组），跳过
        if len(groups) <= 1:
            continue
        for group_value, bucket in groups.items():
            if not bucket:
                continue
            group_metric = metric_value(bucket, metric, cfg)
            if group_metric is None:
                continue
            gap = (group_metric - peer_metric) if not spec.higher_is_better else (peer_metric - group_metric)
            share = len(bucket) / len(parent)
            contribution = max(0.0, gap) * share / (abs(peer_metric) or 1.0)
            rows.append(
                {
                    "drill": drill,
                    "value": group_value,
                    "label": bucket[0].site_name if drill == "站点" else group_value,
                    "orders": len(bucket),
                    "share": round(share, 4),
                    "metric_value": round(group_metric, 4),
                    "metric_display": format_metric(metric, group_metric),
                    "peer_display": format_metric(metric, peer_metric),
                    "gap": round(gap, 4),
                    "contribution": round(contribution, 4),
                    "involved": bool(gap > 0),
                }
            )

    rows.sort(key=lambda r: r["contribution"], reverse=True)
    top_rows = rows[:top]
    conclusion = ""
    if top_rows:
        head = top_rows[0]
        conclusion = (
            f"{dimension}「{parent[0].site_name if dimension == '站点' else value}」近 {recent_days} 天 "
            f"{spec.name} 为 {format_metric(metric, parent_metric)}（同期其他{dimension} {format_metric(metric, peer_metric)}）。"
            f"按{head['drill']}下钻，{head['label']} 的恶化贡献最大："
            f"承接 {head['orders']} 单（占 {head['share']:.0%}），{spec.name} {head['metric_display']}。"
        )
    return {
        "dimension": dimension,
        "value": value,
        "metric": metric,
        "metric_name": spec.name,
        "parent_value": parent_metric,
        "peer_value": peer_metric,
        "parent_display": format_metric(metric, parent_metric),
        "peer_display": format_metric(metric, peer_metric),
        "recent_days": recent_days,
        "rows": top_rows,
        "all_rows": rows,
        "conclusion": conclusion,
    }


def detect_all(orders: Sequence[Order], cfg: Config, anchor: Optional[datetime] = None, top: int = 8) -> dict[str, Any]:
    """一次性跑完规则扫描 + 统计预警 + Top 异常归因，供报告与 AI 使用。"""
    anchor = anchor or (max(o.order_time for o in orders) if orders else datetime.now())
    dimension_anomalies = detect_dimension_anomalies(orders, cfg, anchor=anchor)
    attributions: list[dict[str, Any]] = []
    for anomaly in dimension_anomalies[:top]:
        detail = attribute(orders, cfg, anomaly.dimension, anomaly.value, anomaly.metric, anchor=anchor)
        if detail:
            detail["severity"] = anomaly.severity
            detail["deviation"] = anomaly.deviation
            attributions.append(detail)
    return {
        "anchor": anchor.isoformat(timespec="minutes"),
        "rule": rule_scan(orders, cfg),
        "dimension": [a.to_dict() for a in dimension_anomalies],
        "attributions": attributions,
    }
