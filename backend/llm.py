from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


class LLMTimeout(Exception):
    pass


class LLMApiError(Exception):
    pass


@dataclass(frozen=True)
class LLMResult:
    raw_text: str


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
            timeout=30.0,
            max_retries=0,
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
                temperature=0,
                max_tokens=100,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except APITimeoutError as exc:
            raise LLMTimeout from exc
        except APIError as exc:
            raise LLMApiError from exc
        content = response.choices[0].message.content
        return LLMResult(raw_text=content or "")


class StubLLM:
    def __init__(self, raw_text: str = "", fault: str | None = None) -> None:
        self.model = "stub"
        self._raw_text = raw_text
        self._fault = fault

    def complete(self, system_prompt: str, user_text: str) -> LLMResult:
        if self._fault == "llm_timeout":
            raise LLMTimeout
        if self._fault == "llm_api_error":
            raise LLMApiError
        if self._fault == "llm_parse_error":
            return LLMResult(raw_text="not-json")
        return LLMResult(raw_text=self._raw_text)
