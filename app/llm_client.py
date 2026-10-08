"""OpenAI-compatible HTTP provider used for DeepSeek.

The implementation intentionally uses httpx directly.  The transport has
retries disabled so paid calls are only retried by the bounded policy here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import SupportFlowSettings, get_settings


class ProviderError(Exception):
    def __init__(self, classification: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.classification = classification
        self.retryable = retryable


@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    usage: dict[str, int | None] = field(default_factory=dict)


class LLMProvider(Protocol):
    provider_name: str
    model: str

    def complete_json(self, *, operation: str, system_prompt: str, user_prompt: str) -> LLMResponse:
        ...


class OpenAICompatibleProvider:
    provider_name = "openai_compatible"

    def __init__(self, settings: SupportFlowSettings | None = None):
        self.settings = settings or get_settings()
        self.model = self.settings.model
        timeout = httpx.Timeout(
            connect=self.settings.connect_timeout,
            read=self.settings.read_timeout,
            write=self.settings.write_timeout,
            pool=self.settings.pool_timeout,
        )
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(retries=0),
            timeout=timeout,
        )

    def complete_json(self, *, operation: str, system_prompt: str, user_prompt: str) -> LLMResponse:
        if not self.settings.api_key:
            raise ProviderError("CONFIGURATION", "SUPPORTFLOW_OPENAI_API_KEY is not configured")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        url = f"{self.settings.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

        last_error: ProviderError | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self.client.post(url, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                last_error = ProviderError("NETWORK_ERROR", str(exc), retryable=True)
                if attempt < self.settings.max_retries:
                    continue
                raise last_error from exc

            if response.status_code == 429:
                last_error = ProviderError("RATE_LIMITED", "provider returned HTTP 429", retryable=True)
            elif 500 <= response.status_code <= 599:
                last_error = ProviderError("UPSTREAM_ERROR", f"provider returned HTTP {response.status_code}", retryable=True)
            elif response.status_code == 401 or response.status_code == 403:
                raise ProviderError("AUTHENTICATION", f"provider returned HTTP {response.status_code}")
            elif 400 <= response.status_code <= 499:
                raise ProviderError("BAD_REQUEST", f"provider returned HTTP {response.status_code}")
            else:
                return self._parse_response(response)

            if attempt < self.settings.max_retries:
                continue
            raise last_error

        raise ProviderError("NETWORK_ERROR", "provider request failed", retryable=True)

    def _parse_response(self, response: httpx.Response) -> LLMResponse:
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("MALFORMED_RESPONSE", "provider response was not JSON") from exc

        try:
            choices = body["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("MALFORMED_RESPONSE", "provider response has no usable choices/content") from exc

        if not isinstance(content, str) or not content.strip():
            raise ProviderError("EMPTY_RESPONSE", "provider returned empty content")

        raw_usage = body.get("usage") or {}
        usage = {
            "input_tokens": self._first_int(raw_usage, "prompt_tokens", "input_tokens"),
            "output_tokens": self._first_int(raw_usage, "completion_tokens", "output_tokens"),
            "total_tokens": self._first_int(raw_usage, "total_tokens"),
        }
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model=self.model,
            usage=usage,
        )

    @staticmethod
    def _first_int(data: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, int):
                return value
        return None


class UnavailableProvider:
    provider_name = "openai_compatible"
    model = ""

    def complete_json(self, *, operation: str, system_prompt: str, user_prompt: str) -> LLMResponse:
        raise ProviderError("CONFIGURATION", "LLM provider is not configured")


def build_provider(settings: SupportFlowSettings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.provider != "openai_compatible":
        return UnavailableProvider()
    if not settings.api_key:
        return UnavailableProvider()
    return OpenAICompatibleProvider(settings)
