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
import contextlib
import json
import logging
from typing import Any, cast

import httpx
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, MessageDict
from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.llm.openai")

_MAX_BACKOFF_SECONDS = 45.0
_RETRYABLE_STATUS = {408, 429, *range(500, 600)}
_DEFAULT_API_VERSION = "preview"


def as_bool(value: Any) -> bool:
    """Interpret a config/option value as a bool (``"false"`` is False)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"", "0", "false", "no", "off"}


def _join_content_parts(content: Any) -> str:
    """Flatten OpenAI's content-parts array (``[{"type": "text", ...}]``)."""
    parts: list[str] = []
    for part in cast("list[Any]", content):
        if isinstance(part, dict):
            part_dict = cast("dict[str, Any]", part)
            text = part_dict.get("text")
            if text:
                parts.append(str(text))
        elif isinstance(part, str):
            parts.append(part)
    return "".join(parts)


def decode_sse_completion(text: str) -> tuple[str, str, str]:
    """Fold a server-sent-events completion body into one answer.

    Returns ``(content, model, finish_reason)``. Endpoints that answer
    ``text/event-stream`` regardless of the request's ``stream`` flag (some
    OpenAI-compatible proxies do exactly that) are unreadable as JSON, so the
    frames are decoded here instead of failing the call.

    Both shapes appear in the wild: OpenAI-style ``delta.content`` and the
    full-message form some proxies emit, plus ``reasoning_content`` (a
    thinking model's scratchpad) which must never become the answer.
    """
    parts: list[str] = []
    model = ""
    finish = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            frame: Any = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        frame_dict = cast("dict[str, Any]", frame)
        model = str(frame_dict.get("model") or model)
        choices: Any = frame_dict.get("choices")
        if not isinstance(choices, list):
            continue
        for raw_choice in cast("list[Any]", choices):
            if not isinstance(raw_choice, dict):
                continue
            choice = cast("dict[str, Any]", raw_choice)
            if choice.get("finish_reason"):
                finish = str(choice["finish_reason"])
            for key in ("delta", "message"):
                block: Any = choice.get(key)
                if not isinstance(block, dict):
                    continue
                content: Any = cast("dict[str, Any]", block).get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts.append(_join_content_parts(content))
    return "".join(parts), model, finish


