"""服务履约链路数据模型。

围绕一站式服务场景，建模「下单 → 预约 → 派单 → 上门 → 完工 → 评价」全链路，
覆盖家电家居安装、充电桩服务、家庭维修、3C 服务、搬家五类业务。
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import cached_property
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------- 业务枚举

CATEGORIES: tuple[str, ...] = (
    "家电家居安装",
    "充电桩服务",
    "家庭维修",
    "3C服务",
    "搬家",
)

SITE_TYPES: tuple[str, ...] = ("直营", "加盟")

CITIES: tuple[str, ...] = ("上海", "北京", "广州", "成都", "武汉")

#: 客诉/差评主题分类（与 feedback.py 的词典一致，便于交叉校验）
COMPLAINT_THEMES: tuple[str, ...] = (
    "履约时效",
    "服务技能",
    "费用争议",
    "服务态度",
    "改约派单",
    "物品损坏",
    "其他",
)

#: 环节名称，用于报告可读性
STAGES: tuple[str, ...] = ("下单", "预约", "派单", "上门", "完工", "评价")

_DT_FIELDS = (
    "order_time",
    "appoint_time",
    "promised_time",
    "dispatch_time",
    "arrive_time",
    "finish_time",
    "review_time",
)
_BOOL_FIELDS = ("is_complaint", "is_rework", "is_cancelled")
_INT_FIELDS = ("review_score", "reschedule_count")


def _parse_dt(value: str | None) -> Optional[datetime]:
    if value in (None, "", "None"):
        return None
    return datetime.fromisoformat(value)


def _parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


@dataclass
class Site:
    """服务站点/服务商档案。直营站点对应 JD 中「直营站点运营支持」。"""

    site_id: str
    site_name: str
    city: str
    site_type: str
    engineers: int

    @property
    def is_direct(self) -> bool:
        return self.site_type == "直营"


@dataclass
class Order:
    """一笔履约工单的全链路记录。"""

    order_id: str
    order_id: str
    category: str
    site_id: str
    site_name: str
    site_type: str
    city: str
    engineer_id: str
    order_time: datetime
    appoint_time: Optional[datetime] = None
    promised_time: Optional[datetime] = None
    dispatch_time: Optional[datetime] = None
    arrive_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    review_time: Optional[datetime] = None
    review_score: Optional[int] = None
    is_complaint: bool = False
    complaint_theme: str = ""
    reschedule_count: int = 0
    is_rework: bool = False
    is_cancelled: bool = False
    fee: float = 0.0
    cost: float = 0.0
    feedback: str = ""

    # ------------------------------------------------------------ 派生指标
    @staticmethod
    def _hours(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
        """两个时间点之间的小时数；任一端缺失返回 None（不把缺失当成 0）。"""
        if start is None or end is None:
            return None
        return round((end - start).total_seconds() / 3600.0, 3)

    # 用 cached_property 缓存时长计算：2 万单 × 20+ 指标的分析里，
    # 同一个时长会被反复读取，缓存后 build_evidence 耗时下降约一半。
    @cached_property
    def dispatch_hours(self) -> Optional[float]:
        """下单 → 派单，衡量接单响应能力。"""
        return self._hours(self.order_time, self.dispatch_time)

    @cached_property
    def arrive_delay_hours(self) -> Optional[float]:
        """实际上门相对约定上门时段的偏差（正数表示迟到）。"""
        return self._hours(self.promised_time, self.arrive_time)

    @cached_property
    def promise_lead_hours(self) -> Optional[float]:
        """预约受理 → 约定上门时段的排期提前量。"""
        return self._hours(self.appoint_time, self.promised_time)

    @cached_property
    def appoint_hours(self) -> Optional[float]:
        """下单 → 预约成功，衡量预约受理时效。"""
        return self._hours(self.order_time, self.appoint_time)

    @cached_property
    def arrive_hours(self) -> Optional[float]:
        """下单 → 工程师上门，衡量整体响应速度。"""
        return self._hours(self.order_time, self.arrive_time)

    @cached_property
    def finish_hours(self) -> Optional[float]:
        """上门 → 完工，衡量现场作业效率。"""
        return self._hours(self.arrive_time, self.finish_time)

    @cached_property
    def total_hours(self) -> Optional[float]:
        """下单 → 完工，全链路履约时长。"""
        return self._hours(self.order_time, self.finish_time)

    @property
    def is_finished(self) -> bool:
        return self.finish_time is not None and not self.is_cancelled

    @property
    def is_first_fix(self) -> Optional[bool]:
        """一次完工（首次上门即解决，无返工）。未完工返回 None。"""
        if not self.is_finished:
            return None
        return not self.is_rework

    @cached_property
    def gross_profit(self) -> float:
        return round(self.fee - self.cost, 2)

    # ------------------------------------------------------------ 序列化
    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        for name in _DT_FIELDS:
            value = row[name]
            row[name] = "" if value is None else value.isoformat()
        return row

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Order":
        data: dict[str, Any] = dict(row)
        for name in _DT_FIELDS:
            data[name] = _parse_dt(data.get(name, ""))
        for name in _BOOL_FIELDS:
            data[name] = _parse_bool(data.get(name, ""))
        for name in _INT_FIELDS:
            raw = data.get(name, "")
            data[name] = None if raw in ("", "None") else int(float(raw))
        for name in ("fee", "cost"):
            raw = data.get(name, "")
            data[name] = 0.0 if raw in ("", "None") else float(raw)
        return cls(**data)  # type: ignore[arg-type]


def write_orders_csv(orders: Iterable[Order], path: str) -> int:
    """把工单明细落成 CSV（等价于业务同学导出的 Excel 明细表）。"""
    rows = list(orders)
    fieldnames = list(Order.__dataclass_fields__.keys())  # type: ignore[attr-defined]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for order in rows:
            writer.writerow(order.to_row())
    return len(rows)


def read_orders_csv(path: str) -> list[Order]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return [Order.from_row(row) for row in csv.DictReader(handle)]
