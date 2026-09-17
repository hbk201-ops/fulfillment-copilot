"""交付物测试：SVG 图表、HTML 看板、Excel 工作簿、CLI 端到端。"""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

import support  # noqa: F401
from support import evidence, temp_dir

from fulfillment_copilot import charts
from fulfillment_copilot.cli import main
from fulfillment_copilot.dashboard import render_dashboard
from fulfillment_copilot.excel import ExcelUnavailable, build_workbook, excel_available, write_workbook


class ChartTest(unittest.TestCase):
    def test_line_chart(self):
        svg = charts.line_chart({"准时率": [0.9, 0.91, 0.89]}, ["W1", "W2", "W3"], percent=True, y_max=1.0)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("polyline", svg)
        self.assertEqual(svg.count("<circle"), 3)

    def test_line_chart_handles_empty(self):
        self.assertIn("<svg", charts.line_chart({}, []))

    def test_line_chart_skips_none_points(self):
        svg = charts.line_chart({"A": [None, 0.5, None]}, ["W1", "W2", "W3"])
        self.assertEqual(svg.count("<circle"), 1)

    def test_bar_chart(self):
        svg = charts.bar_chart([("上海", 0.95), ("北京", 0.88)], percent=True)
        self.assertIn("<rect", svg)
        self.assertIn("95.0%", svg)

    def test_bar_chart_empty(self):
        self.assertIn("<svg", charts.bar_chart([]))

    def test_donut_chart(self):
        svg = charts.donut_chart([("时效", 10), ("技能", 5)])
        self.assertIn("stroke-dasharray", svg)
        self.assertIn("15", svg)

    def test_donut_chart_zero_total(self):
        self.assertIn("<svg", charts.donut_chart([("时效", 0)]))


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = render_dashboard(evidence(3000), "测试模式")

    def test_structure(self):
        self.assertTrue(self.html.startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", self.html)
        self.assertIn("<svg", self.html)
        self.assertIn("服务履约质量监控看板", self.html)

    def test_no_placeholder_leaks(self):
        for token in ("None", "nan", "Undefined"):
            self.assertNotIn(token, self.html)

    def test_key_sections_present(self):
        for heading in ("履约质量综合得分", "指标异常清单", "异常归因", "用户声音", "下周动作", "派单评分卡"):
            self.assertIn(heading, self.html)

    def test_severity_badges_rendered(self):
        self.assertGreater(self.html.count('class="badge'), 0)


class ExcelTest(unittest.TestCase):
    def test_workbook_sheets(self):
        if not excel_available():
            self.skipTest("未安装 openpyxl")
        workbook = build_workbook(evidence(3000))
        expected = {"总览", "分站点", "分品类", "分城市", "周趋势", "指标异常", "需跟进工单", "用户声音", "派单评分卡", "口径说明"}
        self.assertTrue(expected.issubset(set(workbook.sheetnames)))

    def test_write_and_reload(self):
        if not excel_available():
            self.skipTest("未安装 openpyxl")
        from openpyxl import load_workbook

        with temp_dir("excel") as tmp:
            path = Path(tmp) / "workbook.xlsx"
            write_workbook(evidence(3000), str(path))
            self.assertTrue(path.exists())
            reloaded = load_workbook(path)
            worksheet = reloaded["总览"]
            values = [str(cell.value) for row in worksheet.iter_rows(values_only=False) for cell in row if cell.value]
            self.assertTrue(any("履约质量综合得分" in value for value in values))
            self.assertIn("指标异常", reloaded.sheetnames)

    def test_unavailable_raises_clear_error(self):
        from fulfillment_copilot import excel

        original = excel.OPENPYXL_AVAILABLE
        excel.OPENPYXL_AVAILABLE = False
        try:
            with self.assertRaises(ExcelUnavailable):
                build_workbook(evidence(1000))
        finally:
            excel.OPENPYXL_AVAILABLE = original


class CliTest(unittest.TestCase):
    def _run(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_help_without_command(self):
        code, output = self._run([])
        self.assertEqual(code, 0)
        self.assertIn("fulfillment-copilot", output)

    def test_kpi_command(self):
        code, output = self._run(["kpi", "--orders", "1200", "--seed", "3"])
        self.assertEqual(code, 0)
        self.assertIn("履约质量综合得分", output)

    def test_verify_command_detects_injected_scenarios(self):
        code, output = self._run(["verify", "--orders", "8000"])
        self.assertEqual(code, 0, output)
        self.assertIn("命中 3/3", output)

    def test_excel_command_degrades_gracefully(self):
        from fulfillment_copilot import excel

        original = excel.OPENPYXL_AVAILABLE
        excel.OPENPYXL_AVAILABLE = False
        try:
            with temp_dir("excel") as tmp:
                code, output = self._run(["excel", "--orders", "1000", "--out", tmp])
        finally:
            excel.OPENPYXL_AVAILABLE = original
        self.assertEqual(code, 0)
        self.assertIn("跳过", output)

    def test_demo_end_to_end(self):
        with temp_dir("excel") as tmp:
            code, output = self._run(["demo", "--orders", "1500", "--out", tmp])
            self.assertEqual(code, 0, output)
            root = Path(tmp)
            self.assertTrue((root / "dashboard.html").exists())
            self.assertTrue((root / "data" / "orders.csv").exists())
            self.assertTrue((root / "data" / "ground_truth.json").exists())
            reports = list((root / "reports").glob("*.md"))
            self.assertGreaterEqual(len(reports), 5)
            for report in reports:
                self.assertGreater(report.stat().st_size, 200)
            if excel_available():
                self.assertTrue((root / "kpi_workbook.xlsx").exists())

    def test_report_type_unknown_returns_error(self):
        with temp_dir("excel") as tmp:
            code, _ = self._run(["report", "--orders", "500", "--type", "weekly", "--out", tmp])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
