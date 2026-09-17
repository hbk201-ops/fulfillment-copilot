#!/usr/bin/env python
"""零配置入口：未安装本包也能直接运行。

    python run.py demo          # 一键跑通全流程
    python run.py kpi           # 只看指标
    python run.py --help        # 全部命令

已安装（``pip install -e .``）后可用等价命令 ``fulfillment-copilot demo``。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fulfillment_copilot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
