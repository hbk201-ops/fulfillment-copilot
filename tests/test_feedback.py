"""用户反馈文本分析测试。"""

from __future__ import annotations

import unittest

import support  # noqa: F401
from support import config, dataset, make_order

from fulfillment_copilot.feedback import analyze, classify_sentiment, classify_theme


class ClassifyThemeTest(unittest.TestCase):
    def test_keyword_mapping(self):
        cases = {
            "下单两天了还没人联系我，预约时间一拖再拖": "履约时效",
            "装完当天就出问题了，第二次上门才弄好，返工": "服务技能",
            "现场说要加收材料费，和页面上写的价格不一样": "费用争议",
            "师傅态度比较差，问两句就不耐烦": "服务态度",
            "派单后换了三个师傅，每个都说时间对不上要改约": "改约派单",
            "安装过程中把墙面磕掉了一块": "物品损坏",
        }
        for text, expected in cases.items():
            self.assertEqual(classify_theme(text), expected, text)

    def test_vague_text_falls_into_other(self):
        self.assertEqual(classify_theme("整体感觉一般般吧，说不上来哪里不对"), "其他")

    def test_empty_text(self):
        self.assertEqual(classify_theme(""), "")


class SentimentTest(unittest.TestCase):
    def test_score_takes_priority(self):
        self.assertEqual(classify_sentiment("还行吧", 5), "正")
        self.assertEqual(classify_sentiment("师傅很专业", 2), "负")

    def test_word_based_when_no_score(self):
        self.assertEqual(classify_sentiment("师傅很专业，价格透明", None), "正")
        self.assertEqual(classify_sentiment("等了一整天，太耽误事", None), "负")
        self.assertEqual(classify_sentiment("", None), "中")


class AnalyzeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.insight = analyze(dataset(8000).orders)

    def test_overview_numbers(self):
        self.assertGreater(self.insight.with_text, 0)
        self.assertLessEqual(self.insight.negative_count, self.insight.with_text)
        self.assertGreater(self.insight.negative_rate or 0, 0)
        self.assertLess(self.insight.negative_rate or 1, 1)

    def test_themes_are_sorted_and_meaningful(self):
        shares = [theme["share"] for theme in self.insight.themes]
        self.assertEqual(shares, sorted(shares, reverse=True))
        self.assertGreater(len(self.insight.themes), 2)

    def test_vague_theme_ratio_is_low(self):
        """回归断言：修完语料-词典脱节问题后，"其他"不应再占大头。"""
        other = next((theme for theme in self.insight.themes if theme["theme"] == "其他"), None)
        share = other["share"] if other else 0.0
        self.assertLess(share, 0.10)

    def test_examples_and_keywords(self):
        self.assertTrue(self.insight.examples)
        self.assertTrue(self.insight.keywords)
        for theme, texts in self.insight.examples.items():
            self.assertLessEqual(len(texts), 2)
            self.assertTrue(all(texts))

    def test_keywords_only_from_negative_texts(self):
        """回归断言：好评里的词（如"价格""讲解"）不得进入负面高频词。"""
        from support import make_order

        orders = [
            make_order("P1", review_score=5, feedback="预约很准时，价格也透明，服务体验不错。"),
            make_order("P2", review_score=5, feedback="安装速度快，讲解清楚，售后也主动回访了。"),
            make_order("N1", review_score=1, feedback="等了一整天没人上门，最后是我主动催了三次。"),
        ]
        insight = analyze(orders)
        words = {word for word, _ in insight.keywords}
        self.assertNotIn("价格", words)
        self.assertNotIn("讲解", words)
        self.assertIn("催", words)

    def test_highlights_are_generated(self):
        self.assertTrue(self.insight.highlights)
        self.assertIn("占比最高", self.insight.highlights[0])

    def test_empty_orders(self):
        empty = analyze([])
        self.assertEqual(empty.with_text, 0)
        self.assertIsNone(empty.negative_rate)
        self.assertEqual(empty.themes, [])

    def test_orders_without_feedback(self):
        order = make_order("NO-FEEDBACK-1", review_score=5)
        insight = analyze([order])
        self.assertEqual(insight.with_text, 0)


if __name__ == "__main__":
    unittest.main()
