"""用户评价与客诉文本分析（零依赖、可解释）。

把非结构化的评价/客诉文本转成可统计的主题与情感，回答三个运营问题：
1. 用户在抱怨什么（主题分布）？
2. 负面声音集中在哪些站点/品类（交叉定位）？
3. 有哪些原话可以作为改善依据（证据留存）？

实现方式是**领域词典 + 规则打分**：完全可解释、可审计、可离线跑，
不需要调用大模型（这也是把 AI 用在"该用的地方"而不是"什么都丢给模型"）。
更细的语义归纳交给 ``ai.workflows.summarize_feedback``（可接真实大模型）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .models import Order

#: 主题词典：命中任一关键词即记为该主题（与 models.COMPLAINT_THEMES 对齐）
THEME_LEXICON: dict[str, tuple[str, ...]] = {
    "履约时效": ("没人联系", "等了一天", "迟到", "晚了", "超时", "拖", "催", "不来", "等待时间", "慢", "耽误"),
    "服务技能": ("返工", "没解决", "不专业", "水平", "报故障", "装好就", "白跑", "二次上门", "没弄好", "还是老样子"),
    "费用争议": ("加收", "加价", "费用", "收费", "价格", "报价", "贵", "明细", "材料费", "搬运费"),
    "服务态度": ("态度", "不耐烦", "爱答不理", "沟通", "没解释", "讲解"),
    "改约派单": ("改约", "换师傅", "改到", "时间对不上", "重新安排", "派单", "临时"),
    "物品损坏": ("磕", "损坏", "划痕", "碰坏", "理赔", "墙面"),
}

NEGATIVE_WORDS: tuple[str, ...] = (
    "差", "慢", "不", "没", "无", "问题", "故障", "贵", "坑", "失望", "投诉", "敷衍", "耽误", "白跑",
)
POSITIVE_WORDS: tuple[str, ...] = (
    "专业", "准时", "不错", "满意", "快", "清楚", "透明", "主动", "省心", "性价比", "卖力", "干净", "耐心", "给力",
)


@dataclass
class FeedbackInsight:
    """评价文本分析结果。"""

    total_orders: int
    with_text: int
    negative_count: int
    negative_rate: Optional[float]
    themes: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[tuple[str, int]] = field(default_factory=list)
    examples: dict[str, list[str]] = field(default_factory=dict)
    highlights: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_orders": self.total_orders,
            "with_text": self.with_text,
            "negative_count": self.negative_count,
            "negative_rate": self.negative_rate,
            "themes": self.themes,
            "keywords": self.keywords,
            "examples": self.examples,
            "highlights": self.highlights,
        }


def classify_theme(text: str) -> str:
    """按词典判定文本主题；多主题命中时取命中次数最多的那个。"""
    if not text:
        return ""
    scores = {theme: sum(1 for word in words if word in text) for theme, words in THEME_LEXICON.items()}
    best_theme, best_score = max(scores.items(), key=lambda kv: kv[1])
    return best_theme if best_score > 0 else "其他"


def classify_sentiment(text: str, review_score: Optional[int] = None) -> str:
    """情感判定：评分优先，其次看正负词典。"""
    if review_score is not None:
        if review_score >= 4:
            return "正"
        if review_score <= 2:
            return "负"
    if not text:
        return "中"
    negative = sum(1 for word in NEGATIVE_WORDS if word in text)
    positive = sum(1 for word in POSITIVE_WORDS if word in text)
    if negative > positive:
        return "负"
    if positive > negative:
        return "正"
    return "中"


def analyze(orders: Sequence[Order], example_per_theme: int = 2, top_keywords: int = 12) -> FeedbackInsight:
    """汇总分析：主题分布 + 情感占比 + 高频词 + 典型案例。"""
    texts = [(o, o.feedback.strip()) for o in orders if o.feedback and o.feedback.strip()]
    total = len(orders)
    if not texts:
        return FeedbackInsight(total_orders=total, with_text=0, negative_count=0, negative_rate=None)

    theme_counter: Counter[str] = Counter()
    negative = 0
    examples: dict[str, list[str]] = {}
    keyword_counter: Counter[str] = Counter()

    for order, text in texts:
        theme = order.complaint_theme or classify_theme(text)
        sentiment = classify_sentiment(text, order.review_score)
        if sentiment == "负":
            negative += 1
            if theme:
                theme_counter[theme] += 1
                bucket = examples.setdefault(theme, [])
                if len(bucket) < example_per_theme:
                    bucket.append(text)
            # 高频词只在负面文本里统计：好评里也会出现"价格""讲解"等词，
            # 混在一起统计会把好评关键词误报成客诉热点。
            for words in THEME_LEXICON.values():
                for word in words:
                    if word in text:
                        keyword_counter[word] += 1

    themes = [
        {
            "theme": theme,
            "count": count,
            "share": round(count / max(negative, 1), 4),
        }
        for theme, count in theme_counter.most_common()
    ]

    highlights: list[str] = []
    if themes:
        top = themes[0]
        highlights.append(f"负面反馈中「{top['theme']}」占比最高（{top['count']} 条，占 {top['share']:.0%}）")
    if examples:
        head_theme = next(iter(examples))
        highlights.append(f"典型原声（{head_theme}）：{examples[head_theme][0]}")

    return FeedbackInsight(
        total_orders=total,
        with_text=len(texts),
        negative_count=negative,
        negative_rate=round(negative / len(texts), 4),
        themes=themes,
        keywords=keyword_counter.most_common(top_keywords),
        examples=examples,
        highlights=highlights,
    )
