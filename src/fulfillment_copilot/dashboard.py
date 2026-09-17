"""HTML 看板：单文件、零外部依赖、双击即可打开。

给"不想跑代码只想看结果"的人准备：面试官、业务方、站点负责人打开一个 HTML 就能看到
履约质量的整体水位、异常清单、归因结论与下周动作。图表是内联 SVG，不联网也能显示。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from .charts import bar_chart, donut_chart, line_chart
from .metrics import METRICS, format_metric

_CSS = """
:root{--bg:#F5F7FB;--card:#FFFFFF;--ink:#1F2937;--muted:#6B7280;--line:#E6EAF2;
--brand:#4D6BFE;--good:#22C3A6;--warn:#F5A623;--bad:#E4536B;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;}
header{background:linear-gradient(120deg,#243B8F,#4D6BFE);color:#fff;padding:26px 34px 22px;}
header h1{margin:0 0 6px;font-size:22px;font-weight:600}
header .meta{font-size:13px;opacity:.9}
header .banner{margin-top:12px;font-size:12.5px;background:rgba(255,255,255,.14);
padding:8px 12px;border-radius:8px;display:inline-block}
main{padding:22px 34px 48px;max-width:1240px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:14px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;
box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card .label{font-size:12.5px;color:var(--muted);margin-bottom:6px}
.card .value{font-size:23px;font-weight:650;letter-spacing:-.4px}
.card .hint{font-size:11.5px;color:var(--muted);margin-top:4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media (max-width:980px){.grid2{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;
margin-bottom:18px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
section h2{margin:0 0 14px;font-size:15.5px;font-weight:600}
section h3{margin:16px 0 8px;font-size:13.5px;font-weight:600;color:#374151}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:12.5px;
border-bottom:1px solid var(--line);padding:7px 8px;white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid #F1F4F9}
tr:hover td{background:#FAFBFE}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:600}
.badge.high{background:#FDECEF;color:#C0344C}
.badge.mid{background:#FFF6E5;color:#A56A00}
.badge.low{background:#EAF7F3;color:#12856B}
.quote{background:#FAFBFE;border-left:3px solid var(--brand);padding:8px 12px;border-radius:6px;
font-size:12.8px;color:#374151;margin:6px 0}
.muted{color:var(--muted);font-size:12.5px}
footer{color:var(--muted);font-size:12px;padding:0 34px 34px;max-width:1240px;margin:0 auto}
.kv{font-size:12.8px;color:#374151;line-height:1.8}
"""


def _badge(severity: str) -> str:
    cls = {"高": "high", "中": "mid", "低": "low"}.get(severity, "low")
    return f'<span class="badge {cls}">{severity}</span>'


def _kpi_card(label: str, value: str, hint: str = "") -> str:
    return f'<div class="card"><div class="label">{label}</div><div class="value">{value}</div><div class="hint">{hint}</div></div>'


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], raw_html: Optional[dict[int, Any]] = None) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            content = raw_html.get(index) if raw_html and index in raw_html else None
            if callable(content):
                cells.append(f"<td>{content(cell)}</td>")
            else:
                cells.append(f"<td>{cell}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_dashboard(evidence: dict[str, Any], provider_label: str = "") -> str:
    """把证据包渲染成单文件 HTML 看板。"""
    meta = evidence["meta"]
    overview = evidence["overview"]
    scorecard = evidence["scorecard"]
    anomalies = evidence["anomalies"]["dimension"]
    trend = evidence["trend"]
    feedback = evidence["feedback"]
    dispatch_view = evidence.get("dispatch") or {}

    def value(key: str) -> str:
        return format_metric(key, overview.get(key, {}).get("value"))

    cards = "".join(
        [
            _kpi_card("履约质量综合得分", f"{scorecard.get('total')} <span class='muted'>{scorecard.get('grade')} 级</span>",
                      f"时效 {scorecard['components'].get('timeliness', {}).get('score')} · "
                      f"质量 {scorecard['components'].get('quality', {}).get('score')} · "
                      f"体验 {scorecard['components'].get('experience', {}).get('score')}"),
            _kpi_card("上门准时率", value("arrive_ontime_rate"), "目标 ≥ 95%"),
            _kpi_card("全链路SLA达成率", value("sla_achieve_rate"), "目标 ≥ 95%"),
            _kpi_card("一次完工率", value("first_fix_rate"), "目标 ≥ 92%"),
            _kpi_card("客诉率", value("complaint_rate"), "目标 ≤ 2%"),
            _kpi_card("差评率", value("bad_review_rate"), "目标 ≤ 2%"),
            _kpi_card("服务NPS", value("nps"), f"已评价 {value('good_review_rate')} 好评"),
            _kpi_card("单均成本 / 毛利率", f"{value('avg_cost')} / {value('gross_margin')}", f"工单量 {overview['order_count']['value']:.0f}"),
        ]
    )

    labels = [row.get("period", "") for row in trend]
    quality_chart = line_chart(
        {
            "上门准时率": [row.get("arrive_ontime_rate") for row in trend],
            "SLA达成率": [row.get("sla_achieve_rate") for row in trend],
            "一次完工率": [row.get("first_fix_rate") for row in trend],
        },
        labels,
        percent=True,
        y_max=1.0,
    )
    risk_chart = line_chart(
        {
            "客诉率": [row.get("complaint_rate") for row in trend],
            "差评率": [row.get("bad_review_rate") for row in trend],
        },
        labels,
        percent=True,
    )

    site_rows = evidence["dimensions"].get("站点") or []
    category_rows = evidence["dimensions"].get("品类") or []
    site_chart = bar_chart([(row.get("label"), row.get("arrive_ontime_rate")) for row in site_rows], percent=True)
    category_chart = bar_chart([(row.get("label"), row.get("first_fix_rate")) for row in category_rows], percent=True, color="#22C3A6")

    anomaly_rows = [
        [
            _badge(item.get("severity", "")),
            item.get("dimension"),
            item.get("label") or item.get("value"),
            item.get("metric_name"),
            format_metric(item["metric"], item.get("baseline_value")),
            format_metric(item["metric"], item.get("current_value")),
            f"{item.get('deviation', 0) * 100:+.1f}%",
            item.get("recent_orders"),
        ]
        for item in anomalies[:14]
    ]

    attribution_cards = []
    for item in evidence["anomalies"].get("attributions", [])[:3]:
        rows = [
            [row.get("drill"), row.get("label"), row.get("orders"), f"{row.get('share', 0) * 100:.1f}%",
             row.get("metric_display"), f"{row.get('contribution', 0):.3f}"]
            for row in (item.get("rows") or [])[:4]
        ]
        attribution_cards.append(
            f"<h3>{item.get('dimension')}「{item.get('value')}」· {item.get('metric_name')}</h3>"
            f"<div class='quote'>{item.get('conclusion')}</div>"
            + _table(["下钻维度", "对象", "工单量", "占比", item.get("metric_name", "指标"), "贡献度"], rows)
        )

    theme_items = [(theme.get("theme"), theme.get("count")) for theme in (feedback.get("themes") or [])[:6]]
    feedback_donut = donut_chart([(label, float(value or 0)) for label, value in theme_items])
    feedback_rows = [
        [theme.get("theme"), theme.get("count"), f"{(theme.get('share') or 0) * 100:.1f}%"]
        for theme in (feedback.get("themes") or [])
    ]
    quotes = "".join(
        f"<div class='quote'>「{theme}」{texts[0]}</div>"
        for theme, texts in list((feedback.get("examples") or {}).items())[:4]
        if texts
    )

    action_rows = []
    from .ai.workflows import next_actions  # 局部导入，避免模块级循环依赖

    for action in next_actions(anomalies, datetime.fromisoformat(meta["anchor"])):
        action_rows.append([action["动作"], action["责任方"], action["度量指标"], action["完成时间"]])

    dispatch_rows = [
        [row.get("site_name"), row.get("site_type"), row.get("engineers"), row.get("orders_per_engineer_day"),
         row.get("score"), format_metric("arrive_ontime_rate", row.get("arrive_ontime_rate")),
         format_metric("complaint_rate", row.get("complaint_rate")), row.get("recommendation")]
        for row in (dispatch_view.get("sites") or [])
    ]

    rule = evidence["anomalies"]["rule"]
    rule_rows = [
        [name, payload.get("count"), f"{(payload.get('rate') or 0) * 100:.2f}%"]
        for name, payload in list((rule.get("by_type") or {}).items())[:8]
    ]

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>服务履约质量看板 · {meta['anchor'][:10]}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>服务履约质量监控看板</h1>
  <div class="meta">数据截至 {meta['anchor'][:10]} · 覆盖最近 {meta['window_days']} 天 · {meta['orders']} 单 ·
  站点 {meta['sites']} 个（直营 {meta['direct_sites']} 个）· 品类：{'、'.join(meta['categories'])}</div>
  <div class="banner">{provider_label}</div>
</header>
<main>
  <div class="cards">{cards}</div>

  <div class="grid2">
    <section><h2>履约质量周趋势</h2>{quality_chart}</section>
    <section><h2>体验风险趋势</h2>{risk_chart}
      <div class="muted" style="margin-top:8px">客诉率与差评率同步抬头时，通常意味着流程性问题而非个体问题。</div>
    </section>
  </div>

  <div class="grid2">
    <section><h2>站点上门准时率对比</h2>{site_chart}
      {_table(["站点", "工单量", "派单P90", "准时率", "一次完工率", "客诉率"],
              [[row.get("label"), row.get("orders"), format_metric("dispatch_p90", row.get("dispatch_p90")),
                format_metric("arrive_ontime_rate", row.get("arrive_ontime_rate")),
                format_metric("first_fix_rate", row.get("first_fix_rate")),
                format_metric("complaint_rate", row.get("complaint_rate"))] for row in site_rows])}
    </section>
    <section><h2>品类一次完工率对比</h2>{category_chart}
      {_table(["品类", "工单量", "一次完工率", "返工率", "客诉率", "毛利率"],
              [[row.get("label"), row.get("orders"), format_metric("first_fix_rate", row.get("first_fix_rate")),
                format_metric("rework_rate", row.get("rework_rate")), format_metric("complaint_rate", row.get("complaint_rate")),
                format_metric("gross_margin", row.get("gross_margin"))] for row in category_rows])}
    </section>
  </div>

  <section><h2>指标异常清单（近 14 天 vs 历史基线）</h2>
    {_table(["严重度", "维度", "对象", "指标", "基线", "当前", "偏离", "样本量"], anomaly_rows)}
    <div class="muted" style="margin-top:8px">检测方法：相对偏离 + 中位数/MAD 稳健 z-score，样本量与显著性双门槛过滤噪声。</div>
  </section>

  <section><h2>异常归因（下钻贡献度）</h2>{''.join(attribution_cards) or '<div class="muted">本期无显著异常</div>'}</section>

  <div class="grid2">
    <section><h2>用户声音主题分布</h2>
      <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">
        <div style="flex:0 0 210px">{feedback_donut}</div>
        <div style="flex:1;min-width:220px">
          {_table(["主题", "条数", "占负面比"], feedback_rows)}
        </div>
      </div>
      {quotes}
    </section>
    <section><h2>需跟进工单分类</h2>
      {_table(["异常类型", "命中工单", "占比"], rule_rows)}
      <div class="kv" style="margin-top:10px">
        需跟进工单（高+中严重度）：<b>{rule.get('follow_up_orders')}</b> 单，占比 <b>{(rule.get('follow_up_rate') or 0) * 100:.1f}%</b><br/>
        严重度分布：高 {rule['by_severity'].get('高')} · 中 {rule['by_severity'].get('中')} · 低 {rule['by_severity'].get('低')}
      </div>
    </section>
  </div>

  <section><h2>派单评分卡（智能派单建议）</h2>
    {_table(["站点", "类型", "工程师", "单/人/天", "综合得分", "上门准时率", "客诉率", "派单建议"], dispatch_rows)}
  </section>

  <section><h2>下周动作（异常 → 动作 → 责任方）</h2>
    {_table(["动作", "责任方", "度量指标", "完成时间"], action_rows) if action_rows else '<div class="muted">本期无需专项动作</div>'}
  </section>
</main>
<footer>
  数据来源：合成数据（固定随机种子，可完全复现），真实业务数据涉及用户隐私与商业机密，不进入公开仓库。<br/>
  生成时间 {meta['generated_at']} · 提示词版本 {meta.get('prompt_version', '—')} · 本页面由 fulfillment-copilot 自动生成，单文件内联样式与 SVG，无外部依赖。
</footer>
</body>
</html>
"""
