"""命令行入口：一条命令跑通「数据 → 指标 → 异常 → 归因 → 报告」全流程。

示例
----
python -m fulfillment_copilot demo                     # 一键跑通，产出全部交付物
python -m fulfillment_copilot kpi                      # 只看核心指标与分层对比
python -m fulfillment_copilot anomaly                  # 只看异常与归因
python -m fulfillment_copilot report --type weekly     # 生成周报（可接真实大模型）
python -m fulfillment_copilot brd --topic dispatch     # 生成需求文档草稿
python -m fulfillment_copilot dashboard                # 生成 HTML 看板
python -m fulfillment_copilot excel                     # 生成 Excel 工作簿
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .ai.provider import get_provider, provider_banner
from .ai.workflows import (
    BRD_TOPICS,
    anomaly_briefing,
    brd_draft,
    build_evidence,
    dispatch_advice,
    feedback_insight,
    weekly_report,
)
from .anomaly import detect_all
from .config import Config
from .dashboard import render_dashboard
from .datagen import Dataset, generate_dataset
from .excel import ExcelUnavailable, excel_available, write_workbook
from .metrics import overall_metrics, scorecard as build_scorecard, slice_by
from .models import write_orders_csv
from .report import dimension_table, md_table, overview_block

DEFAULT_OUTPUT = "output"


# ---------------------------------------------------------------- 公共装配


def _banner(title: str) -> None:
    print(f"\n=== {title} ===")


def load_or_generate(args: argparse.Namespace) -> Dataset:
    """有 --data 就读取 CSV，否则生成合成数据。"""
    data_path = getattr(args, "data", None)
    if data_path:
        from .models import read_orders_csv
        from .datagen import build_sites

        orders = read_orders_csv(data_path)
        return Dataset(sites=build_sites(), orders=orders, ground_truth=[], anchor=None)
    return generate_dataset(orders=args.orders, days=args.days, seed=args.seed)


def _output_dir(args: argparse.Namespace) -> Path:
    path = Path(args.out)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _provider(args: argparse.Namespace):
    return get_provider(getattr(args, "provider", "auto"))


# ---------------------------------------------------------------- 各子命令


def cmd_gen_data(args: argparse.Namespace) -> int:
    dataset = load_or_generate(args)
    out = _output_dir(args) / "data"
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "orders.csv"
    count = write_orders_csv(dataset.orders, str(csv_path))
    truth_path = out / "ground_truth.json"
    truth_path.write_text(dataset.to_ground_truth_json(), encoding="utf-8")
    _banner("数据生成完成")
    print(f"工单明细：{csv_path}（{count} 行）")
    print(f"注入异常：{truth_path}（{len(dataset.ground_truth)} 个剧本，供检测效果验证）")
    return 0


def cmd_kpi(args: argparse.Namespace) -> int:
    dataset = load_or_generate(args)
    cfg = Config.load(args.config)
    metrics = overall_metrics(dataset.orders, cfg)
    card = build_scorecard(metrics, cfg)
    _banner("核心指标")
    print(overview_block(metrics, (
        "order_count", "dispatch_ontime_rate", "arrive_ontime_rate", "sla_achieve_rate",
        "first_fix_rate", "rework_rate", "reschedule_rate", "cancel_rate",
        "complaint_rate", "bad_review_rate", "nps", "dispatch_p90", "total_p90", "avg_cost", "gross_margin",
    )))
    print(f"\n履约质量综合得分：{card.get('total')}（{card.get('grade')} 级）")
    for dimension in ("站点", "品类", "城市"):
        _banner(f"分层对比 · {dimension}")
        rows = slice_by(dataset.orders, dimension, cfg, keys=(
            "dispatch_p90", "arrive_ontime_rate", "first_fix_rate", "complaint_rate", "gross_margin",
        ))
        print(dimension_table(rows, (
            "dispatch_p90", "arrive_ontime_rate", "first_fix_rate", "complaint_rate", "gross_margin",
        ), dimension))
    return 0


def cmd_anomaly(args: argparse.Namespace) -> int:
    dataset = load_or_generate(args)
    cfg = Config.load(args.config)
    result = detect_all(dataset.orders, cfg, anchor=dataset.anchor)
    _banner("统计预警（近 14 天 vs 历史基线）")
    for item in result["dimension"]:
        print(f"[{item['severity']}] {item['message']}（样本 {item['recent_orders']}）")
        print(f"      建议：{item['suggestion']}")
    _banner("归因下钻")
    for item in result["attributions"][:3]:
        print(f"- {item['conclusion']}")
        for row in item["rows"][:3]:
            print(f"    {row['drill']}「{row['label']}」占 {row['share'] * 100:.1f}%，"
                  f"{item['metric_name']} {row['metric_display']}，贡献度 {row['contribution']:.3f}")
    rule = result["rule"]
    _banner("需跟进工单分类")
    print(md_table(["异常类型", "命中工单", "占比"],
                   [[name, payload["count"], f"{payload['rate'] * 100:.2f}%"] for name, payload in rule["by_type"].items()]))
    print(f"\n需跟进（高+中）：{rule['follow_up_orders']} 单，占 {(rule['follow_up_rate'] or 0) * 100:.1f}%")
    return 0


def _write_reports(evidence: dict, out_dir: Path, provider, use_ai: bool, topics: list[str] | None = None) -> list[Path]:
    reports = out_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    artifacts = {
        "weekly_report.md": weekly_report(evidence, provider, use_ai),
        "anomaly_briefing.md": anomaly_briefing(evidence, provider, use_ai),
        "feedback_insight.md": feedback_insight(evidence, provider, use_ai),
        "dispatch_advice.md": dispatch_advice(evidence, provider, use_ai),
    }
    for topic in (topics or ["dispatch"]):
        artifacts[f"brd_{topic}.md"] = brd_draft(evidence, topic, provider, use_ai)
    for name, text in artifacts.items():
        path = reports / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def cmd_report(args: argparse.Namespace) -> int:
    dataset = load_or_generate(args)
    cfg = Config.load(args.config)
    provider = _provider(args)
    out = _output_dir(args)
    evidence = build_evidence(dataset.orders, cfg, sites=dataset.sites, anchor=dataset.anchor, ground_truth=dataset.ground_truth)
    _banner("生成报告")
    print(provider_banner(provider))
    if args.type == "weekly":
        text = weekly_report(evidence, provider, not args.no_ai)
        path = out / "weekly_report.md"
    elif args.type == "anomaly":
        text = anomaly_briefing(evidence, provider, not args.no_ai)
        path = out / "anomaly_briefing.md"
    elif args.type == "feedback":
        text = feedback_insight(evidence, provider, not args.no_ai)
        path = out / "feedback_insight.md"
    elif args.type == "dispatch":
        text = dispatch_advice(evidence, provider, not args.no_ai)
        path = out / "dispatch_advice.md"
    else:
        print(f"未知报告类型：{args.type}", file=sys.stderr)
        return 2
    path.write_text(text, encoding="utf-8")
    print(f"已生成：{path}")
    print(text[:1200])
    return 0


def cmd_brd(args: argparse.Namespace) -> int:
    dataset = load_or_generate(args)
    cfg = Config.load(args.config)
    provider = _provider(args)
    evidence = build_evidence(dataset.orders, cfg, sites=dataset.sites, anchor=dataset.anchor)
    topic = args.topic
    text = brd_draft(evidence, topic, provider, not args.no_ai)
    out = _output_dir(args) / f"brd_{topic}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    _banner(f"需求文档草稿（{BRD_TOPICS.get(topic, {}).get('title', topic)}）")
    print(f"已生成：{out}")
    print(text[:900])
    return 0


def cmd_excel(args: argparse.Namespace) -> int:
    dataset = load_or_generate(args)
    cfg = Config.load(args.config)
    evidence = build_evidence(dataset.orders, cfg, sites=dataset.sites, anchor=dataset.anchor)
    out = _output_dir(args) / "kpi_workbook.xlsx"
    try:
        write_workbook(evidence, str(out))
    except ExcelUnavailable as error:
        _banner("Excel 导出跳过")
        print(str(error))
        print("（其余报告不受影响，可继续生成 Markdown / 看板）")
        return 0
    _banner("Excel 导出完成")
    print(f"多 Sheet 工作簿：{out}（总览 / 分站点 / 分品类 / 分城市 / 周趋势 / 指标异常 / 需跟进工单 / 用户声音 / 派单评分卡 / 口径说明）")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    dataset = load_or_generate(args)
    cfg = Config.load(args.config)
    provider = _provider(args)
    evidence = build_evidence(dataset.orders, cfg, sites=dataset.sites, anchor=dataset.anchor)
    html = render_dashboard(evidence, provider_banner(provider))
    out = _output_dir(args) / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    _banner("看板生成完成")
    print(f"单文件 HTML 看板：{out}（双击即可打开，无需联网）")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    dataset = load_or_generate(args)
    cfg = Config.load(args.config)
    provider = _provider(args)
    out = _output_dir(args)

    _banner("1/5 生成合成数据")
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / "orders.csv"
    write_orders_csv(dataset.orders, str(csv_path))
    (data_dir / "ground_truth.json").write_text(dataset.to_ground_truth_json(), encoding="utf-8")
    print(f"{len(dataset.orders)} 单 → {csv_path}")

    _banner("2/5 计算指标与异常")
    evidence = build_evidence(dataset.orders, cfg, sites=dataset.sites, anchor=dataset.anchor, ground_truth=dataset.ground_truth)
    overview = evidence["overview"]
    print(f"综合得分 {evidence['scorecard'].get('total')}（{evidence['scorecard'].get('grade')} 级）| "
          f"上门准时率 {overview['arrive_ontime_rate']['display']} | "
          f"一次完工率 {overview['first_fix_rate']['display']} | "
          f"客诉率 {overview['complaint_rate']['display']}")
    print(f"统计预警 {len(evidence['anomalies']['dimension'])} 项 | "
          f"需跟进工单 {evidence['anomalies']['rule']['follow_up_orders']} 单 | "
          f"归因结果 {len(evidence['anomalies']['attributions'])} 条")

    _banner("3/5 生成运营报告")
    print(provider_banner(provider))
    written = _write_reports(evidence, out, provider, not args.no_ai, topics=["dispatch", "alert"])
    for path in written:
        print(f"  {path.relative_to(out)}")

    _banner("4/5 生成 HTML 看板")
    dashboard_path = out / "dashboard.html"
    dashboard_path.write_text(render_dashboard(evidence, provider_banner(provider)), encoding="utf-8")
    print(f"  {dashboard_path.relative_to(out)}")

    _banner("5/5 导出 Excel 工作簿")
    if not excel_available():
        print("  跳过：未安装 openpyxl（pip install openpyxl 后重跑即可）")
    else:
        workbook_path = out / "kpi_workbook.xlsx"
        write_workbook(evidence, str(workbook_path))
        print(f"  {workbook_path.relative_to(out)}")

    _banner("完成")
    print(f"全部交付物位于：{out.resolve()}（耗时 {time.perf_counter() - started:.1f} 秒）")
    print("建议先打开 dashboard.html 看全局，再看 reports/weekly_report.md 读结论。")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """验证异常检测是否抓到了注入的异常剧本（自检，也是 CI 的一环）。"""
    dataset = load_or_generate(args)
    cfg = Config.load(args.config)
    result = detect_all(dataset.orders, cfg, anchor=dataset.anchor)
    detected = {(item["dimension"], item["value"], item["metric"]) for item in result["dimension"]}
    _banner("异常检测自检（对照注入剧本）")
    hits = 0
    for scenario in dataset.ground_truth:
        target = (scenario["dimension"], scenario["value"], scenario["metric"])
        found = target in detected
        hits += 1 if found else 0
        mark = "✔ 已识别" if found else "✘ 未识别"
        print(f"{mark}  {scenario['scenario_id']}：{scenario['title']}（{scenario['dimension']}「{scenario['value']}」{scenario['metric']}）")
        if found:
            item = next(i for i in result["dimension"] if (i["dimension"], i["value"], i["metric"]) == target)
            print(f"       基线 {item['baseline_value']} → 当前 {item['current_value']}（{item['deviation'] * 100:+.1f}%，"
                  f"稳健 z={item['robust_z']}，严重度 {item['severity']}）")
    print(f"\n命中 {hits}/{len(dataset.ground_truth)} 个注入异常剧本")
    return 0 if hits == len(dataset.ground_truth) else 1


# ---------------------------------------------------------------- 参数解析


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fulfillment-copilot",
        description="服务履约质量智能运营助手：一站式到家服务的履约监控、异常归因与 AI 报告生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python -m fulfillment_copilot demo --orders 20000",
    )
    parser.add_argument("--version", action="version", version=f"fulfillment-copilot {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--orders", type=int, default=20000, help="合成工单量（默认 20000）")
        sub.add_argument("--days", type=int, default=84, help="覆盖天数（默认 84）")
        sub.add_argument("--seed", type=int, default=42, help="随机种子，保证可复现（默认 42）")
        sub.add_argument("--data", type=str, default=None, help="改为读取既有工单 CSV（跳过合成数据）")
        sub.add_argument("--config", type=str, default=None, help="运营规则与阈值配置文件路径")
        sub.add_argument("--out", type=str, default=DEFAULT_OUTPUT, help=f"输出目录（默认 {DEFAULT_OUTPUT}）")
        sub.add_argument("--provider", type=str, default="auto", choices=["auto", "offline", "openai"],
                         help="大模型通道：auto（有环境变量就用）/ offline（纯模板）/ openai")
        sub.add_argument("--no-ai", action="store_true", help="强制使用确定性模板，不调用大模型")

    for name, handler, help_text in (
        ("demo", cmd_demo, "一键跑通全部流程并产出所有交付物（推荐）"),
        ("gen-data", cmd_gen_data, "只生成合成数据（CSV + 注入异常说明）"),
        ("kpi", cmd_kpi, "输出核心指标与站点/品类/城市分层对比"),
        ("anomaly", cmd_anomaly, "输出异常清单、归因下钻与需跟进工单"),
        ("verify", cmd_verify, "对照注入的异常剧本，自检检测效果"),
        ("excel", cmd_excel, "导出多 Sheet Excel 工作簿"),
        ("dashboard", cmd_dashboard, "导出单文件 HTML 看板"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        add_common(sub)
        sub.set_defaults(func=handler)

    report = subparsers.add_parser("report", help="生成指定类型的报告（可接真实大模型）")
    add_common(report)
    report.add_argument("--type", default="weekly", choices=["weekly", "anomaly", "feedback", "dispatch"])
    report.set_defaults(func=cmd_report)

    brd = subparsers.add_parser("brd", help="生成业务需求文档（BRD）草稿")
    add_common(brd)
    brd.add_argument("--topic", default="dispatch", choices=sorted(BRD_TOPICS.keys()))
    brd.set_defaults(func=cmd_brd)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    # 目录由各写入型子命令按需创建（kpi/anomaly/verify 这类只读命令不产生 output/）
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
