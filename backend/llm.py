from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol


TEMPERATURE = 0
MAX_TOKENS = 100
# 工具调用要吐参数，比分类长；单独一个上限，不影响既有链路
MAX_TOOL_TOKENS = 500
TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 0
THINKING_MODE = "disabled"


class LLMTimeout(Exception):
    pass


class LLMApiError(Exception):
    pass


@dataclass(frozen=True)
class LLMResult:
    raw_text: str


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


class LLMClient(Protocol):
    model: str

    def complete(self, system_prompt: str, user_text: str) -> LLMResult: ...


class LiveLLM:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        from openai import OpenAI

        key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
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
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                extra_body={"thinking": {"type": THINKING_MODE}},
            )
        except APITimeoutError as exc:
            raise LLMTimeout from exc
        except APIError as exc:
            raise LLMApiError from exc
        content = response.choices[0].message.content
        return LLMResult(raw_text=content or "")

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
                temperature=TEMPERATURE,
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
        return ToolCallResult(tool_calls=calls, text=message.content or "")


class StubLLM:
    def __init__(
        self,
        raw_text: str = "",
        fault: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        text: str = "",
    ) -> None:
        self.model = "stub"
        self._raw_text = raw_text
        self._fault = fault
        self._tool_calls = tool_calls or []
        self._text = text

    def complete(self, system_prompt: str, user_text: str) -> LLMResult:
        if self._fault == "llm_timeout":
            raise LLMTimeout
        if self._fault == "llm_api_error":
            raise LLMApiError
        if self._fault == "llm_parse_error":
            return LLMResult(raw_text="not-json")
        return LLMResult(raw_text=self._raw_text)

    def complete_with_tools(
        self, system_prompt: str, user_text: str, tools: list[dict]
    ) -> ToolCallResult:
        if self._fault == "llm_timeout":
            raise LLMTimeout
        if self._fault == "llm_api_error":
            raise LLMApiError
        return ToolCallResult(tool_calls=list(self._tool_calls), text=self._text)
