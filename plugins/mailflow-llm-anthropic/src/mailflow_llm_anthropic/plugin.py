"""Anthropic Messages API backend (Claude).

Speaks the Anthropic chat format: the system prompt travels in the
``system`` field and the remaining messages keep their roles. API keys are
sent only through the ``x-api-key`` header; error text is sanitized so the
request URL or key never leaks into persisted notes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

import httpx
from mailflow.config import LLMConfig
from mailflow.contracts import LLMCompletion, MessageDict, ToolCall
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


def _parse_tool_input(raw: Any) -> dict[str, Any]:
    """Tool arguments as they travel back to this API: always a JSON object.

    ``tool_use.input`` is an object, not a string, so an assistant turn built
    from a streamed chat call (whose ``arguments`` is a JSON string) has to be
    re-parsed before it can be replayed to Anthropic.
    """
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            return {}
        return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
    return {}


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

    def _body(
        self,
        messages: list[MessageDict],
        temperature: float | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        rest: list[dict[str, Any]] = []
        for m in messages:
            role = str(m.get("role", "user"))
            if role == "system":
                continue
            if role == "tool":
                # a tool result is a content block of a user turn in this API
                rest.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": str(m.get("tool_call_id", "")),
                                "content": str(m.get("content", "")),
                            }
                        ],
                    }
                )
                continue
            text = str(m.get("content", ""))
            raw_calls: Any = m.get("tool_calls")
            if not raw_calls:
                # a plain turn keeps the flat string form this API also accepts
                rest.append({"role": role, "content": text})
                continue
            blocks: list[dict[str, Any]] = []
            if text:
                blocks.append({"type": "text", "text": text})
            for raw_call in cast("list[Any]", raw_calls):
                call = cast("dict[str, Any]", raw_call)
                function = cast("dict[str, Any]", call.get("function") or {})
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "input": _parse_tool_input(function.get("arguments")),
                    }
                )
            rest.append({"role": role, "content": blocks})
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
        if tools:
            body["tools"] = tools
            body.setdefault("tool_choice", {"type": "auto"})
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
        tool_calls: list[ToolCall] = []
        raw_blocks: Any = payload.get("content") or []
        for block in cast("list[Any]", raw_blocks):
            item = cast(dict[str, Any], block)
            if item.get("type") == "text":
                content_parts.append(str(item.get("text", "")))
            elif item.get("type") == "tool_use":
                name = str(item.get("name") or "")
                if not name:
                    continue
                raw_input: Any = item.get("input")
                tool_calls.append(
                    ToolCall(
                        call_id=str(item.get("id") or f"call-{len(tool_calls)}"),
                        name=name,
                        arguments=(
                            cast("dict[str, Any]", raw_input) if isinstance(raw_input, dict) else {}
                        ),
                    )
                )
        raw_model = payload.get("model", "")
        text = "".join(content_parts)
        if not text.strip() and not tool_calls:
            # an empty answer is an error, never a silent empty completion:
            # the caller would fail much later with an unrelated parse message
            stop = str(payload.get("stop_reason") or "unknown")
            raise RuntimeError(f"endpoint returned a response with no content (stop_reason={stop})")
        return LLMCompletion(
            text=text,
            model=str(raw_model),
            raw=payload,
            tool_calls=tool_calls,
        )

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMCompletion:
        body = self._body(messages, temperature, tools)
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
