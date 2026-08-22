"""OpenAI-family LLM backends.

One plugin exposes five fine-grained component ids so configuration can
name the exact API shape instead of relying on ``options.path``:

- ``openai-completions`` — POST ``{base}/chat/completions``
- ``openai-responses`` — POST ``{base}/responses`` (stateless)
- ``openai-codex-responses`` — responses shape with Codex defaults
  (``store=false``, instructions-first system prompt)
- ``azure-openai-responses`` — Azure deployment URL +
  ``api-key`` authentication
- ``openai-compatible`` — legacy alias for ``openai-completions``

The request URL never appears in raised error text (query strings may
carry credentials); the core LLM router additionally redacts configured
API keys from any aggregated error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import httpx
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, MessageDict
from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.llm.openai")

_MAX_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {408, 429, *range(500, 600)}
_DEFAULT_API_VERSION = "preview"


def _retryable(exc: Exception) -> bool:
    """Only transient failures deserve another attempt: timeouts, transport
    errors, 408/429 and 5xx. A 400/401/404 will fail identically forever."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in _RETRYABLE_STATUS


class OpenAIBackend:
    """Shared transport: bounded retries on transient errors, sanitized
    error text, header/query merging from config and per-call options."""

    backend_id = "openai-compatible"
    default_path = "chat/completions"

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._path = str(config.options.get("path", self.default_path))

    # -- request construction ---------------------------------------------------

    def _url(self) -> str:
        base = self._config.base_url.rstrip("/")
        return f"{base}/{self._path.lstrip('/')}"

    def _headers(self, options: dict[str, Any] | None) -> dict[str, str]:
        merged: dict[str, str] = {"Content-Type": "application/json"}
        merged.update(self._config.headers)
        if self._config.api_key:
            merged.setdefault("Authorization", f"Bearer {self._config.api_key}")
        if options and isinstance(options.get("headers"), dict):
            merged.update({str(k): str(v) for k, v in options["headers"].items()})
        return merged

    def _query(self, options: dict[str, Any] | None) -> dict[str, str]:
        merged: dict[str, str] = dict(self._config.query)
        if options and isinstance(options.get("query"), dict):
            merged.update({str(k): str(v) for k, v in options["query"].items()})
        return merged

    def _body(
        self,
        messages: list[MessageDict],
        temperature: float | None,
        options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self._config.model, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        body.update(self._config.extra_body)
        if options:
            if isinstance(options.get("body"), dict):
                body.update(options["body"])
            if "model" in options:
                body["model"] = options["model"]
            if "temperature" in options:
                body["temperature"] = options["temperature"]
        return body

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        url = self._url()
        headers = self._headers(options)
        params = self._query(options)
        body = self._body(messages, temperature, options)
        max_retries = max(0, min(self._config.max_retries, 20))

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                    response = await client.post(url, headers=headers, params=params, json=body)
                    response.raise_for_status()
                return self._parse(response.json())
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries or not _retryable(exc):
                    break
                backoff = min(2**attempt, _MAX_BACKOFF_SECONDS)
                logger.debug(
                    "%s attempt %d/%d failed (%s); retrying in %.1fs",
                    self.backend_id,
                    attempt + 1,
                    max_retries + 1,
                    self._sanitize(exc),
                    backoff,
                )
                await asyncio.sleep(backoff)

        assert last_error is not None
        raise RuntimeError(f"llm request failed: {self._sanitize(last_error)}")

    # -- response handling --------------------------------------------------------

    @staticmethod
    def _sanitize(exc: Exception) -> str:
        """Error text without URLs, query strings or header details."""
        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            return f"HTTP {response.status_code}: {response.reason_phrase or 'request failed'}"
        if isinstance(exc, httpx.TimeoutException):
            return "request timed out"
        if isinstance(exc, httpx.RequestError):
            return f"transport error: {type(exc).__name__}"
        return str(exc)

    @staticmethod
    def _join_content_parts(content: Any) -> str:
        parts: list[str] = []
        for part in cast(list[Any], content):
            if isinstance(part, dict):
                part_dict = cast(dict[str, Any], part)
                text = part_dict.get("text")
                if text:
                    parts.append(str(text))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)

    def _parse(self, payload: dict[str, Any]) -> LLMCompletion:
        choices: Any = payload.get("choices") or []
        if not choices:
            raise RuntimeError("response contained no choices")
        message: Any = choices[0].get("message") or {}
        content: Any = message.get("content") or ""
        if isinstance(content, list):
            # some endpoints return content parts (e.g. [{"type": "text", "text": ...}])
            content = self._join_content_parts(content)
        raw_model: Any = payload.get("model") or ""
        return LLMCompletion(text=str(content), model=str(raw_model), raw=payload)