def _non_json_diagnosis(exc: Exception, response: Any) -> str:
    """A readable cause for a reply that is not JSON at all.

    ``Expecting value: line 1 column 1 (char 0)`` is what a user sees today:
    it names neither the status, the content type nor the shape of the body,
    so an endpoint that started streaming (or that answered with an HTML error
    page) looks like a bug in MailFlow. Say what actually arrived instead.
    """
    body = ""
    status = ""
    content_type = ""
    if response is not None:
        with contextlib.suppress(Exception):
            content_type = str(response.headers.get("content-type") or "").split(";")[0]
        with contextlib.suppress(Exception):
            status = f"HTTP {response.status_code}"
        with contextlib.suppress(Exception):
            body = response.text or ""
    preview = " ".join(body.split())[:120]
    if content_type.startswith("text/event-stream") or body.lstrip().startswith("data:"):
        return (
            "endpoint replied with a server-sent-events stream that carried no "
            f"content ({status or 'no status'}); the response could not be read as JSON"
        )
    if body.lstrip().startswith("<"):
        return f"endpoint replied with HTML instead of JSON ({status or 'no status'})"
    if not preview:
        return f"endpoint replied with an empty body ({status or 'no status'})"
    return (
        f"endpoint replied with unreadable JSON ({type(exc).__name__}: {exc}; "
        f"{status or 'no status'}, body starts {preview[:60]!r})"
    )


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
        self._opt_max_tokens = int(config.options.get("max_tokens", 0) or 0)
        opt_temp = config.options.get("temperature")
        self._opt_temperature = float(opt_temp) if opt_temp not in (None, "") else None
        # ``stream`` defaults to on: a plain JSON reply is still accepted, and
        # proxies exist whose *non*-streaming path returns an empty stream
        # (verified against ai-ext), so asking for the stream is what makes
        # them answer at all. Set ``options.stream = false`` for a server that
        # rejects the flag.
        raw_stream = config.options.get("stream")
        self._use_stream = True if raw_stream in (None, "") else as_bool(raw_stream)

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
        if self._use_stream:
            body["stream"] = True
        if temperature is not None:
            body["temperature"] = temperature
        elif self._opt_temperature is not None:
            body["temperature"] = self._opt_temperature
        if self._opt_max_tokens:
            body["max_tokens"] = self._opt_max_tokens
        body.update(self._config.extra_body)
        if options:
            if isinstance(options.get("body"), dict):
                body.update(options["body"])
            if "model" in options:
                body["model"] = options["model"]
            if "temperature" in options:
                body["temperature"] = options["temperature"]
            if "max_tokens" in options:
                body["max_tokens"] = options["max_tokens"]
            if "stream" in options:
                body["stream"] = as_bool(options["stream"])
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
        # some OpenAI-compatible proxies answer with a server-sent-events
        # stream even when the request did not ask for one, and an empty
        # stream carries no answer at all; remembering the last response lets
        # the failure name what really arrived
        last_response: httpx.Response | None = None
        # a 429 window is often longer than a doubling backoff can cover —
        # honor Retry-After when the endpoint sends one, else wait out a
        # longer capped curve (the old 1s/2s max-5s curve still failed the
        # whole smart search on the AMD endpoint's per-minute quota)
        retry_after: float | None = None
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                    response = await client.post(url, headers=headers, params=params, json=body)
                    if response.status_code == 429:
                        raw = response.headers.get("Retry-After")
                        if raw is not None:
                            with contextlib.suppress(ValueError):
                                retry_after = min(float(raw), _MAX_BACKOFF_SECONDS)
                    response.raise_for_status()
                    last_response = response
                return self._completion_from_response(response)
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries or not _retryable(exc):
                    break
                if retry_after is not None:
                    backoff = retry_after
                elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                    # no Retry-After header: the quota window is typically
                    # ~60s — wait the full cap instead of 1s/2s (which
                    # lands every retry INSIDE the same window and fails)
                    backoff = _MAX_BACKOFF_SECONDS
                else:
                    backoff = min(2**attempt, _MAX_BACKOFF_SECONDS)
                logger.info(
                    "%s attempt %d/%d failed (%s); retrying in %.1fs",
                    self.backend_id,
                    attempt + 1,
                    max_retries + 1,
                    self._sanitize(exc),
                    backoff,
                )
                await asyncio.sleep(backoff)

        assert last_error is not None
        sanitized = self._sanitize(last_error)
        if isinstance(last_error, ValueError) and last_response is not None:
            # a JSON decode failure says nothing about the endpoint; name the
            # real cause (streaming reply, HTML error page, empty body)
            sanitized = _non_json_diagnosis(last_error, last_response)
        raise RuntimeError(f"llm request failed: {sanitized}")

    def _completion_from_response(self, response: httpx.Response) -> LLMCompletion:
        """Turn one successful HTTP reply into a completion.

        The reply is JSON in the normal case and a server-sent-events stream
        when the endpoint streams; both are accepted so a streaming-only
        endpoint works without configuration. An answer that carries no text
        at all is an error, never a silent empty completion — the caller would
        otherwise fail much later with an unrelated parse message.
        """
        content_type = str(response.headers.get("content-type") or "").split(";")[0]
        if content_type.startswith("text/event-stream"):
            text, model, finish = decode_sse_completion(response.text)
            if not text.strip():
                raise RuntimeError(
                    "endpoint streamed a response with no content"
                    + (f" (finish_reason={finish})" if finish else "")
                )
            return LLMCompletion(text=text, model=model, raw={"streamed": True, "finish": finish})
        try:
            payload = response.json()
        except ValueError:
            # not JSON: the endpoint streams without saying so, or returned a
            # non-JSON body; decode the frames before giving up
            text, model, finish = decode_sse_completion(response.text)
            if text.strip():
                return LLMCompletion(
                    text=text, model=model, raw={"streamed": True, "finish": finish}
                )
            raise
        completion = self._parse(payload)
        if not completion.text.strip() and not self._content_is_list(payload):
            raise RuntimeError("endpoint returned a response with no content")
        return completion

    @staticmethod
    def _content_is_list(payload: Any) -> bool:
        """Whether the reply carried structured content parts rather than
        plain text (those may legitimately be empty text)."""
        if not isinstance(payload, dict):
            return False
        choices: Any = cast("dict[str, Any]", payload).get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        first: Any = cast("list[Any]", choices)[0]
        if not isinstance(first, dict):
            return False
        message: Any = cast("dict[str, Any]", first).get("message")
        return isinstance(cast("dict[str, Any]", message).get("content"), list)

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

    def _parse(self, payload: dict[str, Any]) -> LLMCompletion:
        choices: Any = payload.get("choices") or []
        if not choices:
            raise RuntimeError("response contained no choices")
        message: Any = choices[0].get("message") or {}
        content: Any = message.get("content") or ""
        if isinstance(content, list):
            # some endpoints return content parts (e.g. [{"type": "text", "text": ...}])
            content = _join_content_parts(content)
        raw_model: Any = payload.get("model") or ""
        return LLMCompletion(text=str(content), model=str(raw_model), raw=payload)


class ResponsesBackend(OpenAIBackend):
    """The stateless ``/responses`` API: system prompt travels as
    ``instructions``, conversation turns as ``input``.

    ``stream`` is not sent here: the responses shape enables it per request
    and the JSON reply is what this backend reads."""

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
        elif self._opt_temperature is not None:
            body["temperature"] = self._opt_temperature
        if self._opt_max_tokens:
            body.setdefault("max_output_tokens", self._opt_max_tokens)
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
            if "max_tokens" in options:
                body["max_tokens"] = options["max_tokens"]
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
