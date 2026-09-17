"""指标口径测试：用手工构造的已知工单精确断言每一个指标。"""

from __future__ import annotations

import unittest
from datetime import timedelta

import support  # noqa: F401  （导入即完成 src 路径注入）
from support import BASE, config, make_order

from fulfillment_copilot.metrics import (
    METRICS,
    evaluate,
    format_metric,
    metric_value,
    overall_metrics,
    percentile,
    safe_rate,
    scorecard,
    slice_by,
    weekly_trend,
)


def fixture_orders():
    """四笔覆盖完整业务形态的工单（准时/超时返工/取消/未评价）。"""
    perfect = make_order(
        "O1",
        appoint_time=BASE + timedelta(hours=2),
        promised_time=BASE + timedelta(hours=24),
        dispatch_time=BASE + timedelta(hours=1),
        arrive_time=BASE + timedelta(hours=24, minutes=30),
        finish_time=BASE + timedelta(hours=26),
        review_score=5,
        fee=200,
        cost=120,
    )
    bad = make_order(
        "O2",
        order_time=BASE,
        appoint_time=BASE + timedelta(hours=3),
        promised_time=BASE + timedelta(hours=24),
        dispatch_time=BASE + timedelta(hours=5),
        arrive_time=BASE + timedelta(hours=26),
        finish_time=BASE + timedelta(hours=29),
        review_score=2,
        is_complaint=True,
        complaint_theme="履约时效",
        is_rework=True,
        fee=260,
        cost=200,
    )
    cancelled = make_order(
        "O3",
        order_time=BASE + timedelta(days=1),
        appoint_time=BASE + timedelta(days=1, hours=1),
        promised_time=BASE + timedelta(days=2),
        dispatch_time=BASE + timedelta(days=1, hours=2),
        is_cancelled=True,
    )
    no_review = make_order(
        "O4",
        order_time=BASE + timedelta(days=2),
        appoint_time=BASE + timedelta(days=2, hours=1),
        promised_time=BASE + timedelta(days=3),
        dispatch_time=BASE + timedelta(days=2, hours=2),
        arrive_time=BASE + timedelta(days=3, minutes=30),
        finish_time=BASE + timedelta(days=3, hours=1, minutes=30),
        fee=180,
        cost=110,
    )
    return [perfect, bad, cancelled, no_review]


