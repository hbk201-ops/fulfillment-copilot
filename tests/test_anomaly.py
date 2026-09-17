"""异常检测与归因测试。

最重要的一条：**注入的异常剧本必须被抓到**。这是全项目"分析是否真的有效"的底线验证，
也是 CI 里会拦住回归的那道门。
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import support  # noqa: F401
from support import BASE, config, dataset, make_order

from fulfillment_copilot.anomaly import (
    SUGGESTIONS,
    attribute,
    detect_all,
    detect_dimension_anomalies,
    rule_scan,
)


class InjectedScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = dataset(8000)
        cls.detected = detect_dimension_anomalies(cls.ds.orders, config(), anchor=cls.ds.anchor)

    def test_all_injected_scenarios_are_detected(self):
        found = {(item.dimension, item.value, item.metric) for item in self.detected}
        for scenario in self.ds.ground_truth:
            key = (scenario["dimension"], scenario["value"], scenario["metric"])
            self.assertIn(key, found, f"未识别注入异常：{scenario['scenario_id']} - {scenario['title']}")

    def test_detected_items_carry_evidence(self):
        for item in self.detected:
            self.assertIn(item.severity, ("高", "中", "低"))
            self.assertIsNotNone(item.baseline_value)
            self.assertIsNotNone(item.current_value)
            self.assertGreaterEqual(item.recent_orders, 20)
            self.assertTrue(item.suggestion)
            self.assertIn("基线", item.message)

    def test_deviation_direction_is_worsening(self):
        """只报"变差"的指标：越大越好的指标偏离必须为负，越小越好的必须为正。"""
        from fulfillment_copilot.metrics import METRICS

        for item in self.detected:
            spec = METRICS[item.metric]
            if spec.higher_is_better:
                self.assertLess(item.deviation, 0, f"{item.metric} 偏离方向应为负")
            else:
                self.assertGreater(item.deviation, 0, f"{item.metric} 偏离方向应为正")

    def test_every_suggestion_is_mapped(self):
        for item in self.detected:
            self.assertIn(item.metric, SUGGESTIONS)

    def test_empty_input_returns_empty(self):
        self.assertEqual(detect_dimension_anomalies([], config()), [])


class RuleScanTest(unittest.TestCase):
    def test_overdue_order_is_flagged_with_severity(self):
        # 家庭维修承诺：派单 3h、上门到达窗口 1h。这里派单 12h（4 倍）→ 高；
        # 上门晚 2.5h（2.5 倍窗口）→ 中；低分差评与客诉 → 中/高。
        order = make_order(
            "LATE-1",
            appoint_time=BASE + timedelta(hours=3),
            promised_time=BASE + timedelta(hours=20),
            dispatch_time=BASE + timedelta(hours=12),
            arrive_time=BASE + timedelta(hours=22, minutes=30),
            finish_time=BASE + timedelta(hours=24),
            review_score=2,
            is_complaint=True,
            complaint_theme="履约时效",
        )
        result = rule_scan([order], config())
        types = {item["anomaly_type"]: item for item in result["detail"]}
        self.assertIn("派单超时", types)
        self.assertEqual(types["派单超时"]["severity"], "高")
        self.assertIn("上门迟到", types)
        self.assertEqual(types["上门迟到"]["severity"], "中")
        self.assertIn("客诉", types)
        self.assertIn("低分差评", types)
        self.assertNotIn("全链路超时", types, "全链路 24h 未超 36h 承诺，不应误报")
        self.assertEqual(result["total_orders"], 1)
        self.assertEqual(result["follow_up_orders"], 1)
        self.assertEqual(set(result["by_severity"]), {"高", "中", "低"})

    def test_cancelled_order_flagged_as_cancel(self):
        order = make_order("CANCEL-1", is_cancelled=True)
        detail = rule_scan([order], config())["detail"]
        self.assertEqual([item["anomaly_type"] for item in detail], ["订单取消"])

    def test_healthy_order_is_not_flagged(self):
        order = make_order(
            "OK-1",
            appoint_time=BASE + timedelta(hours=1),
            promised_time=BASE + timedelta(hours=20),
            dispatch_time=BASE + timedelta(hours=1),
            arrive_time=BASE + timedelta(hours=20, minutes=20),
            finish_time=BASE + timedelta(hours=22),
            review_score=5,
            fee=200,
            cost=120,
        )
        result = rule_scan([order], config())
        self.assertEqual(result["flagged_orders"], 0)
        self.assertEqual(result["detail"], [])


class AttributionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = dataset(8000)
        cls.attribution = attribute(cls.ds.orders, config(), "站点", "S03", "dispatch_p90", anchor=cls.ds.anchor)

    def test_attribution_has_conclusion_and_rows(self):
        self.assertTrue(self.attribution["conclusion"])
        self.assertGreater(len(self.attribution["rows"]), 0)
        self.assertIn("S03", self.attribution["conclusion"] + self.attribution["value"])

    def test_rows_are_sorted_by_contribution(self):
        contributions = [row["contribution"] for row in self.attribution["rows"]]
        self.assertEqual(contributions, sorted(contributions, reverse=True))

    def test_degenerate_drill_dimension_is_skipped(self):
        """站点异常再按城市下钻只有一个分组，没有信息量，必须被跳过。"""
        drills = {row["drill"] for row in self.attribution["all_rows"]}
        self.assertNotIn("城市", drills)

    def test_share_sums_within_one(self):
        rows = self.attribution["all_rows"]
        for drill in {row["drill"] for row in rows}:
            total = sum(row["share"] for row in rows if row["drill"] == drill)
            self.assertAlmostEqual(total, 1.0, places=3)

    def test_unknown_dimension_raises(self):
        with self.assertRaises(KeyError):
            attribute(self.ds.orders, config(), "不存在的维度", "X", "dispatch_p90")


class DetectAllTest(unittest.TestCase):
    def test_detect_all_returns_expected_bundle(self):
        ds = dataset(4000)
        result = detect_all(ds.orders, config(), anchor=ds.anchor)
        self.assertEqual(set(result), {"anchor", "rule", "dimension", "attributions"})
        self.assertGreater(len(result["dimension"]), 0)
        for item in result["attributions"]:
            self.assertIn("conclusion", item)
            self.assertIn("severity", item)


if __name__ == "__main__":
    unittest.main()
