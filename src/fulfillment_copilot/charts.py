"""纯 SVG 图表：零依赖、可嵌入 HTML、可离线打开。

不引入 matplotlib/plotly 的原因：HTML 看板要求"双击就能看、不联网、不装环境"，
而一次性生成的 SVG 比 canvas/JS 图表更稳、更容易被邮件和 IM 直接预览。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

PALETTE = ("#4D6BFE", "#22C3A6", "#F5A623", "#E4536B", "#8B5CF6", "#0EA5E9", "#64748B")


def _esc(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def line_chart(
    series: dict[str, Sequence[Optional[float]]],
    labels: Sequence[str],
    width: int = 720,
    height: int = 260,
    y_max: Optional[float] = None,
    percent: bool = False,
) -> str:
    """多折线趋势图（用于周趋势）。"""
    pad_left, pad_right, pad_top, pad_bottom = 52, 16, 18, 42
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    values = [v for seq in series.values() for v in seq if v is not None]
    if not values or not labels:
        return f'<svg viewBox="0 0 {width} {height}" width="100%"></svg>'
    top = y_max if y_max is not None else max(values) * 1.1
    bottom = 0.0
    span = max(top - bottom, 1e-9)

    def x_at(index: int) -> float:
        return pad_left + (plot_w * index / max(len(labels) - 1, 1))

    def y_at(value: float) -> float:
        return pad_top + plot_h - (value - bottom) / span * plot_h

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    # 网格与 Y 轴刻度
    for step in range(5):
        value = bottom + span * step / 4
        y = y_at(value)
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" stroke="#E6EAF2" stroke-width="1"/>')
        text = f"{value * 100:.0f}%" if percent else f"{value:.0f}"
        parts.append(f'<text x="{pad_left - 8}" y="{y + 4:.1f}" font-size="11" fill="#8A94A6" text-anchor="end">{text}</text>')
    # X 轴标签
    for index, label in enumerate(labels):
        parts.append(
            f'<text x="{x_at(index):.1f}" y="{height - pad_bottom + 18}" font-size="11" fill="#8A94A6" text-anchor="middle">{_esc(label)}</text>'
        )
    # 折线
    for order, (name, seq) in enumerate(series.items()):
        color = PALETTE[order % len(PALETTE)]
        points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(seq) if v is not None)
        if points:
            parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round"/>')
        for i, v in enumerate(seq):
            if v is not None:
                parts.append(f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="2.6" fill="{color}"/>')
    # 图例
    legend_x = pad_left
    for order, name in enumerate(series.keys()):
        color = PALETTE[order % len(PALETTE)]
        parts.append(f'<rect x="{legend_x}" y="{pad_top - 12}" width="18" height="4" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{legend_x + 24}" y="{pad_top - 8}" font-size="11" fill="#4A5568">{_esc(name)}</text>')
        legend_x += 24 + 12 * len(str(name)) + 44
    parts.append("</svg>")
    return "".join(parts)


def bar_chart(
    items: Sequence[tuple[str, Optional[float]]],
    width: int = 560,
    height: Optional[int] = None,
    percent: bool = False,
    color: str = PALETTE[0],
    reverse_color: bool = False,
) -> str:
    """横向条形图（用于站点/品类对比）。``reverse_color`` 时数值越大颜色越暖（表示越差）。"""
    rows = [(label, value if value is not None else 0.0) for label, value in items]
    if not rows:
        return f'<svg viewBox="0 0 {width} 40" width="100%"></svg>'
    height = height or max(60, 30 * len(rows) + 16)
    pad_left, pad_right, pad_top, pad_bottom = 116, 64, 10, 10
    plot_w = width - pad_left - pad_right
    top = max((value for _, value in rows), default=1.0) or 1.0
    row_h = (height - pad_top - pad_bottom) / len(rows)

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    for index, (label, value) in enumerate(rows):
        y = pad_top + index * row_h + row_h * 0.18
        bar_h = row_h * 0.64
        bar_w = max(1.0, plot_w * value / top)
        fill = color
        if reverse_color:
            ratio = value / top
            fill = "#63BE7B" if ratio < 0.4 else "#FFEB84" if ratio < 0.75 else "#F8696B"
        parts.append(f'<rect x="{pad_left}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="3" fill="{fill}" opacity="0.92"/>')
        parts.append(
            f'<text x="{pad_left - 8}" y="{y + bar_h * 0.72:.1f}" font-size="11.5" fill="#4A5568" text-anchor="end">{_esc(label)}</text>'
        )
        text = f"{value * 100:.1f}%" if percent else f"{value:.0f}"
        parts.append(
            f'<text x="{pad_left + bar_w + 8:.1f}" y="{y + bar_h * 0.72:.1f}" font-size="11.5" fill="#2D3748">{text}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def donut_chart(items: Sequence[tuple[str, float]], size: int = 220) -> str:
    """环形图（用于客诉主题占比）。"""
    rows = [(label, max(value, 0.0)) for label, value in items if value]
    total = sum(value for _, value in rows)
    if not rows or total <= 0:
        return f'<svg viewBox="0 0 {size} {size}" width="100%"></svg>'
    radius, stroke = size / 2 - 26, 26
    circumference = 2 * 3.141592653589793 * radius
    parts = [f'<svg viewBox="0 0 {size} {size}" width="100%" role="img">']
    offset = 0.0
    for index, (label, value) in enumerate(rows):
        fraction = value / total
        color = PALETTE[index % len(PALETTE)]
        dash = circumference * fraction
        parts.append(
            f'<circle cx="{size / 2}" cy="{size / 2}" r="{radius}" fill="none" stroke="{color}" stroke-width="{stroke}" '
            f'stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {size / 2} {size / 2})" stroke-linecap="butt"/>'
        )
        offset += dash
    parts.append(
        f'<text x="{size / 2}" y="{size / 2 - 2}" font-size="20" font-weight="600" fill="#2D3748" text-anchor="middle">{int(total)}</text>'
    )
    parts.append(f'<text x="{size / 2}" y="{size / 2 + 18}" font-size="11" fill="#8A94A6" text-anchor="middle">负面反馈</text>')
    parts.append("</svg>")
    return "".join(parts)
