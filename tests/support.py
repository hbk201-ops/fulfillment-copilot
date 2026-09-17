"""测试共用夹具与小工具。

同时负责把 ``src`` 加入 ``sys.path``，这样在未执行 ``pip install -e .`` 的环境里
（例如 CI 的 stdlib 任务）也能直接跑测试。
"""

from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 把临时目录指到仓库内：CI/沙箱环境下系统临时目录可能不可写，
# 而测试要创建临时输出目录（CLI 端到端测试会写报告文件）。
_TMP = Path(__file__).resolve().parents[1] / ".tmp"
_TMP.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_TMP)

from fulfillment_copilot.ai.workflows import build_evidence  # noqa: E402
from fulfillment_copilot.config import Config  # noqa: E402
from fulfillment_copilot.datagen import Dataset, generate_dataset  # noqa: E402
from fulfillment_copilot.models import Order  # noqa: E402


@lru_cache(maxsize=4)
def dataset(orders: int = 8000) -> Dataset:
    """固定种子的合成数据集（默认 8000 单，兼顾检测灵敏度与测试速度）。"""
    return generate_dataset(orders=orders, days=84, seed=42)


@lru_cache(maxsize=2)
def config() -> Config:
    return Config.load()


@lru_cache(maxsize=4)
def evidence(orders: int = 8000) -> dict:
    ds = dataset(orders)
    return build_evidence(ds.orders, config(), sites=ds.sites, anchor=ds.anchor, ground_truth=ds.ground_truth)


@contextlib.contextmanager
def temp_dir(name: str = "case"):
    """仓库内的临时目录。

    不用 ``tempfile.TemporaryDirectory``：在受限环境（CI 沙箱）下它退出时会 chmod 失败，
    导致测试因为"清理不了临时目录"而失败——那是环境问题，不该污染测试结论。
    """
    path = _TMP / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


BASE = datetime(2026, 1, 5, 9, 0)


def make_order(
    order_id: str,
    *,
    category: str = "家庭维修",
    site_id: str = "S01",
    site_name: str = "上海直营服务站",
    site_type: str = "直营",
    city: str = "上海",
    engineer_id: str = "S01-E01",
    order_time: datetime = BASE,
    appoint_time=None,
    promised_time=None,
    dispatch_time=None,
    arrive_time=None,
    finish_time=None,
    review_score=None,
    is_complaint: bool = False,
    complaint_theme: str = "",
    reschedule_count: int = 0,
    is_rework: bool = False,
    is_cancelled: bool = False,
    fee: float = 0.0,
    cost: float = 0.0,
    feedback: str = "",
) -> Order:
    """手工构造一笔工单，用于口径的精确断言。"""
    return Order(
        order_id=order_id,
        category=category,
        site_id=site_id,
        site_name=site_name,
        site_type=site_type,
        city=city,
        engineer_id=engineer_id,
        order_time=order_time,
        appoint_time=appoint_time,
        promised_time=promised_time,
        dispatch_time=dispatch_time,
        arrive_time=arrive_time,
        finish_time=finish_time,
        review_score=review_score,
        is_complaint=is_complaint,
        complaint_theme=complaint_theme,
        reschedule_count=reschedule_count,
        is_rework=is_rework,
        is_cancelled=is_cancelled,
        fee=fee,
        cost=cost,
        feedback=feedback,
    )
