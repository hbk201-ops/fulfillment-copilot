"""AI 工作流测试：证据包完整性、报告小节、提示词约束、模型失败回退。"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import support  # noqa: F401
from support import evidence

from fulfillment_copilot.ai import prompts
from fulfillment_copilot.ai.provider import LLMError, LLMProvider, get_provider
from fulfillment_copilot.ai.workflows import (
    ACTION_PLAYBOOK,
    BRD_TOPICS,
    anomaly_briefing,
    brd_draft,
    build_evidence,
    dispatch_advice,
    feedback_insight,
    next_actions,
    weekly_report,
)
from fulfillment_copilot.metrics import METRICS


class EvidencePackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ev = evidence(8000)

    def test_required_sections(self):
        self.assertEqual(
            set(self.ev),
            {"meta", "overview", "scorecard", "dimensions", "trend", "anomalies", "feedback", "dispatch", "sites", "ground_truth"},
        )

    def test_meta_fields(self):
        meta = self.ev["meta"]
        self.assertGreater(meta["orders"], 0)
        self.assertEqual(meta["sites"], 6)
        self.assertEqual(meta["direct_sites"], 2)
        self.assertEqual(len(meta["categories"]), 5)
        self.assertEqual(meta["prompt_version"], prompts.PROMPT_VERSION)

    def test_overview_covers_every_metric(self):
        self.assertEqual(set(self.ev["overview"]), set(METRICS))
        for key, item in self.ev["overview"].items():
            self.assertEqual(item["name"], METRICS[key].name)
            self.assertIn(item["direction"], ("up_better", "down_better"))
            self.assertTrue(item["display"])

    def test_dimensions_and_dispatch(self):
        self.assertEqual(set(self.ev["dimensions"]), {"站点", "品类", "城市", "站点类型"})
        # 直营站点不应出现在"站点类型"分组的第二行之外
        types = {row["value"] for row in self.ev["dimensions"]["站点类型"]}
        self.assertEqual(types, {"直营", "加盟"})
        self.assertEqual(len(self.ev["dispatch"]["sites"]), 6)
        self.assertGreaterEqual(self.ev["dispatch"]["engineer_count"], 10)
        self.assertGreaterEqual(len(self.ev["dispatch"]["rules"]), 3)

    def test_next_actions_map_to_playbook(self):
        actions = next_actions(self.ev["anomalies"]["dimension"], __import__("datetime").datetime.fromisoformat(self.ev["meta"]["anchor"]))
        self.assertGreater(len(actions), 0)
        for action in actions:
            self.assertIn(action["责任方"], {owner for _, owner, _ in ACTION_PLAYBOOK.values()})
            self.assertRegex(action["完成时间"], r"^\d{4}-\d{2}-\d{2}$")


class PromptTest(unittest.TestCase):
    def test_weekly_prompt_contains_hard_rules_and_sections(self):
        system, user = prompts.build_weekly_prompt(evidence(2000))
        self.assertIn("硬约束", system)
        self.assertIn("不得编造", system)
        for section in prompts.WEEKLY_SECTIONS:
            self.assertIn(section, user)
        self.assertIn("证据包", user)

    def test_digest_is_compact_and_valid_json(self):
        import json

        digest = prompts.evidence_digest(evidence(2000))
        payload = json.loads(digest)
        self.assertIn("overview", payload)
        self.assertLessEqual(len(payload["anomalies"]["dimension"]), 6)
        self.assertLessEqual(len(payload["trend"]), 8)

    def test_brd_prompt_includes_topic(self):
        _, user = prompts.build_brd_prompt(evidence(2000), "智能派单优化")
        self.assertIn("智能派单优化", user)
        for section in prompts.BRD_SECTIONS:
            self.assertIn(section, user)


class ProviderTest(unittest.TestCase):
    def test_offline_provider_is_not_available(self):
        provider = get_provider("offline")
        self.assertFalse(provider.available)

    def test_auto_without_env_is_offline(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("FQC_LLM_BASE_URL", "FQC_LLM_API_KEY", "FQC_LLM_MODEL"):
                os.environ.pop(key, None)
            self.assertFalse(get_provider("auto").available)
            with self.assertRaises(LLMError):
                get_provider("openai")

    def test_auto_with_env_uses_openai_compatible(self):
        env = {"FQC_LLM_BASE_URL": "https://api.example.com/v1", "FQC_LLM_API_KEY": "secret", "FQC_LLM_MODEL": "demo-model"}
        with mock.patch.dict(os.environ, env, clear=False):
            provider = get_provider("auto")
            self.assertTrue(provider.available)
            self.assertEqual(provider.model, "demo-model")
            self.assertIn("demo-model", provider.describe())


class ExplodingProvider(LLMProvider):
    """模拟"模型服务挂了"的场景。"""

    def __init__(self) -> None:
        super().__init__(name="exploding", model="boom-1", available=True)

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        raise LLMError("模拟模型服务不可用")


class StubProvider(LLMProvider):
    """记录提示词、返回固定文本，用于验证"模型通道确实被调用且提示词合规"。"""

    def __init__(self) -> None:
        super().__init__(name="stub", model="stub-1", available=True)
        self.system = ""
        self.user = ""

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.system, self.user = system, user
        return "### 一、本周结论\n\n模型生成的结论（用于测试）。"


class ReportSectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ev = evidence(8000)

    def test_weekly_report_sections(self):
        text = weekly_report(self.ev)
        for section in prompts.WEEKLY_SECTIONS:
            self.assertIn(section, text)
        self.assertIn("履约质量综合得分", text)
        self.assertIn("数据口径", text)

    def test_weekly_report_quotes_real_metric_values(self):
        text = weekly_report(self.ev)
        display = self.ev["overview"]["arrive_ontime_rate"]["display"]
        self.assertIn(display, text, "周报必须引用证据包中的真实指标值")

    def test_anomaly_briefing_sections(self):
        text = anomaly_briefing(self.ev)
        for section in prompts.ANOMALY_SECTIONS:
            self.assertIn(section, text)

    def test_feedback_insight_sections(self):
        text = feedback_insight(self.ev)
        for section in prompts.FEEDBACK_SECTIONS:
            self.assertIn(section, text)

    def test_dispatch_advice_sections(self):
        text = dispatch_advice(self.ev)
        for section in prompts.DISPATCH_SECTIONS:
            self.assertIn(section, text)
        self.assertIn("不使用黑盒模型", text)

    def test_brd_all_topics(self):
        for topic in BRD_TOPICS:
            text = brd_draft(self.ev, topic)
            for section in prompts.BRD_SECTIONS:
                self.assertIn(section, text, f"{topic} 缺少小节 {section}")
            self.assertIn("FR-1", text)

    def test_brd_generic_topic_fallback(self):
        text = brd_draft(self.ev, "不存在的主题")
        self.assertIn("## 一、背景与问题", text)


class ModelChannelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ev = evidence(2000)

    def test_model_output_is_used_when_available(self):
        stub = StubProvider()
        text = weekly_report(self.ev, stub)
        self.assertIn("模型生成的结论", text)
        self.assertTrue(stub.system and stub.user)
        self.assertIn("硬约束", stub.system)
        # 提示词里必须带上程序生成的初稿，降低幻觉、保证小节齐全
        self.assertIn("以下是程序按同一份证据包生成的初稿", stub.user)

    def test_failure_falls_back_to_template(self):
        text = weekly_report(self.ev, ExplodingProvider())
        self.assertIn("自动回退到确定性模板版本", text)
        for section in prompts.WEEKLY_SECTIONS:
            self.assertIn(section, text)

    def test_no_ai_flag_skips_model(self):
        stub = StubProvider()
        text = weekly_report(self.ev, stub, use_ai=False)
        self.assertNotIn("模型生成的结论", text)
        self.assertEqual(stub.user, "")


if __name__ == "__main__":
    unittest.main()
