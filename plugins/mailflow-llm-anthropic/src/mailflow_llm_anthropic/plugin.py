"""Anthropic Messages API backend (Claude).

Speaks the Anthropic chat format: the system prompt travels in the
``system`` field and the remaining messages keep their roles. API keys are
sent only through the ``x-api-key`` header; error text is sanitized so the
request URL or key never leaks into persisted notes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import httpx
from mailflow.config import LLMConfig
from mailflow.contracts import LLMCompletion, MessageDict
from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.llm.anthropic")

_DEFAULT_URL = "https://api.anthropic.com/v1/messages"
_VERSION_HEADER = "2023-06-01"
_MAX_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {408, 429, *range(500, 600)}


def _retryable(exc: Exception) -> bool:
    """Only transient failures deserve another attempt: timeouts, transport
    errors, 408/429 and 5xx. A 400/401 will fail identically forever."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    text = str(exc)
    if text.startswith("anthropic api error "):
        try:
            return int(text.split(":")[0].rsplit(" ", 1)[1]) in _RETRYABLE_STATUS
        except (IndexError, ValueError):
            return False
    return False


class AnthropicBackend:
    backend_id = "anthropic"

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._base_url = str(config.options.get("base_url", _DEFAULT_URL)).rstrip("/")
        self._max_tokens = int(config.options.get("max_tokens", 1024))
        self._thinking_budget = int(config.options.get("thinking_budget", 0) or 0)
        if self._thinking_budget:
            # Anthropic requires max_tokens > budget_tokens when thinking is on
            self._max_tokens = max(self._max_tokens, self._thinking_budget + 1)

    def _url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "content-type": "application/json",
            "anthropic-version": _VERSION_HEADER,
        }
        if self._config.api_key:
            headers["x-api-key"] = self._config.api_key
        for name, value in self._config.headers.items():
            headers[str(name).lower()] = str(value)
        return headers

    def _body(self, messages: list[MessageDict], temperature: float | None) -> dict[str, Any]:
        system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        rest = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
            if m.get("role") != "system"
        ]
        body: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._max_tokens,
            "messages": rest or [{"role": "user", "content": ""}],
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if self._thinking_budget:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget,
            }
        return body

    @staticmethod
    def _sanitize(exc: Exception) -> str:
        """Error text without URLs, query strings or header details."""
        text = str(exc)
        if "http" in text.lower():
            return "transport error"
        return text

    @staticmethod
    def _parse(payload: dict[str, Any]) -> LLMCompletion:
        content_parts: list[str] = []
        for block in payload.get("content") or []:  # pyright: ignore[reportUnknownVariableType]
            item = cast(dict[str, Any], block)
            if item.get("type") == "text":
                content_parts.append(str(item.get("text", "")))
        raw_model = payload.get("model", "")
        return LLMCompletion(
            text="".join(content_parts),
            model=str(raw_model),
            raw=payload,
        )

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        body = self._body(messages, temperature)
        if options:
            # only known Anthropic body fields pass through; the
            # openai-compatible option convention (body/headers/query/path)
            # must not leak unknown top-level keys into the Messages API
            allowed = (
                "system",
                "max_tokens",
                "metadata",
                "stop_sequences",
                "top_p",
                "top_k",
                "tools",
                "tool_choice",
            )
            body.update({k: v for k, v in options.items() if k in allowed and k not in body})
        headers = self._headers()
        url = self._url()
        max_retries = max(0, min(self._config.max_retries, 20))
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                    response = await client.post(url, json=body, headers=headers)
                    if response.status_code >= 400:
                        # status code only: the response body may echo the
                        # request (including the API key) and must never
                        # reach persisted processor notes
                        raise RuntimeError(f"anthropic api error {response.status_code}")
                    return self._parse(response.json())
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries or not _retryable(exc):
                    break
                await asyncio.sleep(min(2**attempt, _MAX_BACKOFF_SECONDS))
        raise RuntimeError(
            f"llm request failed: {self._sanitize(last_error or RuntimeError('unknown'))}"
        )


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-llm-anthropic",
    name="Anthropic LLM Backend",
    version="0.2.0",
    description="Anthropic Messages API transport (component id: anthropic-messages; legacy alias: anthropic)",
    kinds=[ComponentKind.LLM_BACKEND],
)


class LLMPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        # fine-grained id plus the historical alias so old configs keep working
        registrar.add_llm("anthropic-messages", AnthropicBackend)
        registrar.add_llm("anthropic", AnthropicBackend)


plugin = LLMPlugin()

__all__ = ["AnthropicBackend", "LLMPlugin", "plugin"]
