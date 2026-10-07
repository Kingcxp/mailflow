"""Google Generative AI (Gemini API) backend.

Component id ``google-generative-ai``. Talks to the public Gemini API:

    POST {base_url}/v1beta/models/{model}:generateContent
    x-goog-api-key: <api key>

The default base URL is https://generativelanguage.googleapis.com; a
self-hosted proxy can be configured through ``base_url``. Error text is
sanitized (no URLs, no key material); the core router redacts the
configured key as well. Only transient failures (timeouts, transport
errors, 408/429/5xx) are retried.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

import httpx
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, MessageDict, ToolCall
from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.llm.google")

_DEFAULT_BASE = "https://generativelanguage.googleapis.com"
_MAX_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {408, 429, *range(500, 600)}


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in _RETRYABLE_STATUS


def _function_declarations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert chat-shaped tool schemas to Gemini ``functionDeclarations``.

    Chat: ``{"type": "function", "function": {"name", "description",
    "parameters"}}``; Gemini wants the declaration unwrapped (``parameters`` is
    an OpenAPI-subset schema, so the JSON schema travels as-is).
    """
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        function: Any = tool.get("function")
        if not isinstance(function, dict):
            continue
        fn = cast("dict[str, Any]", function)
        name = str(fn.get("name", ""))
        if not name:
            continue
        declaration: dict[str, Any] = {"name": name}
        if fn.get("description"):
            declaration["description"] = str(fn["description"])
        declaration["parameters"] = fn.get("parameters") or {"type": "object", "properties": {}}
        declarations.append(declaration)
    return declarations


def _tool_parts(message: MessageDict) -> list[dict[str, Any]]:
    """Parts for one non-system message: text, function calls, results.

    A ``role: "tool"`` turn becomes a ``functionResponse`` part of a user turn
    (this API has no dedicated tool role), and an assistant turn's ``tool_calls``
    become ``functionCall`` parts of a model turn.
    """
    parts: list[dict[str, Any]] = []
    role = str(message.get("role", "user"))
    text = str(message.get("content", ""))
    if role == "tool":
        function = str(message.get("name", "")) or str(message.get("tool_call_id", ""))
        return [
            {
                "functionResponse": {
                    "name": function,
                    "response": {"result": text},
                }
            }
        ]
    if text:
        parts.append({"text": text})
    for raw_call in cast("list[Any]", message.get("tool_calls") or []):
        call = cast("dict[str, Any]", raw_call)
        fn = cast("dict[str, Any]", call.get("function") or {})
        parts.append(
            {
                "functionCall": {
                    "name": str(fn.get("name", "")),
                    "args": _tool_arguments(fn.get("arguments")),
                }
            }
        )
    return parts


def _tool_arguments(raw: Any) -> dict[str, Any]:
    """``functionCall.args`` is an object; a JSON string is re-parsed."""
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            return {}
        return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
    return {}


