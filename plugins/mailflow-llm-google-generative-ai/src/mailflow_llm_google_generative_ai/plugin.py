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
import logging
from typing import Any, cast

import httpx
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, MessageDict
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


class GeminiBackend:
    backend_id = "google-generative-ai"

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
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

    def _body(self, messages: list[MessageDict], temperature: float | None) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system: list[dict[str, Any]] = []
        for message in messages:
            text = str(message.get("content", ""))
            role = str(message.get("role", "user"))
            if role == "system":
                system.append({"text": text})
                continue
            # Gemini alternates user/model roles
            contents.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
            )
        body: dict[str, Any] = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
        if system:
            body["systemInstruction"] = {"parts": system}
        generation: dict[str, Any] = {}
        if temperature is not None:
            generation["temperature"] = temperature
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
        parts: list[Any] = []
        if isinstance(candidates, list) and candidates:
            candidate = cast(dict[str, Any], candidates[0])
            content = cast(Any, candidate.get("content"))
            content_map = cast("dict[str, Any]", content) if isinstance(content, dict) else {}
            parts = cast(list[Any], content_map.get("parts") or [])
        for part in parts:
            text = cast(dict[str, Any], part).get("text")
            if text:
                chunks.append(str(text))
        if not chunks:
            raise RuntimeError("response contained no candidates text")
        usage = cast(dict[str, Any], payload.get("usageMetadata") or {})
        model_name: Any = usage.get("modelVersion") or self._config.model
        return LLMCompletion(text="".join(chunks), model=str(model_name), raw=payload)

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        url = self._url()
        headers = self._headers()
        body = self._body(messages, temperature)
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