class PercentileTest(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(percentile([], 0.5))

    def test_single_value(self):
        self.assertEqual(percentile([7.5], 0.9), 7.5)

    def test_linear_interpolation(self):
        self.assertAlmostEqual(percentile([1, 2, 3], 0.5), 2.0)
        self.assertAlmostEqual(percentile([25.5, 26, 29], 0.9), 28.4, places=3)

    def test_ignores_none(self):
        self.assertEqual(percentile([None, 4, None, 4], 0.5), 4.0)


class SafeRateTest(unittest.TestCase):
    def test_zero_denominator_is_none(self):
        """分母为 0 必须返回 None（样本不足），而不是 0（被误判成表现差）。"""
        self.assertIsNone(safe_rate(0, 0))

    def test_normal_rate(self):
        self.assertEqual(safe_rate(3, 4), 0.75)


class MetricValueTest(unittest.TestCase):
    def setUp(self):
        self.orders = fixture_orders()
        self.cfg = config()

    def test_counts_and_cancel_rate(self):
        self.assertEqual(metric_value(self.orders, "order_count", self.cfg), 4)
        self.assertEqual(metric_value(self.orders, "cancel_rate", self.cfg), 0.25)

    def test_dispatch_ontime_rate(self):
        # O2 派单 5h > 承诺 3h；其余 3 单达标 → 3/4
        self.assertEqual(metric_value(self.orders, "dispatch_ontime_rate", self.cfg), 0.75)

    def test_arrive_ontime_rate_uses_promised_slot(self):
        # 以「约定上门时段 + 到达窗口(1h)」判定：O1 晚 0.5h 准时，O2 晚 2h 迟到，O4 晚 0.5h 准时
        self.assertAlmostEqual(metric_value(self.orders, "arrive_ontime_rate", self.cfg), 2 / 3, places=4)

    def test_first_fix_and_rework(self):
        # 已完工 3 单（O1/O2/O4），其中 O2 返工
        self.assertAlmostEqual(metric_value(self.orders, "first_fix_rate", self.cfg), 2 / 3, places=4)
        self.assertAlmostEqual(metric_value(self.orders, "rework_rate", self.cfg), 1 / 3, places=4)

    def test_review_metrics_only_count_reviewed(self):
        # 只有 O1(5分)、O2(2分) 有评价
        self.assertEqual(metric_value(self.orders, "bad_review_rate", self.cfg), 0.5)
        self.assertEqual(metric_value(self.orders, "good_review_rate", self.cfg), 0.5)
        self.assertEqual(metric_value(self.orders, "avg_review_score", self.cfg), 3.5)
        self.assertEqual(metric_value(self.orders, "nps", self.cfg), 0.0)

    def test_complaint_rate(self):
        self.assertEqual(metric_value(self.orders, "complaint_rate", self.cfg), 0.25)

    def test_gross_margin_and_cost(self):
        self.assertAlmostEqual(metric_value(self.orders, "gross_margin", self.cfg), 210 / 640, places=4)
        self.assertAlmostEqual(metric_value(self.orders, "avg_cost", self.cfg), 430 / 4, places=2)

    def test_total_p90_skips_unfinished(self):
        # 已完工时长 26h / 29h / 25.5h → P90 = 28.4h（取消单不参与）
        self.assertAlmostEqual(metric_value(self.orders, "total_p90", self.cfg), 28.4, places=3)

    def test_unknown_metric_raises(self):
        """指标 key 写错必须报错，而不是静默返回 None。"""
        with self.assertRaises(KeyError):
            metric_value(self.orders, "not_a_metric", self.cfg)

    def test_format_metric(self):
        self.assertEqual(format_metric("arrive_ontime_rate", 0.9123), "91.2%")
        self.assertEqual(format_metric("dispatch_p90", 5.04), "5.0h")
        self.assertEqual(format_metric("order_count", 20000), "20000")
        self.assertEqual(format_metric("arrive_ontime_rate", None), "—")


class ScorecardTest(unittest.TestCase):
    def test_lower_is_better_metric_scoring(self):
        cfg = config()
        at_target = scorecard({"complaint_rate": 0.02, "bad_review_rate": 0.02, "good_review_rate": 0.90}, cfg)
        worse = scorecard({"complaint_rate": 0.04, "bad_review_rate": 0.02, "good_review_rate": 0.90}, cfg)
        self.assertEqual(at_target["components"]["experience"]["detail"]["complaint_rate"], 100.0)
        self.assertEqual(worse["components"]["experience"]["detail"]["complaint_rate"], 50.0)

    def test_higher_is_better_metric_scoring(self):
        cfg = config()
        card = scorecard({"first_fix_rate": 0.92, "rework_rate": 0.05}, cfg)
        self.assertEqual(card["components"]["quality"]["detail"]["first_fix_rate"], 100.0)
        self.assertEqual(card["components"]["quality"]["score"], 100.0)

    def test_grade_thresholds(self):
        cfg = config()
        top = scorecard({"arrive_ontime_rate": 1.0, "sla_achieve_rate": 1.0, "dispatch_ontime_rate": 1.0,
                         "first_fix_rate": 1.0, "rework_rate": 0.0, "complaint_rate": 0.0,
                         "bad_review_rate": 0.0, "good_review_rate": 1.0}, cfg)
        self.assertEqual(top["total"], 100.0)
        self.assertEqual(top["grade"], "A")


class SliceTest(unittest.TestCase):
    def test_slice_by_site(self):
        orders = fixture_orders()
        orders[2].site_id, orders[2].site_name = "S02", "北京直营服务站"
        orders[2].city = "北京"
        orders[3].site_id, orders[3].site_name = "S02", "北京直营服务站"
        orders[3].city = "北京"
        rows = slice_by(orders, "站点", config(), keys=["complaint_rate", "cancel_rate"])
        self.assertEqual(len(rows), 2)
        by_value = {row["value"]: row for row in rows}
        self.assertEqual(by_value["S01"]["orders"], 2)
        self.assertEqual(by_value["S02"]["orders"], 2)
        self.assertEqual(by_value["S02"]["label"], "北京直营服务站")
        self.assertEqual(by_value["S02"]["cancel_rate"], 0.5)

    def test_min_orders_filter(self):
        # 夹具 4 单同属一个品类，门槛设为 5 时应被过滤掉
        rows = slice_by(fixture_orders(), "品类", config(), keys=["complaint_rate"], min_orders=5)
        self.assertEqual(rows, [])

    def test_unknown_dimension_raises(self):
        with self.assertRaises(KeyError):
            slice_by(fixture_orders(), "不存在维度", config())


class OverallAndTrendTest(unittest.TestCase):
    def test_overall_metrics_covers_all_registry_keys(self):
        metrics = overall_metrics(fixture_orders(), config())
        self.assertEqual(set(metrics), set(METRICS))

    def test_weekly_trend_buckets_and_labels(self):
        rows = weekly_trend(fixture_orders(), config(), keys=["order_count"])
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            self.assertRegex(row["period"], r"^\d{4}-W\d{2}$")
            self.assertIn("start", row)
            self.assertEqual(row["orders"], row["order_count"])

    def test_evaluate_subset(self):
        result = evaluate(fixture_orders(), ["order_count", "cancel_rate"], config())
        self.assertEqual(set(result), {"order_count", "cancel_rate"})


if __name__ == "__main__":
    unittest.main()