class GeminiBackend:
    backend_id = "google-generative-ai"

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._max_output_tokens = int(config.options.get("max_tokens", 0) or 0)
        self._temperature_opt = (
            float(config.options["temperature"])
            if config.options.get("temperature") not in (None, "")
            else None
        )
        self._thinking_budget = int(config.options.get("thinking_budget", 0) or 0)
        self._base = (config.base_url or _DEFAULT_BASE).rstrip("/")

    def _url(self) -> str:
        model = self._config.model.strip("/")
        return f"{self._base}/v1beta/models/{model}:generateContent"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["x-goog-api-key"] = self._config.api_key
        headers.update({str(k): str(v) for k, v in self._config.headers.items()})
        return headers

    def _body(
        self,
        messages: list[MessageDict],
        temperature: float | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system: list[dict[str, Any]] = []
        for message in messages:
            text = str(message.get("content", ""))
            role = str(message.get("role", "user"))
            if role == "system":
                system.append({"text": text})
                continue
            parts = _tool_parts(message)
            if not parts:
                parts = [{"text": ""}]
            # Gemini alternates user/model roles
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
        body: dict[str, Any] = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
        if system:
            body["systemInstruction"] = {"parts": system}
        if tools:
            declarations = _function_declarations(tools)
            if declarations:
                body["tools"] = [{"functionDeclarations": declarations}]
        generation: dict[str, Any] = {}
        if temperature is not None:
            generation["temperature"] = temperature
        elif self._temperature_opt is not None:
            generation["temperature"] = self._temperature_opt
        if self._max_output_tokens:
            generation["maxOutputTokens"] = self._max_output_tokens
        if self._thinking_budget:
            generation["thinkingConfig"] = {"thinkingBudget": self._thinking_budget}
        extra = self._config.extra_body.get("generationConfig")
        if isinstance(extra, dict):
            generation.update(cast(dict[str, Any], extra))
        if generation:
            body["generationConfig"] = generation
        for key, value in self._config.extra_body.items():
            if key != "generationConfig":
                body[key] = value
        return body

    @staticmethod
    def _sanitize(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            detail = response.text[:200] if response.status_code >= 400 else ""
            return f"HTTP {response.status_code}: {detail or response.reason_phrase}"
        if isinstance(exc, httpx.TimeoutException):
            return "request timed out"
        if isinstance(exc, httpx.RequestError):
            return f"transport error: {type(exc).__name__}"
        return str(exc)

    def _parse(self, payload: dict[str, Any]) -> LLMCompletion:
        candidates: Any = payload.get("candidates") or []
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        parts: list[Any] = []
        if isinstance(candidates, list) and candidates:
            candidate = cast(dict[str, Any], candidates[0])
            content = cast(Any, candidate.get("content"))
            content_map = cast("dict[str, Any]", content) if isinstance(content, dict) else {}
            parts = cast(list[Any], content_map.get("parts") or [])
        for part in parts:
            part_map = cast(dict[str, Any], part)
            text = part_map.get("text")
            if text:
                chunks.append(str(text))
            call: Any = part_map.get("functionCall")
            if not isinstance(call, dict):
                continue
            call_map = cast("dict[str, Any]", call)
            name = str(call_map.get("name") or "")
            if not name:
                continue
            raw_args: Any = call_map.get("args")
            tool_calls.append(
                ToolCall(
                    # this protocol does not return a call id; the name plus the
                    # position is what the result turn can reference
                    call_id=f"{name}-{len(tool_calls)}",
                    name=name,
                    arguments=(
                        cast("dict[str, Any]", raw_args) if isinstance(raw_args, dict) else {}
                    ),
                )
            )
        if not chunks and not tool_calls:
            # name the reason: an empty answer is usually a safety block or a
            # token limit, and "no candidates text" alone hides both
            reason = ""
            if isinstance(candidates, list) and candidates:
                reason = str(cast(dict[str, Any], candidates[0]).get("finishReason") or "")
            raise RuntimeError(
                "endpoint returned a response with no content"
                + (f" (finish_reason={reason})" if reason else "")
            )
        usage = cast(dict[str, Any], payload.get("usageMetadata") or {})
        model_name: Any = usage.get("modelVersion") or self._config.model
        return LLMCompletion(
            text="".join(chunks),
            model=str(model_name),
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
        url = self._url()
        headers = self._headers()
        body = self._body(messages, temperature, tools)
        max_retries = max(0, min(self._config.max_retries, 20))

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                    response = await client.post(url, headers=headers, json=body)
                    response.raise_for_status()
                return self._parse(response.json())
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries or not _retryable(exc):
                    break
                backoff = min(2**attempt, _MAX_BACKOFF_SECONDS)
                logger.debug(
                    "gemini attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    self._sanitize(exc),
                    backoff,
                )
                await asyncio.sleep(backoff)

        assert last_error is not None
        raise RuntimeError(f"llm request failed: {self._sanitize(last_error)}")


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-llm-google-generative-ai",
    name="Google Generative AI Backend",
    version="0.1.0",
    description="Google Gemini API transport (component id: google-generative-ai)",
    kinds=[ComponentKind.LLM_BACKEND],
)


class LLMPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        registrar.add_llm("google-generative-ai", GeminiBackend)


plugin = LLMPlugin()

__all__ = ["GeminiBackend", "LLMPlugin", "plugin"]
