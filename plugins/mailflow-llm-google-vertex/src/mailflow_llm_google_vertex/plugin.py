"""Google Vertex AI backend (Gemini models on Vertex).

Component id ``google-vertex``. Talks to the Vertex AI endpoint:

    POST {base}/v1/projects/{project}/locations/{location}/
         publishers/google/models/{model}:generateContent
    Authorization: Bearer <access token>

Configuration:
- ``base_url`` defaults to ``https://aiplatform.googleapis.com``
- ``options.project`` / ``options.location`` select the project and region
  (location default ``us-central1``)
- authentication: ``api_key``/``api_key_env`` carries a short-lived OAuth2
  access token (e.g. from ``gcloud auth print-access-token``), or set
  ``options.service_account_file`` to a service-account JSON — when the
  ``google-auth`` package is installed it is used to mint tokens
  automatically.

Error text is sanitized; only transient failures are retried.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from typing import Any, cast

import httpx
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, MessageDict
from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.llm.google-vertex")

_DEFAULT_BASE = "https://aiplatform.googleapis.com"
_DEFAULT_LOCATION = "us-central1"
_MAX_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {408, 429, *range(500, 600)}
_TOKEN_SKEW_SECONDS = 300


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in _RETRYABLE_STATUS


class _TokenCache:
    """Mints and caches Vertex OAuth2 tokens from a service account."""

    def __init__(self) -> None:
        self._token = ""
        self._expires_at = 0.0

    def get(self, service_account_file: str) -> str | None:
        now = time.monotonic()
        if self._token and now < self._expires_at:
            return self._token
        try:
            # optional dependency, imported dynamically so the plugin works
            # without it when an explicit access token is configured
            auth: Any = importlib.import_module("google.auth")
            requests_mod: Any = importlib.import_module("google.auth.transport.requests")
            credentials, _project = auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            request: Any = requests_mod.Request()
            credentials.refresh(request)
        except ImportError as exc:
            raise RuntimeError(
                "options.service_account_file requires the 'google-auth' package "
                "(uv pip install google-auth); alternatively put a short-lived "
                "access token into api_key/api_key_env"
            ) from exc
        except Exception as exc:  # google.auth raises assorted types
            logger.warning("vertex token refresh failed: %s", type(exc).__name__)
            return None
        credentials_any: Any = credentials
        token = getattr(credentials_any, "token", None)
        expiry = getattr(credentials_any, "expiry", None)
        if not token:
            return None
        self._token = str(token)
        expires_in = 3600.0
        if expiry is not None:
            from datetime import datetime

            try:
                expires_in = max(
                    60.0,
                    (
                        datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                        - datetime.now(expiry.tzinfo)
                    ).total_seconds(),
                )
            except (ValueError, AttributeError):
                expires_in = 3600.0
        self._expires_at = time.monotonic() + expires_in - _TOKEN_SKEW_SECONDS
        return self._token


class VertexBackend:
    backend_id = "google-vertex"

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._base = (config.base_url or _DEFAULT_BASE).rstrip("/")
        self._project = str(config.options.get("project", ""))
        self._location = str(config.options.get("location", _DEFAULT_LOCATION))
        self._service_account_file = str(config.options.get("service_account_file", "") or "")
        if not self._project:
            raise ValueError(
                "google-vertex requires options.project (and optionally options.location)"
            )
        self._tokens = _TokenCache()

    def _url(self) -> str:
        model = self._config.model.strip("/")
        return (
            f"{self._base}/v1/projects/{self._project}/locations/{self._location}"
            f"/publishers/google/models/{model}:generateContent"
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token: str | None = None
        if self._service_account_file:
            token = self._tokens.get(self._service_account_file)
        if token is None and self._config.api_key:
            token = self._config.api_key
        if token is None:
            raise RuntimeError(
                "google-vertex has no credential: set api_key/api_key_env to an "
                "access token or configure options.service_account_file"
            )
        headers["Authorization"] = f"Bearer {token}"
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
        body = self._body(messages, temperature)
        max_retries = max(0, min(self._config.max_retries, 20))

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                # headers built per attempt so a refreshed token is picked up
                async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                    response = await client.post(url, headers=self._headers(), json=body)
                    response.raise_for_status()
                return self._parse(response.json())
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries or not _retryable(exc):
                    break
                backoff = min(2**attempt, _MAX_BACKOFF_SECONDS)
                logger.debug(
                    "vertex attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    self._sanitize(exc),
                    backoff,
                )
                await asyncio.sleep(backoff)

        assert last_error is not None
        raise RuntimeError(f"llm request failed: {self._sanitize(last_error)}")


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-llm-google-vertex",
    name="Google Vertex AI Backend",
    version="0.1.0",
    description="Gemini on Vertex AI transport (component id: google-vertex)",
    kinds=[ComponentKind.LLM_BACKEND],
)


class LLMPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        registrar.add_llm("google-vertex", VertexBackend)


plugin = LLMPlugin()

__all__ = ["LLMPlugin", "VertexBackend", "plugin"]