class ResponsesBackend(OpenAIBackend):
    """The stateless ``/responses`` API: system prompt travels as
    ``instructions``, conversation turns as ``input``."""

    backend_id = "openai-responses"
    default_path = "responses"
    _codex = False

    def _body(
        self,
        messages: list[MessageDict],
        temperature: float | None,
        options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        turns: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                continue
            turns.append({"role": str(m.get("role", "user")), "content": str(m.get("content", ""))})
        body: dict[str, Any] = {
            "model": self._config.model,
            "input": turns or [{"role": "user", "content": ""}],
        }
        if system:
            body["instructions"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if self._codex:
            # Codex endpoints are stateless by contract
            body.setdefault("store", False)
        body.update(self._config.extra_body)
        if options:
            if isinstance(options.get("body"), dict):
                body.update(options["body"])
            if "model" in options:
                body["model"] = options["model"]
            if "temperature" in options:
                body["temperature"] = options["temperature"]
        return body

    def _parse(self, payload: dict[str, Any]) -> LLMCompletion:
        text = payload.get("output_text")
        if not text:
            chunks: list[str] = []
            for item in cast(list[Any], payload.get("output") or []):
                item_map = cast(dict[str, Any], item)
                for part in cast(list[Any], item_map.get("content") or []):
                    part_map = cast(dict[str, Any], part)
                    if part_map.get("type") in ("output_text", "text") and part_map.get("text"):
                        chunks.append(str(part_map["text"]))
            text = "".join(chunks)
        if not text:
            raise RuntimeError("response contained no output text")
        raw_model: Any = payload.get("model") or ""
        return LLMCompletion(text=str(text), model=str(raw_model), raw=payload)


class CodexResponsesBackend(ResponsesBackend):
    """Codex-flavoured responses endpoint (``store=false`` defaults)."""

    backend_id = "openai-codex-responses"
    _codex = True


class AzureResponsesBackend(OpenAIBackend):
    """Azure OpenAI: deployment-scoped URLs and ``api-key`` auth."""

    backend_id = "azure-openai-responses"
    default_path = "responses"

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._api_version = str(config.options.get("api_version", _DEFAULT_API_VERSION))

    def _url(self) -> str:
        base = self._config.base_url.rstrip("/")
        return f"{base}/openai/deployments/{self._config.model}/{self._path.lstrip('/')}"

    def _headers(self, options: dict[str, Any] | None) -> dict[str, str]:
        merged: dict[str, str] = {"Content-Type": "application/json"}
        merged.update(self._config.headers)
        if self._config.api_key:
            merged.setdefault("api-key", self._config.api_key)
        if options and isinstance(options.get("headers"), dict):
            merged.update({str(k): str(v) for k, v in options["headers"].items()})
        return merged

    def _query(self, options: dict[str, Any] | None) -> dict[str, str]:
        merged = super()._query(options)
        merged.setdefault("api-version", self._api_version)
        return merged


_COMPONENTS: tuple[tuple[str, type[OpenAIBackend]], ...] = (
    ("openai-completions", OpenAIBackend),
    ("openai-responses", ResponsesBackend),
    ("openai-codex-responses", CodexResponsesBackend),
    ("azure-openai-responses", AzureResponsesBackend),
    ("openai-compatible", OpenAIBackend),  # legacy alias, kept for old configs
)

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-llm-openai-compatible",
    name="OpenAI-family LLM Backends",
    version="0.2.0",
    description=(
        "Chat Completions and Responses transports: openai-completions, "
        "openai-responses, openai-codex-responses, azure-openai-responses "
        "(legacy alias: openai-compatible)"
    ),
    kinds=[ComponentKind.LLM_BACKEND],
)

# historical name kept for imports and tests
OpenAICompatibleBackend = OpenAIBackend


class LLMPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        for component_id, backend in _COMPONENTS:
            registrar.add_llm(component_id, backend)


plugin = LLMPlugin()

__all__ = [
    "AzureResponsesBackend",
    "CodexResponsesBackend",
    "LLMPlugin",
    "OpenAIBackend",
    "OpenAICompatibleBackend",
    "ResponsesBackend",
    "plugin",
]
