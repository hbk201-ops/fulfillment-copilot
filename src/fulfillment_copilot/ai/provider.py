"""大模型接入层。

设计原则：**AI 是增强项，不是单点依赖**
--------------------------------------
- 默认走 ``OfflineProvider``：不上云、不需要 API Key，报告由确定性模板从证据包生成，
  保证 demo、CI、面试现场都能一键复现；
- 配置了环境变量后自动切换到 ``OpenAICompatProvider``（兼容 DeepSeek / OpenAI / 通义等
  OpenAI 协议的服务），由大模型把同一份证据包写成更有洞察的叙述型报告；
- 两条路径共用同一份「证据包」（evidence pack，见 workflows.build_evidence），
  因此可以对比、可以回归测试，模型不可用时也不会让整条流水线失败。

环境变量
--------
``FQC_LLM_BASE_URL``  例如 https://api.deepseek.com/v1
``FQC_LLM_API_KEY``   API Key
``FQC_LLM_MODEL``     例如 deepseek-chat
``FQC_LLM_TIMEOUT``   可选，秒，默认 60
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、响应格式等）。"""


@dataclass
class LLMProvider:
    """模型提供方基类。"""

    name: str = "base"
    model: str = ""
    available: bool = False

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:  # pragma: no cover - 抽象
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name


class OfflineProvider(LLMProvider):
    """离线兜底：不调用任何模型，报告由模板引擎从证据包生成。"""

    def __init__(self) -> None:
        super().__init__(name="offline-template", model="", available=False)

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        raise LLMError("当前为离线模式：未配置大模型 API，报告由确定性模板生成")


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容的 Chat Completions 接口（DeepSeek / OpenAI / 通义 等）。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0) -> None:
        super().__init__(name="openai-compatible", model=model, available=True)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": False,
        }
        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:  # 鉴权/额度/参数错误
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise LLMError(f"模型接口返回 HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise LLMError(f"无法连接模型接口 {self.base_url}: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise LLMError(f"模型返回内容不是合法 JSON: {error}") from error

        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as error:
            raise LLMError(f"模型响应结构不符合预期: {str(body)[:300]}") from error


def get_provider(name: str = "auto", **overrides: object) -> LLMProvider:
    """构造模型提供方。

    :param name: ``auto`` / ``offline`` / ``openai``
    """
    base_url = str(overrides.get("base_url") or os.environ.get("FQC_LLM_BASE_URL") or "")
    api_key = str(overrides.get("api_key") or os.environ.get("FQC_LLM_API_KEY") or "")
    model = str(overrides.get("model") or os.environ.get("FQC_LLM_MODEL") or "")
    timeout = float(overrides.get("timeout") or os.environ.get("FQC_LLM_TIMEOUT") or 60.0)

    if name == "offline":
        return OfflineProvider()
    if name in ("openai", "auto") and base_url and api_key and model:
        return OpenAICompatProvider(base_url, api_key, model, timeout)
    if name == "openai":
        raise LLMError("选择了 openai 模式，但缺少 FQC_LLM_BASE_URL / FQC_LLM_API_KEY / FQC_LLM_MODEL")
    return OfflineProvider()


def provider_banner(provider: LLMProvider) -> str:
    """用于报告页脚：让读者一眼知道这份报告是人写的模板还是模型生成的。"""
    if provider.available:
        return f"报告由大模型生成（{provider.describe()}），证据包为程序计算的真实指标"
    return (
        "报告由**确定性模板**从证据包生成（离线模式，无需 API Key）；"
        "配置 FQC_LLM_BASE_URL / FQC_LLM_API_KEY / FQC_LLM_MODEL 后可切换为大模型叙述"
    )
