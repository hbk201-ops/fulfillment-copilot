"""运营规则与监控阈值配置。

业务同学可以直接修改 ``config/thresholds.json`` 调整时效口径、异常阈值和考核权重，
代码无需改动——对应 JD 中「推动运营规则完善」的能力。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: 仓库根目录（src/fulfillment_copilot/config.py -> 上三级）
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "thresholds.json"

#: 内置兜底配置：即使配置文件缺失（例如被单独打包）也能运行
DEFAULTS: dict[str, Any] = {
    "sla": {
        "家电家居安装": {"dispatch_hours": 4, "appoint_hours": 6, "finish_hours": 4, "total_hours": 48, "arrive_window_hours": 1},
        "充电桩服务": {"dispatch_hours": 6, "appoint_hours": 8, "finish_hours": 6, "total_hours": 72, "arrive_window_hours": 2},
        "家庭维修": {"dispatch_hours": 3, "appoint_hours": 4, "finish_hours": 3, "total_hours": 36, "arrive_window_hours": 1},
        "3C服务": {"dispatch_hours": 3, "appoint_hours": 4, "finish_hours": 2, "total_hours": 24, "arrive_window_hours": 1},
        "搬家": {"dispatch_hours": 6, "appoint_hours": 8, "finish_hours": 8, "total_hours": 96, "arrive_window_hours": 2},
    },
    "anomaly": {
        "low_score_threshold": 3,
        "bad_review_threshold": 2,
        "reschedule_warn_count": 2,
        "robust_zscore_threshold": 2.5,
        "mom_change_threshold": 0.30,
        "min_sample_size": 20,
        "cost_deviation_threshold": 0.25,
    },
    "scorecard": {
        "weights": {"timeliness": 0.40, "quality": 0.35, "experience": 0.25},
        "targets": {"ontime_rate": 0.95, "first_fix_rate": 0.92, "complaint_rate": 0.02, "bad_review_rate": 0.02},
    },
    "monitor_dimensions": ["站点", "品类", "城市", "工程师"],
}

#: 兜底 SLA（品类未配置时使用）
FALLBACK_SLA: dict[str, float] = {
    "dispatch_hours": 4.0,
    "appoint_hours": 6.0,
    "finish_hours": 4.0,
    "total_hours": 48.0,
    "arrive_window_hours": 1.0,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class Config:
    """已解析生效的运营规则。"""

    sla: dict[str, dict[str, float]] = field(default_factory=dict)
    anomaly: dict[str, float] = field(default_factory=dict)
    scorecard: dict[str, Any] = field(default_factory=dict)
    monitor_dimensions: list[str] = field(default_factory=list)
    source: str = "builtin"

    # ------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        """加载配置：显式路径 > 环境变量 ``FQC_CONFIG`` > 仓库默认文件 > 内置兜底。"""
        candidates: list[Path] = []
        if path is not None:
            candidates.append(Path(path))
        env_path = os.environ.get("FQC_CONFIG")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(DEFAULT_CONFIG_PATH)

        for candidate in candidates:
            if candidate.is_file():
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                merged = _deep_merge(DEFAULTS, raw)
                return cls(
                    sla=merged["sla"],
                    anomaly=merged["anomaly"],
                    scorecard=merged["scorecard"],
                    monitor_dimensions=list(merged["monitor_dimensions"]),
                    source=str(candidate),
                )
        return cls(
            sla=dict(DEFAULTS["sla"]),
            anomaly=dict(DEFAULTS["anomaly"]),
            scorecard=dict(DEFAULTS["scorecard"]),
            monitor_dimensions=list(DEFAULTS["monitor_dimensions"]),
            source="builtin",
        )

    # ------------------------------------------------------------ 查询
    def sla_for(self, category: str) -> dict[str, float]:
        """取某品类的时效承诺，缺省回落到通用 SLA。"""
        merged = dict(FALLBACK_SLA)
        merged.update({k: float(v) for k, v in self.sla.get(category, {}).items()})
        return merged

    def threshold(self, key: str, default: float = 0.0) -> float:
        return float(self.anomaly.get(key, default))

    # ------------------------------------------------------------ 综合得分
    def weight(self, category: str) -> float:
        return float(self.scorecard.get("weights", {}).get(category, 0.0))

    def target(self, key: str, default: float = 1.0) -> float:
        return float(self.scorecard.get("targets", {}).get(key, default))

    def as_dict(self) -> dict[str, Any]:
        return {
            "sla": self.sla,
            "anomaly": self.anomaly,
            "scorecard": self.scorecard,
            "monitor_dimensions": self.monitor_dimensions,
            "source": self.source,
        }
