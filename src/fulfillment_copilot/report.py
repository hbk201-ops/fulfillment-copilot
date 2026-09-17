"""Markdown 报告渲染组件。

报告统一使用 Markdown：GitHub 上可直接阅读、可 diff、可复制进 PPT/邮件，
也方便非技术同学在飞书/钉钉里直接贴。所有表格与图表都从指标结果自动生成，
避免"手工抄数抄错"这类低级但高频的问题。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from .metrics import CATEGORY_LABELS, METRICS, format_metric

#: unicode 迷你趋势图，用于在表格/正文里一眼看趋势
_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[Optional[float]]) -> str:
    """把序列渲染成迷你趋势线（缺失值用空格占位）。"""
    data = [v for v in values if v is not None]
    if not data:
        return ""
    low, high = min(data), max(data)
    span = high - low
    out = []
    for value in values:
        if value is None:
            out.append(" ")
        elif span == 0:
            out.append(_SPARK_CHARS[len(_SPARK_CHARS) // 2])
        else:
            index = int(round((value - low) / span * (len(_SPARK_CHARS) - 1)))
            out.append(_SPARK_CHARS[index])
    return "".join(out)


def md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """生成 Markdown 表格。"""
    lines = ["| " + " | ".join(str(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)


def scorecard_block(scorecard: dict[str, Any]) -> str:
    """综合得分卡片（含分项）。"""
    total = scorecard.get("total")
    grade = scorecard.get("grade") or "—"
    parts = [f"**履约质量综合得分：{total if total is not None else '—'}（{grade} 级）**", ""]
    rows = []
    for key, payload in (scorecard.get("components") or {}).items():
        detail = payload.get("detail") or {}
        weak = [METRICS[k].name for k, v in detail.items() if v is not None and v < 90]
        rows.append([CATEGORY_LABELS.get(key, key), payload.get("score"), "、".join(weak) if weak else "—"])
    parts.append(md_table(["分项", "得分", "拖后腿的指标"], rows))
    return "\n".join(parts)


def overview_block(metrics: dict[str, Optional[float]], keys: Sequence[str], title: str = "核心指标") -> str:
    """核心指标总览表。"""
    rows = []
    for key in keys:
        spec = METRICS.get(key)
        rows.append([spec.name if spec else key, format_metric(key, metrics.get(key))])
    return f"**{title}**\n\n" + md_table(["指标", "当前值"], rows)


def dimension_table(
    rows: Sequence[dict[str, Any]],
    keys: Sequence[str],
    dimension_label: str = "维度",
    include_orders: bool = True,
    trend: Optional[dict[str, str]] = None,
) -> str:
    """分层对比表：维度 + 工单量 + 若干指标（可带迷你趋势）。"""
    headers = [dimension_label]
    if include_orders:
        headers.append("工单量")
    headers.extend(METRICS[k].name for k in keys)
    if trend:
        headers.append("趋势")

    body = []
    for row in rows:
        line: list[Any] = [row.get("label") or row.get("value")]
        if include_orders:
            line.append(row.get("orders"))
        line.extend(format_metric(k, row.get(k)) for k in keys)
        if trend:
            line.append(trend.get(str(row.get("value")), ""))
        body.append(line)
    return md_table(headers, body)


def trend_table(rows: Sequence[dict[str, Any]], keys: Sequence[str]) -> str:
    """周趋势表；``orders`` 已固定为独立列，不会重复出现在指标列里。"""
    metric_keys = [key for key in keys if key != "orders"]
    headers = ["周期", "区间", "工单量"] + [METRICS[k].name for k in metric_keys]
    body = [
        [row.get("period"), f"{row.get('start')} ~ {row.get('end')}", row.get("orders")]
        + [format_metric(k, row.get(k)) for k in metric_keys]
        for row in rows
    ]
    return md_table(headers, body)


def anomaly_table(anomalies: Sequence[dict[str, Any]], limit: int = 12) -> str:
    headers = ["严重度", "维度", "对象", "指标", "基线", "当前", "偏离", "样本量"]
    body = []
    for item in anomalies[:limit]:
        metric = item.get("metric")
        body.append(
            [
                item.get("severity"),
                item.get("dimension"),
                item.get("label") or item.get("value"),
                item.get("metric_name") or (METRICS[metric].name if metric in METRICS else metric),
                format_metric(metric, item.get("baseline_value")) if metric in METRICS else item.get("baseline_value"),
                format_metric(metric, item.get("current_value")) if metric in METRICS else item.get("current_value"),
                f"{item.get('deviation', 0) * 100:+.1f}%",
                item.get("recent_orders"),
            ]
        )
    return md_table(headers, body)


def rule_table(rule_result: dict[str, Any], limit: int = 8) -> str:
    by_type = list((rule_result.get("by_type") or {}).items())[:limit]
    body = [[name, payload.get("count"), f"{(payload.get('rate') or 0) * 100:.2f}%"] for name, payload in by_type]
    return md_table(["异常类型", "命中工单", "占比"], body)


def attribution_block(attribution: dict[str, Any], limit: int = 4) -> str:
    """归因块的 Markdown 片段：结论句 + 贡献度排序表。"""
    if not attribution:
        return "_（无归因结果）_"
    lines = [
        f"**归因结论**：{attribution['conclusion']}",
        "",
        md_table(
            ["下钻维度", "对象", "承接工单", "占比", attribution.get("metric_name", "指标"), "贡献度"],
            [
                [
                    row.get("drill"),
                    row.get("label"),
                    row.get("orders"),
                    f"{row.get('share', 0) * 100:.1f}%",
                    row.get("metric_display"),
                    f"{row.get('contribution', 0):.3f}",
                ]
                for row in (attribution.get("rows") or [])[:limit]
            ],
        ),
    ]
    return "\n".join(lines)


def feedback_block(feedback: dict[str, Any]) -> str:
    themes = feedback.get("themes") or []
    if not themes:
        return "_（本周无有效文本反馈）_"
    theme_rows = [[t.get("theme"), t.get("count"), f"{(t.get('share') or 0) * 100:.1f}%"] for t in themes]
    lines = [
        f"共 {feedback.get('with_text')} 条文本反馈，其中负面 {feedback.get('negative_count')} 条"
        f"（{((feedback.get('negative_rate') or 0) * 100):.1f}%）。",
        "",
        md_table(["主题", "条数", "占负面比"], theme_rows),
    ]
    examples = feedback.get("examples") or {}
    if examples:
        lines.append("")
        lines.append("**典型原声**")
        for theme, texts in list(examples.items())[:4]:
            if texts:
                lines.append(f"- 「{theme}」{texts[0]}")
    keywords = feedback.get("keywords") or []
    if keywords:
        lines.append("")
        lines.append("高频词：" + "、".join(f"{word}({count})" for word, count in keywords[:10]))
    return "\n".join(lines)
