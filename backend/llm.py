from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol


TEMPERATURE = 0
MAX_TOKENS = 100
# 工具调用要吐参数，比分类长；单独一个上限，不影响既有链路
MAX_TOOL_TOKENS = 500
# 训练计划是结构化长输出，比工具参数还长；同样单独一个上限
MAX_PLAN_TOKENS = 600
TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 0
THINKING_MODE = "disabled"


class LLMTimeout(Exception):
    pass


class LLMApiError(Exception):
    pass


@dataclass(frozen=True)
class Usage:
    """一次模型调用的 token 消耗。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def as_payload(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def usage_payload(usage: "Usage | None") -> dict[str, int] | None:
    """成本是可观测数据，走 trace，不进业务返回结构——否则响应契约断言会被撑破。"""
    return usage.as_payload() if usage is not None else None


def read_usage(response: Any) -> Usage | None:
    raw = getattr(response, "usage", None)
    if raw is None:
        return None
    return Usage(
        prompt_tokens=getattr(raw, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(raw, "completion_tokens", 0) or 0,
        total_tokens=getattr(raw, "total_tokens", 0) or 0,
    )


@dataclass(frozen=True)
class LLMResult:
    raw_text: str
    usage: Usage | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict
    raw_arguments: str = ""


@dataclass(frozen=True)
class ToolCallResult:
    """工具调用轮的返回：要么模型要调工具，要么直接给了文本。"""

    tool_calls: list[ToolCall]
    text: str = ""
    usage: Usage | None = None


class LLMClient(Protocol):
    model: str

    def complete(self, system_prompt: str, user_text: str) -> LLMResult: ...


class LiveLLM:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        from openai import OpenAI

        key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        # 默认沿用全局 0；只有稳定性专项需要故意升温制造波动，用来验证波动检测本身
        self.temperature = TEMPERATURE if temperature is None else temperature
        # 输出长度上限按链路给：分类要 100，计划生成要 600，不能共用一个常量
        self.max_tokens = max_tokens or MAX_TOKENS
        self._client = OpenAI(
            api_key=key,
            base_url="https://api.deepseek.com",
            timeout=TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )

    def complete(self, system_prompt: str, user_text: str) -> LLMResult:
        from openai import APIError, APITimeoutError

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                response_format={"type": "json_object"},
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                extra_body={"thinking": {"type": THINKING_MODE}},
            )
        except APITimeoutError as exc:
            raise LLMTimeout from exc
        except APIError as exc:
            raise LLMApiError from exc
        content = response.choices[0].message.content
        return LLMResult(raw_text=content or "", usage=read_usage(response))

    def complete_with_tools(
        self, system_prompt: str, user_text: str, tools: list[dict]
    ) -> ToolCallResult:
        from openai import APIError, APITimeoutError

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                tools=tools,
                temperature=self.temperature,
                max_tokens=MAX_TOOL_TOKENS,
                extra_body={"thinking": {"type": THINKING_MODE}},
            )
        except APITimeoutError as exc:
            raise LLMTimeout from exc
        except APIError as exc:
            raise LLMApiError from exc

        message = response.choices[0].message
        calls = []
        for call in getattr(message, "tool_calls", None) or []:
            raw = call.function.arguments or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                # 参数不是合法 JSON：保留原文交给上层判失败，不在这里吞掉
                parsed = None
            calls.append(
                ToolCall(
                    name=call.function.name,
                    arguments=parsed if isinstance(parsed, dict) else {},
                    raw_arguments=raw,
                )
            )
        return ToolCallResult(
            tool_calls=calls, text=message.content or "", usage=read_usage(response)
        )


class StubLLM:
    def __init__(
        self,
        raw_text: str = "",
        fault: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        text: str = "",
        usage: Usage | None = None,
        raw_texts: list[str] | None = None,
    ) -> None:
        self.model = "stub"
        self._raw_text = raw_text
        self._fault = fault
        self._tool_calls = tool_calls or []
        self._text = text
        # 默认不带 usage：stub 不花 token，成本口径只在 live 下成立
        self._usage = usage
        # 多节点链路（解析 → 生成）需要按序应答；用完最后一档就停在那一档
        self._raw_texts = list(raw_texts or [])
        self._calls = 0

    def complete(self, system_prompt: str, user_text: str) -> LLMResult:
        if self._fault == "llm_timeout":
            raise LLMTimeout
        if self._fault == "llm_api_error":
            raise LLMApiError
        if self._fault == "llm_parse_error":
            return LLMResult(raw_text="not-json", usage=self._usage)
        if self._raw_texts:
            index = min(self._calls, len(self._raw_texts) - 1)
            self._calls += 1
            return LLMResult(raw_text=self._raw_texts[index], usage=self._usage)
        return LLMResult(raw_text=self._raw_text, usage=self._usage)

    def complete_with_tools(
        self, system_prompt: str, user_text: str, tools: list[dict]
    ) -> ToolCallResult:
        if self._fault == "llm_timeout":
            raise LLMTimeout
        if self._fault == "llm_api_error":
            raise LLMApiError
        return ToolCallResult(
            tool_calls=list(self._tool_calls), text=self._text, usage=self._usage
        )
