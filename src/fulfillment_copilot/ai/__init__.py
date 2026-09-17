"""AI 应用层：模型接入、提示词模板与报告工作流。"""

from .provider import LLMError, LLMProvider, OpenAICompatProvider, OfflineProvider, get_provider, provider_banner
from .prompts import PROMPT_VERSION
from .workflows import (
    BRD_TOPICS,
    build_evidence,
    anomaly_briefing,
    brd_draft,
    dispatch_advice,
    feedback_insight,
    next_actions,
    weekly_report,
)

__all__ = [
    "LLMError",
    "LLMProvider",
    "OpenAICompatProvider",
    "OfflineProvider",
    "get_provider",
    "provider_banner",
    "PROMPT_VERSION",
    "BRD_TOPICS",
    "build_evidence",
    "weekly_report",
    "anomaly_briefing",
    "feedback_insight",
    "brd_draft",
    "dispatch_advice",
    "next_actions",
]
