"""履约质量指标体系：口径集中定义 + 计算 + 分层汇总 + 综合得分。

设计要点
--------
1. **口径单一来源**：所有指标的名称、单位、方向（越大越好/越小越好）、目标值都在
   ``METRICS`` 注册表里定义，报告、异常检测、归因、Excel 导出复用同一套实现，
   避免"同一个指标在不同报表里算法不一致"这个运营分析最常见的坑。
2. **缺失值安全**：分母为 0 时返回 ``None`` 而不是 0，避免把"没有样本"误判成"表现差"。
3. **可解释**：每个指标都能回答"它衡量哪个环节、偏离多少算异常"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Optional, Sequence

from .config import Config
from .models import Order

# ============================================================ 基础统计工具


def percentile(values: Iterable[Optional[float]], q: float) -> Optional[float]:
    """线性插值分位数（P50/P90 等），自动忽略空值。"""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return round(data[0], 3)
    position = (len(data) - 1) * q
    low = int(position)
    high = min(low + 1, len(data) - 1)
    weight = position - low
    return round(data[low] + (data[high] - data[low]) * weight, 3)


def safe_rate(numerator: int, denominator: int) -> Optional[float]:
    """比率计算；分母为 0 返回 None（样本不足，不参与考核）。"""
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def safe_mean(values: Iterable[Optional[float]]) -> Optional[float]:
    data = [v for v in values if v is not None]
    if not data:
        return None
    return round(sum(data) / len(data), 3)


# ============================================================ 子集与判定


def finished_orders(orders: Sequence[Order]) -> list[Order]:
    return [o for o in orders if o.is_finished]


def reviewed_orders(orders: Sequence[Order]) -> list[Order]:
    return [o for o in orders if o.review_score is not None]


def _within(value: Optional[float], limit: Optional[float]) -> Optional[bool]:
    if value is None or limit is None:
        return None
    return value <= limit


def dispatch_breach(order: Order, cfg: Config) -> Optional[bool]:
    """派单是否超时（下单 → 派单超出该品类承诺）。"""
    return _within(order.dispatch_hours, cfg.sla_for(order.category)["dispatch_hours"])


def appoint_breach(order: Order, cfg: Config) -> Optional[bool]:
    return _within(order.appoint_hours, cfg.sla_for(order.category)["appoint_hours"])


def arrive_ontime(order: Order, cfg: Config) -> Optional[bool]:
    """上门是否准时：实际上门时间不晚于「约定上门时段 + 品类允许的到达窗口」。"""
    if order.arrive_time is None or order.promised_time is None:
        return None
    window = cfg.sla_for(order.category)["arrive_window_hours"]
    return order.arrive_time <= order.promised_time + timedelta(hours=window)


def finish_breach(order: Order, cfg: Config) -> Optional[bool]:
    """现场作业是否超时（上门 → 完工）。"""
    return _within(order.finish_hours, cfg.sla_for(order.category)["finish_hours"])


def total_breach(order: Order, cfg: Config) -> Optional[bool]:
    """全链路是否超时（下单 → 完工）。"""
    return _within(order.total_hours, cfg.sla_for(order.category)["total_hours"])


# ============================================================ 指标注册表


@dataclass(frozen=True)
class MetricSpec:
    """一个指标的口径定义。"""

    key: str
    name: str
    unit: str  # 小时 / % / 单 / 元
    direction: str  # down_better / up_better
    category: str  # timeliness / quality / experience / efficiency

    @property
    def higher_is_better(self) -> bool:
        return self.direction == "up_better"


def _is_breach(predicate: Callable[[Order, Config], Optional[bool]], orders: Sequence[Order], cfg: Config) -> int:
    return sum(1 for o in orders if predicate(o, cfg) is False)


def _breach_denominator(predicate: Callable[[Order, Config], Optional[bool]], orders: Sequence[Order], cfg: Config) -> int:
    return sum(1 for o in orders if predicate(o, cfg) is not None)


_EXTRACTORS: dict[str, Callable[[Sequence[Order], Config], Optional[float]]] = {
    # ---------------- 时效
    "order_count": lambda orders, cfg: len(orders),
    "dispatch_p50": lambda orders, cfg: percentile((o.dispatch_hours for o in orders), 0.5),
    "dispatch_p90": lambda orders, cfg: percentile((o.dispatch_hours for o in orders), 0.9),
    "dispatch_ontime_rate": lambda orders, cfg: safe_rate(
        _breach_denominator(dispatch_breach, orders, cfg) - _is_breach(dispatch_breach, orders, cfg),
        _breach_denominator(dispatch_breach, orders, cfg),
    ),
    "appoint_p90": lambda orders, cfg: percentile((o.appoint_hours for o in orders), 0.9),
    "arrive_ontime_rate": lambda orders, cfg: safe_rate(
        sum(1 for o in orders if arrive_ontime(o, cfg) is True),
        sum(1 for o in orders if arrive_ontime(o, cfg) is not None),
    ),
    "finish_p90": lambda orders, cfg: percentile((o.finish_hours for o in orders), 0.9),
    "finish_ontime_rate": lambda orders, cfg: safe_rate(
        _breach_denominator(finish_breach, orders, cfg) - _is_breach(finish_breach, orders, cfg),
        _breach_denominator(finish_breach, orders, cfg),
    ),
    "total_p50": lambda orders, cfg: percentile((o.total_hours for o in orders), 0.5),
    "total_p90": lambda orders, cfg: percentile((o.total_hours for o in orders), 0.9),
    "sla_achieve_rate": lambda orders, cfg: safe_rate(
        _breach_denominator(total_breach, orders, cfg) - _is_breach(total_breach, orders, cfg),
        _breach_denominator(total_breach, orders, cfg),
    ),
    # ---------------- 质量
    "first_fix_rate": lambda orders, cfg: safe_rate(
        sum(1 for o in orders if o.is_first_fix is True),
        sum(1 for o in orders if o.is_first_fix is not None),
    ),
    "rework_rate": lambda orders, cfg: safe_rate(sum(1 for o in finished_orders(orders) if o.is_rework), len(finished_orders(orders))),
    "reschedule_rate": lambda orders, cfg: safe_rate(sum(1 for o in orders if o.reschedule_count > 0), len(orders)),
    "cancel_rate": lambda orders, cfg: safe_rate(sum(1 for o in orders if o.is_cancelled), len(orders)),
    "complaint_rate": lambda orders, cfg: safe_rate(sum(1 for o in orders if o.is_complaint), len(orders)),
    # ---------------- 体验
    "avg_review_score": lambda orders, cfg: safe_mean((float(o.review_score) for o in reviewed_orders(orders))),
    "good_review_rate": lambda orders, cfg: safe_rate(sum(1 for o in reviewed_orders(orders) if (o.review_score or 0) >= 4), len(reviewed_orders(orders))),
    "bad_review_rate": lambda orders, cfg: safe_rate(
        sum(1 for o in reviewed_orders(orders) if (o.review_score or 5) <= cfg.threshold("bad_review_threshold", 2)),
        len(reviewed_orders(orders)),
    ),
    "nps": lambda orders, cfg: (
        None
        if not reviewed_orders(orders)
        else round(
            (
                sum(1 for o in reviewed_orders(orders) if (o.review_score or 0) >= 4)
                - sum(1 for o in reviewed_orders(orders) if (o.review_score or 5) <= 2)
            )
            / len(reviewed_orders(orders))
            * 100,
            2,
        )
    ),
    # ---------------- 效率与成本
    "avg_fee": lambda orders, cfg: safe_mean((o.fee for o in orders)),
    "avg_cost": lambda orders, cfg: safe_mean((o.cost for o in orders)),
    "gross_margin": lambda orders, cfg: (
        None
        if not orders or sum(o.fee for o in orders) == 0
        else round((sum(o.fee for o in orders) - sum(o.cost for o in orders)) / sum(o.fee for o in orders), 4)
    ),
    "avg_reschedule_count": lambda orders, cfg: safe_mean((float(o.reschedule_count) for o in orders)),
}

METRICS: dict[str, MetricSpec] = {
    "order_count": MetricSpec("order_count", "工单量", "单", "up_better", "efficiency"),
    "dispatch_p50": MetricSpec("dispatch_p50", "派单时长P50", "小时", "down_better", "timeliness"),
    "dispatch_p90": MetricSpec("dispatch_p90", "派单时长P90", "小时", "down_better", "timeliness"),
    "dispatch_ontime_rate": MetricSpec("dispatch_ontime_rate", "派单及时率", "%", "up_better", "timeliness"),
    "appoint_p90": MetricSpec("appoint_p90", "预约时长P90", "小时", "down_better", "timeliness"),
    "arrive_ontime_rate": MetricSpec("arrive_ontime_rate", "上门准时率", "%", "up_better", "timeliness"),
    "finish_p90": MetricSpec("finish_p90", "完工时长P90", "小时", "down_better", "timeliness"),
    "finish_ontime_rate": MetricSpec("finish_ontime_rate", "完工及时率", "%", "up_better", "timeliness"),
    "total_p50": MetricSpec("total_p50", "全链路时长P50", "小时", "down_better", "timeliness"),
    "total_p90": MetricSpec("total_p90", "全链路时长P90", "小时", "down_better", "timeliness"),
    "sla_achieve_rate": MetricSpec("sla_achieve_rate", "全链路SLA达成率", "%", "up_better", "timeliness"),
    "first_fix_rate": MetricSpec("first_fix_rate", "一次完工率", "%", "up_better", "quality"),
    "rework_rate": MetricSpec("rework_rate", "返工率", "%", "down_better", "quality"),
    "reschedule_rate": MetricSpec("reschedule_rate", "改约率", "%", "down_better", "quality"),
    "cancel_rate": MetricSpec("cancel_rate", "取消率", "%", "down_better", "quality"),
    "complaint_rate": MetricSpec("complaint_rate", "客诉率", "%", "down_better", "experience"),
    "avg_review_score": MetricSpec("avg_review_score", "平均评分", "分", "up_better", "experience"),
    "good_review_rate": MetricSpec("good_review_rate", "好评率", "%", "up_better", "experience"),
    "bad_review_rate": MetricSpec("bad_review_rate", "差评率", "%", "down_better", "experience"),
    "nps": MetricSpec("nps", "服务NPS", "分", "up_better", "experience"),
    "avg_fee": MetricSpec("avg_fee", "单均收入", "元", "up_better", "efficiency"),
    "avg_cost": MetricSpec("avg_cost", "单均成本", "元", "down_better", "efficiency"),
    "gross_margin": MetricSpec("gross_margin", "毛利率", "%", "up_better", "efficiency"),
}

#: 指标大类的中文名（报告与 Excel 共用，避免同一分类出现两种叫法）
CATEGORY_LABELS: dict[str, str] = {
    "timeliness": "时效",
    "quality": "质量",
    "experience": "体验",
    "efficiency": "效率与成本",
}

#: 综合得分的分项构成（键为分项名，值为参与打分的指标）
SCORECARD_COMPONENTS: dict[str, list[str]] = {
    "timeliness": ["arrive_ontime_rate", "sla_achieve_rate", "dispatch_ontime_rate"],
    "quality": ["first_fix_rate", "rework_rate"],
    "experience": ["complaint_rate", "bad_review_rate", "good_review_rate"],
}

#: 打标目标值（可在 config/thresholds.json 的 scorecard.targets 中覆盖）
DEFAULT_TARGETS: dict[str, float] = {
    "arrive_ontime_rate": 0.95,
    "sla_achieve_rate": 0.95,
    "dispatch_ontime_rate": 0.95,
    "first_fix_rate": 0.92,
    "rework_rate": 0.05,
    "complaint_rate": 0.02,
    "bad_review_rate": 0.02,
    "good_review_rate": 0.90,
}

#: 展示用小数列位
_PRECISION = {"%": 4, "小时": 2, "元": 2, "分": 2, "单": 0}


def metric_value(orders: Sequence[Order], key: str, cfg: Config) -> Optional[float]:
    """按指标 key 计算单值，未知 key 抛错（防止口径拼写错误静默通过）。"""
    if key not in _EXTRACTORS:
        raise KeyError(f"未定义的指标: {key}")
    return _EXTRACTORS[key](orders, cfg)


def evaluate(orders: Sequence[Order], keys: Iterable[str], cfg: Config) -> dict[str, Optional[float]]:
    return {key: metric_value(orders, key, cfg) for key in keys}


def format_metric(key: str, value: Optional[float], dash: str = "—") -> str:
    """把指标值渲染成报告里可直接展示的字符串。"""
    if value is None:
        return dash
    spec = METRICS.get(key)
    unit = spec.unit if spec else ""
    if unit == "%":
        return f"{value * 100:.1f}%"
    if unit == "单":
        return f"{int(value)}"
    if unit == "小时":
        return f"{value:.1f}h"
    if unit == "元":
        return f"{value:.1f}"
    return f"{value:.2f}"


# ============================================================ 分层汇总


def overall_metrics(orders: Sequence[Order], cfg: Config, keys: Optional[Iterable[str]] = None) -> dict[str, Optional[float]]:
    """全局总览指标。"""
    return evaluate(orders, keys or list(METRICS.keys()), cfg)


DIMENSION_KEYS: dict[str, Callable[[Order], str]] = {
    "品类": lambda o: o.category,
    "站点": lambda o: o.site_id,
    "城市": lambda o: o.city,
    "站点类型": lambda o: o.site_type,
    "工程师": lambda o: o.engineer_id,
}

_DIMENSION_LABEL: dict[str, Callable[[Order], str]] = {
    "站点": lambda o: o.site_name,
}


def slice_by(
    orders: Sequence[Order],
    dimension: str,
    cfg: Config,
    keys: Optional[Iterable[str]] = None,
    min_orders: int = 1,
) -> list[dict]:
    """按维度切分并计算指标，按工单量倒序返回。

    返回的每一行都带 ``value``（维度取值）与 ``label``（展示名），方便直接进报告和 Excel。
    """
    if dimension not in DIMENSION_KEYS:
        raise KeyError(f"不支持的维度: {dimension}")
    key_fn = DIMENSION_KEYS[dimension]
    label_fn = _DIMENSION_LABEL.get(dimension)
    groups: dict[str, list[Order]] = {}
    for order in orders:
        groups.setdefault(key_fn(order), []).append(order)

    rows: list[dict] = []
    for value, bucket in groups.items():
        if len(bucket) < min_orders:
            continue
        row: dict = {
            "dimension": dimension,
            "value": value,
            "label": label_fn(bucket[0]) if label_fn else value,
            "orders": len(bucket),
        }
        row.update(evaluate(bucket, keys or list(METRICS.keys()), cfg))
        rows.append(row)
    rows.sort(key=lambda r: r["orders"], reverse=True)
    return rows


def _week_start(moment: datetime) -> date:
    day = moment.date()
    return day - timedelta(days=day.weekday())


def weekly_trend(
    orders: Sequence[Order],
    cfg: Config,
    keys: Optional[Iterable[str]] = None,
    anchor: Optional[datetime] = None,
) -> list[dict]:
    """按自然周（周一起）汇总趋势，用于监控指标拐点。"""
    groups: dict[date, list[Order]] = {}
    for order in orders:
        groups.setdefault(_week_start(order.order_time), []).append(order)
    rows: list[dict] = []
    for start in sorted(groups):
        bucket = groups[start]
        row: dict = {
            "period": f"{start.isocalendar()[0]}-W{start.isocalendar()[1]:02d}",
            "start": start.isoformat(),
            "end": (start + timedelta(days=6)).isoformat(),
            "orders": len(bucket),
        }
        row.update(evaluate(bucket, keys or list(METRICS.keys()), cfg))
        rows.append(row)
    return rows


# ============================================================ 综合得分


def _score_component(key: str, value: Optional[float], cfg: Config) -> Optional[float]:
    """单项得分：达成率打分法，满分 100，超出目标最多按 100 计。"""
    if value is None:
        return None
    spec = METRICS.get(key)
    target = cfg.target(key, DEFAULT_TARGETS.get(key, 1.0))
    if target <= 0:
        return None
    if spec is not None and not spec.higher_is_better:
        score = 100.0 if value <= 0 else min(1.0, target / value) * 100
    else:
        score = min(1.0, value / target) * 100
    return round(score, 1)


def scorecard(metrics: dict[str, Optional[float]], cfg: Config) -> dict:
    """履约质量综合得分（分项 + 总分 + 等级），支撑站点/品类横向对标。"""
    result: dict = {"components": {}, "total": None, "grade": None}
    weighted_total = 0.0
    weight_used = 0.0
    for component, keys in SCORECARD_COMPONENTS.items():
        scores = {key: _score_component(key, metrics.get(key), cfg) for key in keys}
        valid = [v for v in scores.values() if v is not None]
        component_score = round(sum(valid) / len(valid), 1) if valid else None
        result["components"][component] = {"score": component_score, "detail": scores}
        weight = cfg.weight(component)
        if component_score is not None and weight > 0:
            weighted_total += component_score * weight
            weight_used += weight
    if weight_used > 0:
        total = round(weighted_total / weight_used, 1)
        result["total"] = total
        result["grade"] = "A" if total >= 90 else "B" if total >= 80 else "C" if total >= 70 else "D"
    return result
