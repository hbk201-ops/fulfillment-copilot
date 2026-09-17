"""合成数据测试：可复现性、业务不变式、异常剧本覆盖。"""

from __future__ import annotations

import unittest

import support  # noqa: F401
from support import dataset

from fulfillment_copilot.datagen import SITE_PROFILE, SCENARIOS, generate_dataset
from fulfillment_copilot.models import CATEGORIES


class DeterminismTest(unittest.TestCase):
    def test_same_seed_is_reproducible(self):
        """固定种子必须完全复现——这是"结论可追溯"的前提。"""
        first = generate_dataset(orders=500, days=30, seed=7)
        second = generate_dataset(orders=500, days=30, seed=7)
        self.assertEqual([o.order_id for o in first.orders], [o.order_id for o in second.orders])
        self.assertEqual([o.order_time for o in first.orders], [o.order_time for o in second.orders])
        self.assertEqual([o.review_score for o in first.orders], [o.review_score for o in second.orders])

    def test_different_seed_changes_data(self):
        first = generate_dataset(orders=500, days=30, seed=7)
        second = generate_dataset(orders=500, days=30, seed=8)
        self.assertNotEqual([o.order_time for o in first.orders], [o.order_time for o in second.orders])

    def test_ground_truth_matches_scenarios(self):
        ds = generate_dataset(orders=500, days=30, seed=7)
        self.assertEqual(len(ds.ground_truth), len(SCENARIOS))
        self.assertEqual({item["scenario_id"] for item in ds.ground_truth}, {s.scenario_id for s in SCENARIOS})
        for item in ds.ground_truth:
            self.assertEqual(len(item["window"]), 2)


class InvariantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = dataset(8000)

    def test_covers_all_categories_and_sites(self):
        self.assertEqual({o.category for o in self.ds.orders}, set(CATEGORIES))
        self.assertEqual({o.site_id for o in self.ds.orders}, {row[0] for row in SITE_PROFILE})

    def test_timeline_is_monotonic(self):
        for order in self.ds.orders:
            self.assertLessEqual(order.order_time, order.appoint_time)
            self.assertLessEqual(order.appoint_time, order.promised_time)
            self.assertLessEqual(order.order_time, order.dispatch_time)
            self.assertLessEqual(order.dispatch_time, order.arrive_time)
            if order.finish_time is not None:
                self.assertLessEqual(order.arrive_time, order.finish_time)

    def test_cancelled_orders_have_no_finish_time(self):
        for order in self.ds.orders:
            if order.is_cancelled:
                self.assertIsNone(order.finish_time)
                self.assertFalse(order.is_rework)

    def test_review_scores_in_range(self):
        for order in self.ds.orders:
            if order.review_score is not None:
                self.assertIn(order.review_score, (1, 2, 3, 4, 5))

    def test_money_fields_are_positive(self):
        for order in self.ds.orders:
            self.assertGreater(order.fee, 0)
            self.assertGreater(order.cost, 0)

    def test_negative_feedback_always_carries_a_theme_keyword(self):
        """回归测试：负面原声不能是"没有信息量的套话"，否则主题分析会全落到"其他"。"""
        from fulfillment_copilot.feedback import classify_theme

        vague = 0
        negative = 0
        for order in self.ds.orders:
            if not order.feedback:
                continue
            if order.review_score is not None and order.review_score <= 2 and not order.complaint_theme:
                negative += 1
                if classify_theme(order.feedback) == "其他":
                    vague += 1
        self.assertGreater(negative, 0)
        self.assertLess(vague / negative, 0.30, "负面文本中无主题的比例过高，说明语料与词典脱节")


if __name__ == "__main__":
    unittest.main()
