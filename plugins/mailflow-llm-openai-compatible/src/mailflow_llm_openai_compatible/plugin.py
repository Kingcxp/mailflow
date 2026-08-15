"""OpenAI-compatible Chat Completions backend.

Works against OpenCode relays, llama.cpp, vLLM and other compatible
services. The request URL never appears in raised error text (query strings
may carry credentials); the core LLM router additionally redacts configured
API keys from any aggregated error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import httpx
from mailflow.config import LLMConfig
from mailflow.contracts import LLMCompletion, MessageDict
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.llm.openai")

DEFAULT_PATH = "chat/completions"
_MAX_BACKOFF_SECONDS = 5.0


class OpenAICompatibleBackend:
    backend_id = "openai-compatible"

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._path = str(config.options.get("path", DEFAULT_PATH))

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
                if attempt >= max_retries:
                    break
                backoff = min(2**attempt, _MAX_BACKOFF_SECONDS)
                logger.debug(
                    "openai-compatible request attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    self._sanitize(exc),
                    backoff,
                )
                await asyncio.sleep(backoff)

        assert last_error is not None
        raise RuntimeError(f"llm request failed: {self._sanitize(last_error)}")

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

    @staticmethod
    def _parse(payload: dict[str, Any]) -> LLMCompletion:
        choices: Any = payload.get("choices") or []
        if not choices:
            raise RuntimeError("response contained no choices")
        message: Any = choices[0].get("message") or {}
        content: Any = message.get("content") or ""
        if isinstance(content, list):
            # some endpoints return content parts (e.g. [{"type": "text", "text": ...}])
            content = OpenAICompatibleBackend._join_content_parts(content)
        raw_model: Any = payload.get("model") or ""
        return LLMCompletion(text=str(content), model=str(raw_model), raw=payload)


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-llm-openai-compatible",
    name="OpenAI-Compatible Backend",
    version="0.1.0",
    description="Chat completions over any OpenAI-compatible HTTP endpoint",
)


class LLMPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_llm("openai-compatible", OpenAICompatibleBackend)


plugin = LLMPlugin()

__all__ = ["LLMPlugin", "OpenAICompatibleBackend", "plugin"]
