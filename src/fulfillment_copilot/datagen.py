"""合成数据生成器：构造全链路履约明细，并**定向注入异常**。

为什么要用合成数据
------------------
真实履约明细涉及用户隐私（手机号、地址、评价文本）和商业机密，不可能放进公开仓库。
本项目改用「可复现的合成数据」演示完整方法链路，并主动注入若干**已知异常**，
让异常检测、归因分析、AI 报告的效果可以被自动化测试验证——
这比拿一份来路不明的数据更有说服力，也避免合规风险。

注入的三类异常（ground truth 会随数据一起输出，供测试与复盘校准）
----------------------------------------------------------------
A. 站点派单能力恶化：广州加盟站点 S03 最近 14 天派单时长显著变长
   → 连锁反应：上门准时率下降、时效类客诉上升。
B. 新品类质量不稳：充电桩服务最近 21 天一次完工率下降、返工率上升。
C. 成本异常上涨：搬家品类最近 14 天单均成本上涨约 35%，毛利率被侵蚀。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .models import CATEGORIES, CITIES, Order, Site

# ---------------------------------------------------------------- 站点档案

#: 直营 / 加盟混合的服务站点，对应 JD 中「直营站点运营支持」
SITE_PROFILE: tuple[tuple[str, str, str, str, int, float], ...] = (
    # site_id, 名称, 城市, 类型, 工程师数, 工单承接权重
    ("S01", "上海直营服务站", "上海", "直营", 14, 1.35),
    ("S02", "北京直营服务站", "北京", "直营", 11, 1.20),
    ("S03", "广州粤通服务商", "广州", "加盟", 9, 1.00),
    ("S04", "成都蓉城服务商", "成都", "加盟", 8, 0.85),
    ("S05", "武汉江城服务商", "武汉", "加盟", 7, 0.75),
    ("S06", "上海沪联服务商", "上海", "加盟", 8, 0.80),
)

#: 品类承接结构（权重）
CATEGORY_WEIGHTS: dict[str, float] = {
    "家电家居安装": 0.34,
    "家庭维修": 0.22,
    "3C服务": 0.18,
    "充电桩服务": 0.14,
    "搬家": 0.12,
}

#: 各品类的基准时长（小时）、单均收入/成本
#: promise_lead = 预约受理后到「约定上门时段」的排期提前量
CATEGORY_BASELINE: dict[str, dict[str, float]] = {
    "家电家居安装": {"dispatch": 2.2, "appoint": 3.0, "promise_lead": 14.0, "work": 2.0, "fee": 260, "cost": 168},
    "充电桩服务": {"dispatch": 4.0, "appoint": 5.0, "promise_lead": 30.0, "work": 4.0, "fee": 980, "cost": 690},
    "家庭维修": {"dispatch": 1.6, "appoint": 2.2, "promise_lead": 8.0, "work": 1.4, "fee": 190, "cost": 118},
    "3C服务": {"dispatch": 1.2, "appoint": 1.8, "promise_lead": 6.0, "work": 1.1, "fee": 220, "cost": 150},
    "搬家": {"dispatch": 3.2, "appoint": 4.5, "promise_lead": 36.0, "work": 5.0, "fee": 760, "cost": 520},
}

#: 直营 / 加盟的运营特征差异——体现"直营做标准、加盟做覆盖"的真实取舍：
#: 直营自有工程师、培训与质检更严 → 派单更快、返工与客诉更少，但人力与标准成本更高；
#: 加盟靠社会运力 → 成本更低、毛利更好，但质量波动更大。这条差异是「直营站点运营」分析的落脚点。
SITE_TYPE_EFFECTS: dict[str, dict[str, float]] = {
    "直营": {"dispatch": 0.90, "rework": 0.72, "complaint": 0.78, "cost": 1.10},
    "加盟": {"dispatch": 1.06, "rework": 1.18, "complaint": 1.16, "cost": 0.94},
}

# ---------------------------------------------------------------- 异常剧本


@dataclass
class Scenario:
    """一个被注入的异常剧本。"""

    scenario_id: str
    title: str
    dimension: str
    value: str
    metric: str
    days: int
    multiplier: float
    expect: str

    def window(self, anchor: datetime) -> tuple[datetime, datetime]:
        end = anchor
        start = anchor - timedelta(days=self.days)
        return start, end

    def to_dict(self, anchor: datetime) -> dict[str, Any]:
        start, end = self.window(anchor)
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "dimension": self.dimension,
            "value": self.value,
            "metric": self.metric,
            "window": [start.date().isoformat(), end.date().isoformat()],
            "multiplier": self.multiplier,
            "expect": self.expect,
        }


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        scenario_id="A_SITE_DISPATCH_DEGRADATION",
        title="广州加盟站点派单能力恶化",
        dimension="站点",
        value="S03",
        metric="dispatch_p90",
        days=14,
        multiplier=2.6,
        expect="S03 派单时长 P90 显著高于其他站点，且可归因到站内部分工程师，连带上门准时率下降、时效类客诉上升",
    ),
    Scenario(
        scenario_id="B_NEW_CATEGORY_QUALITY_DROP",
        title="充电桩品类一次完工率下降（返工率上升）",
        dimension="品类",
        value="充电桩服务",
        metric="rework_rate",
        days=21,
        multiplier=3.2,
        expect="充电桩服务返工率上升（一次完工率下降），归因指向工程师技能与新品类培训不足",
    ),
    Scenario(
        scenario_id="C_COST_INFLATION",
        title="搬家品类成本异常上涨",
        dimension="品类",
        value="搬家",
        metric="avg_cost",
        days=14,
        multiplier=1.35,
        expect="搬家单均成本上涨约 35%，毛利率下滑，需要复核结算规则与运力采购价",
    ),
)


# ---------------------------------------------------------------- 文本模板

_FEEDBACK_POOL: dict[tuple[str, str], list[str]] = {
    ("履约时效", "负"): [
        "下单两天了还没人联系我，预约时间一拖再拖，太耽误事。",
        "师傅比预约时间晚了两个多小时才到，也没有提前打电话告知。",
        "等了一整天没人上门，最后是我主动催了三次才安排。",
    ],
    ("服务技能", "负"): [
        "师傅装完当天就出问题了，第二次上门才弄好，水平不太行。",
        "充电桩装好之后一直报故障，返工了一次还是没解决。",
        "问题没找对，换了个配件还是老样子，白跑一趟。",
    ],
    ("费用争议", "负"): [
        "现场说要加收材料费，和下单时页面上写的价格不一样。",
        "搬运费比报价单高了不少，事先也没说清楚收费规则。",
        "费用明细看不懂，问客服和问师傅说法还不一致。",
    ],
    ("服务态度", "负"): [
        "师傅态度比较差，问两句就不耐烦。",
        "沟通很不顺畅，全程没有解释作业流程。",
    ],
    ("改约派单", "负"): [
        "派单后换了三个师傅，每个都说时间对不上要改约。",
        "约好的时间临时改到第二天，我又请了一次假。",
    ],
    ("物品损坏", "负"): [
        "安装过程中把墙面磕掉了一块，事后也没人处理。",
        "搬家把柜子边角磕坏了，理赔流程走了很久。",
    ],
    # 泛化负面原声：没有明确指向某个环节，但要能被情感分析识别为负面、被主题分析归入"其他"
    ("", "负"): [
        "整体体验不太好，等待时间比预期长，希望过程能主动同步进度。",
        "这次服务体验一般，沟通和安排都还有提升空间。",
        "不太满意，问题反馈之后处理得比较慢。",
    ],
    ("", "中"): ["还行吧，就是约的时间段太宽了，等了挺久。"],
    ("", "正"): [
        "师傅很专业，进门穿鞋套，作业完还帮忙清理了现场。",
        "预约很准时，价格也透明，服务体验不错。",
        "安装速度快，讲解清楚，售后也主动回访了。",
        "搬运师傅很卖力，东西一件没磕碰，性价比高。",
    ],
}


def _pick_feedback(rng: random.Random, theme: str, polarity: str) -> str:
    """按（主题, 情感）取用户原声；主题缺失时退回泛化语料，绝不跨情感复用。"""
    pool = _FEEDBACK_POOL.get((theme, polarity))
    if pool is None:
        pool = _FEEDBACK_POOL.get(("", polarity)) or _FEEDBACK_POOL[("", "中")]
    return rng.choice(pool)


# ---------------------------------------------------------------- 生成器


@dataclass
class Dataset:
    """一份可复现的合成数据集。"""

    sites: list[Site]
    orders: list[Order]
    ground_truth: list[dict[str, Any]] = field(default_factory=list)
    anchor: Optional[datetime] = None

    def to_ground_truth_json(self) -> str:
        return json.dumps(
            {
                "generated_at": (self.anchor or datetime.now()).isoformat(timespec="seconds"),
                "orders": len(self.orders),
                "scenarios": self.ground_truth,
            },
            ensure_ascii=False,
            indent=2,
        )


def build_sites() -> list[Site]:
    return [Site(site_id=sid, site_name=name, city=city, site_type=stype, engineers=eng) for sid, name, city, stype, eng, _ in SITE_PROFILE]


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    items = list(weights.items())
    total = sum(w for _, w in items)
    threshold = rng.random() * total
    cumulative = 0.0
    for key, weight in items:
        cumulative += weight
        if threshold <= cumulative:
            return key
    return items[-1][0]


def _active_scenarios(moment: datetime, category: str, site_id: str, anchor: datetime) -> list[Scenario]:
    hits: list[Scenario] = []
    for scenario in SCENARIOS:
        start, end = scenario.window(anchor)
        if not (start <= moment <= end):
            continue
        if scenario.dimension == "站点" and scenario.value == site_id:
            hits.append(scenario)
        elif scenario.dimension == "品类" and scenario.value == category:
            hits.append(scenario)
    return hits


@dataclass
class _Noise:
    """按环节的随机扰动（对数正态，贴近真实业务的长尾分布）。"""

    rng: random.Random

    def hours(self, base: float, sigma: float = 0.45) -> float:
        return max(0.1, base * self.rng.lognormvariate(0.0, sigma))


def generate_dataset(
    orders: int = 20000,
    days: int = 84,
    seed: int = 42,
    anchor: Optional[datetime] = None,
) -> Dataset:
    """生成一份完整数据集。

    :param orders: 生成工单量（默认 2 万单，既能撑起分层下钻，又能在 1 秒内跑完）
    :param days: 覆盖最近多少天
    :param seed: 随机种子（同种子同参数 => 结果完全可复现）
    :param anchor: 数据截止时间，默认取当天 23:59
    """
    rng = random.Random(seed)
    anchor = anchor or datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
    sites = build_sites()
    site_weights = {sid: weight for sid, _, _, _, _, weight in SITE_PROFILE}
    site_by_id = {site.site_id: site for site in sites}

    generated: list[Order] = []
    for index in range(orders):
        # ---- 时间与维度
        offset_days = rng.random() * days
        order_time = anchor - timedelta(days=offset_days, hours=rng.random() * 20)
        category = _weighted_choice(rng, CATEGORY_WEIGHTS)
        site_id = _weighted_choice(rng, site_weights)
        site = site_by_id[site_id]
        base = CATEGORY_BASELINE[category]
        scenarios = _active_scenarios(order_time, category, site_id, anchor)
        scenario_ids = {s.scenario_id for s in scenarios}

        # 工程师先于时长确定：派单恶化剧本只影响站内部分工程师（模拟排班/产能问题），
        # 这样归因下钻能真正指认到具体的人，而不是笼统地把整个站点打成异常。
        engineer_no = rng.randint(1, site.engineers)
        engineer_id = f"{site_id}-E{engineer_no:02d}"
        degraded = ("A_SITE_DISPATCH_DEGRADATION" in scenario_ids) and engineer_no <= max(2, site.engineers // 3)

        noise = _Noise(rng)
        effects = SITE_TYPE_EFFECTS.get(site.site_type, SITE_TYPE_EFFECTS["加盟"])
        # 周末与旺季扰动
        weekend_factor = 1.18 if order_time.weekday() >= 5 else 1.0

        # ---- 派单：S03 派单恶化剧本
        dispatch_base = base["dispatch"] * weekend_factor * effects["dispatch"]
        if degraded:
            dispatch_base *= 2.6
        dispatch_hours = noise.hours(dispatch_base, 0.5)
        dispatch_time = order_time + timedelta(hours=dispatch_hours)

        # ---- 预约受理 + 约定上门时段（两者是不同的业务事件，必须分开建模）
        appoint_hours = max(dispatch_hours, noise.hours(base["appoint"] * weekend_factor, 0.4))
        appoint_time = order_time + timedelta(hours=appoint_hours)
        promised_time = appoint_time + timedelta(hours=noise.hours(base["promise_lead"], 0.35))

        # ---- 改约：重新排期，约定时段整体后移
        reschedule_count = 0
        if rng.random() < 0.07 + (0.12 if degraded else 0.0):
            reschedule_count = 1 + (1 if rng.random() < 0.25 else 0)
            shift = timedelta(hours=24 * reschedule_count * rng.uniform(0.6, 1.2))
            promised_time += shift

        # ---- 上门：以约定时段为基准抖动；派单晚于时段时上门被顺延（真实业务的连锁反应）
        arrive_offset = noise.hours(0.5, 0.65) - 0.45
        arrive_time = promised_time + timedelta(hours=arrive_offset)
        if rng.random() < 0.05:  # 偶发明显迟到（堵车、前序工单延误等）
            arrive_time += timedelta(hours=rng.uniform(2, 8))
        arrive_time = max(arrive_time, dispatch_time + timedelta(hours=1))

        # ---- 完工与返工：充电桩质量剧本
        work_hours = noise.hours(base["work"], 0.4)
        rework_probability = 0.035 * effects["rework"]
        if "B_NEW_CATEGORY_QUALITY_DROP" in scenario_ids:
            rework_probability = 0.19 * effects["rework"]
        is_rework = rng.random() < rework_probability
        finish_time = arrive_time + timedelta(hours=work_hours)
        if is_rework:
            finish_time += timedelta(hours=noise.hours(base["work"] * 1.4 + 12, 0.5))

        # ---- 取消
        is_cancelled = rng.random() < 0.021
        if is_cancelled:
            finish_time = None  # 未完工

        # ---- 评价与客诉
        ontime_proxy = True
        window_hours = 1.0 if category != "搬家" else 2.0
        if promised_time is not None and arrive_time is not None:
            ontime_proxy = arrive_time <= promised_time + timedelta(hours=window_hours)

        negative_pressure = 0.010
        if not ontime_proxy:
            negative_pressure += 0.055
        if is_rework:
            negative_pressure += 0.090
        if reschedule_count > 0:
            negative_pressure += 0.030
        if degraded:
            negative_pressure += 0.040
        if "C_COST_INFLATION" in scenario_ids:
            negative_pressure += 0.020
        negative_pressure *= effects["complaint"]

        is_complaint = (not is_cancelled) and rng.random() < negative_pressure

        review_score: Optional[int] = None
        if not is_cancelled and rng.random() < 0.86:
            roll = rng.random()
            if is_complaint or is_rework:
                review_score = 1 if roll < 0.22 else 2 if roll < 0.50 else 3 if roll < 0.72 else 4 if roll < 0.92 else 5
            elif not ontime_proxy:
                review_score = 2 if roll < 0.15 else 3 if roll < 0.35 else 4 if roll < 0.72 else 5
            else:
                review_score = 5 if roll < 0.78 else 4 if roll < 0.96 else 3 if roll < 0.99 else 2

        # 情感 → 主题 → 原声：负面反馈必须落到具体痛点，避免生成"没有信息量的差评"
        polarity = "负" if (is_complaint or (review_score is not None and review_score <= 2)) else "正" if (review_score or 5) >= 4 else "中"
        theme = ""
        if is_complaint or polarity == "负":
            theme_weights = {"履约时效": 0.30, "服务技能": 0.18, "费用争议": 0.16, "服务态度": 0.12, "改约派单": 0.12, "物品损坏": 0.06, "其他": 0.06}
            if not ontime_proxy or degraded:
                theme_weights["履约时效"] += 0.25
                theme_weights["改约派单"] += 0.10
            if is_rework or "B_NEW_CATEGORY_QUALITY_DROP" in scenario_ids:
                theme_weights["服务技能"] += 0.30
            if "C_COST_INFLATION" in scenario_ids:
                theme_weights["费用争议"] += 0.25
            theme = _weighted_choice(rng, theme_weights)

        feedback = "" if is_cancelled and rng.random() < 0.5 else _pick_feedback(rng, theme, polarity)

        # ---- 收入与成本
        fee = round(base["fee"] * rng.uniform(0.82, 1.25), 2)
        cost_base = base["cost"] * rng.uniform(0.85, 1.15) * effects["cost"]
        if "C_COST_INFLATION" in scenario_ids:
            cost_base *= 1.35
        if is_rework:
            cost_base *= 1.28
        if reschedule_count > 0:
            cost_base *= 1.0 + 0.06 * reschedule_count
        cost = round(cost_base, 2)

        review_time = finish_time + timedelta(hours=rng.uniform(1, 40)) if (finish_time and review_score is not None) else None

        generated.append(
            Order(
                order_id=f"SO{anchor.year}{index + 1:06d}",
                category=category,
                site_id=site_id,
                site_name=site.site_name,
                site_type=site.site_type,
                city=site.city,
                engineer_id=engineer_id,
                order_time=order_time.replace(microsecond=0),
                appoint_time=appoint_time.replace(microsecond=0),
                promised_time=promised_time.replace(microsecond=0),
                dispatch_time=dispatch_time.replace(microsecond=0),
                arrive_time=arrive_time.replace(microsecond=0),
                finish_time=finish_time.replace(microsecond=0) if finish_time else None,
                review_time=review_time.replace(microsecond=0) if review_time else None,
                review_score=review_score,
                is_complaint=is_complaint,
                complaint_theme=theme if is_complaint else "",
                reschedule_count=reschedule_count,
                is_rework=is_rework if not is_cancelled else False,
                is_cancelled=is_cancelled,
                fee=fee,
                cost=cost,
                feedback=feedback,
            )
        )

    generated.sort(key=lambda o: o.order_time)
    return Dataset(
        sites=sites,
        orders=generated,
        ground_truth=[scenario.to_dict(anchor) for scenario in SCENARIOS],
        anchor=anchor,
    )
