"""重建 examples/ 下的样例产物。

为什么要把产物提交进仓库：HR 和面试官不一定愿意配环境跑代码。
仓库里直接放好"看板 + 周报 + 异常专报 + BRD + Excel"，点开就能评估项目质量。

用法::

    python scripts/build_examples.py                # 默认 20000 单
    python scripts/build_examples.py --orders 8000  # 小规模快速重建
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fulfillment_copilot.ai.workflows import (  # noqa: E402
    anomaly_briefing,
    brd_draft,
    build_evidence,
    dispatch_advice,
    feedback_insight,
    weekly_report,
)
from fulfillment_copilot.config import Config  # noqa: E402
from fulfillment_copilot.dashboard import render_dashboard  # noqa: E402
from fulfillment_copilot.datagen import generate_dataset  # noqa: E402
from fulfillment_copilot.excel import ExcelUnavailable, write_workbook  # noqa: E402
from fulfillment_copilot.metrics import format_metric, slice_by  # noqa: E402
from fulfillment_copilot.models import write_orders_csv  # noqa: E402

DIMENSION_METRICS = ("dispatch_p90", "arrive_ontime_rate", "first_fix_rate", "complaint_rate", "bad_review_rate", "gross_margin")


def main() -> int:
    parser = argparse.ArgumentParser(description="重建 examples/ 样例产物")
    parser.add_argument("--orders", type=int, default=20000, help="合成工单量")
    parser.add_argument("--sample-rows", type=int, default=400, help="提交进仓库的明细行数（控制仓库体积）")
    parser.add_argument("--out", type=str, default=str(REPO / "examples"), help="输出目录")
    args = parser.parse_args()

    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "reports").mkdir(parents=True, exist_ok=True)

    dataset = generate_dataset(orders=args.orders, days=84, seed=42)
    cfg = Config.load()

    sample = dataset.orders[: args.sample_rows]
    write_orders_csv(sample, str(out / "data" / "orders_sample.csv"))
    (out / "data" / "ground_truth.json").write_text(dataset.to_ground_truth_json(), encoding="utf-8")

    evidence = build_evidence(dataset.orders, cfg, sites=dataset.sites, anchor=dataset.anchor, ground_truth=dataset.ground_truth)
    artifacts = {
        "weekly_report.md": weekly_report(evidence),
        "anomaly_briefing.md": anomaly_briefing(evidence),
        "feedback_insight.md": feedback_insight(evidence),
        "brd_dispatch.md": brd_draft(evidence, "dispatch"),
        "brd_alert.md": brd_draft(evidence, "alert"),
        "brd_appointment.md": brd_draft(evidence, "appointment"),
        "dispatch_advice.md": dispatch_advice(evidence),
    }
    for name, text in artifacts.items():
        (out / "reports" / name).write_text(text, encoding="utf-8")

    (out / "dashboard.html").write_text(render_dashboard(evidence, "样例产物：由确定性模板离线生成"), encoding="utf-8")
    try:
        write_workbook(evidence, str(out / "kpi_workbook.xlsx"))
        excel_note = "kpi_workbook.xlsx 已导出（10 个 Sheet）"
    except ExcelUnavailable:
        excel_note = "未安装 openpyxl，跳过 Excel 导出"

    # ---- 控制台摘要（同时用于文档取数，保证文档与产物一致）
    print(f"工单 {len(dataset.orders)} 单 | 明细样例 {len(sample)} 行 | {excel_note}")
    print(f"综合得分 {evidence['scorecard']['total']}（{evidence['scorecard']['grade']}）")
    for key in ("arrive_ontime_rate", "sla_achieve_rate", "first_fix_rate", "complaint_rate", "bad_review_rate", "nps", "avg_cost", "gross_margin"):
        print(f"  {evidence['overview'][key]['name']}: {evidence['overview'][key]['display']}")
    print(f"异常 {len(evidence['anomalies']['dimension'])} 项 | 需跟进工单 {evidence['anomalies']['rule']['follow_up_orders']} "
          f"({(evidence['anomalies']['rule']['follow_up_rate'] or 0) * 100:.1f}%)")

    print("\n[站点类型对比]")
    for row in slice_by(dataset.orders, "站点类型", cfg, keys=DIMENSION_METRICS):
        print(f"  {row['value']} n={row['orders']} " + " ".join(f"{k}={format_metric(k, row.get(k))}" for k in DIMENSION_METRICS))

    print("\n[站点明细]")
    for row in slice_by(dataset.orders, "站点", cfg, keys=DIMENSION_METRICS):
        print(f"  {row['value']} {row['label']} n={row['orders']} " + " ".join(f"{k}={format_metric(k, row.get(k))}" for k in DIMENSION_METRICS))

    print("\n[派单评分卡]")
    for row in evidence["dispatch"]["sites"]:
        print(f"  {row['site_id']} {row['site_name']} {row['site_type']} 工程师{row['engineers']} 单/人/天={row['orders_per_engineer_day']} "
              f"得分={row['score']} 建议={row['recommendation']}")

    print("\n[工程师画像 Top5]")
    for row in evidence["dispatch"]["engineers"][:5]:
        print(f"  {row['engineer_id']} 单量{row['orders']} 得分{row['quality_score']} 一次完工率={format_metric('first_fix_rate', row['first_fix_rate'])} 建议={row['advice']}")

    print("\n[文本主题]")
    for theme in evidence["feedback"]["themes"]:
        print(f"  {theme['theme']} {theme['count']} 条 {theme['share'] * 100:.1f}%")

    print("\n[Top 异常]")
    for item in evidence["anomalies"]["dimension"][:6]:
        print(f"  [{item['severity']}] {item['dimension']}「{item['label']}」{item['metric_name']} "
              f"{format_metric(item['metric'], item['baseline_value'])} → {format_metric(item['metric'], item['current_value'])} "
              f"({item['deviation'] * 100:+.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
