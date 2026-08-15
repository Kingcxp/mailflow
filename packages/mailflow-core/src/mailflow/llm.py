"""Named LLM routing with ordered fallback and de-duplication.

The router maps a named LLM id to its backend instance, tries backends in
fallback order, stamps the completion with the backend/llm actually used and
raises a single ``LLMRouteError`` aggregating sanitized per-backend failures.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from mailflow.config import LLMConfig
from mailflow.contracts import LLMBackend, LLMCompletion, MessageDict

logger = logging.getLogger("mailflow.llm")


class LLMRouteError(RuntimeError):
    """All configured backends failed for a chat request."""


class LLMRouterImpl:
    """Concrete router; satisfies the ``mailflow.contracts.LLMRouter`` protocol.

    ``backends`` maps named llm ids to their backend *instances* (one instance
    per configured LLM — each has its own endpoint, model and credentials);
    ``configs`` maps the same ids to their configuration.
    """

    def __init__(
        self, backends: Mapping[str, LLMBackend], configs: Mapping[str, LLMConfig]
    ) -> None:
        self._backends = dict(backends)
        self._configs = dict(configs)
        self._secrets = [cfg.api_key for cfg in self._configs.values() if cfg.api_key]

    def backend_for(self, llm_id: str) -> tuple[LLMBackend, LLMConfig] | None:
        backend = self._backends.get(llm_id)
        config = self._configs.get(llm_id)
        if backend is None or config is None:
            return None
        return backend, config

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        primary: str,
        fallback: list[str] | None = None,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        candidate_ids: list[str] = []
        for llm_id in [primary, *(fallback or [])]:
            if llm_id in candidate_ids:
                continue  # de-duplicate repeated ids
            candidate_ids.append(llm_id)

        errors: list[str] = []
        for llm_id in candidate_ids:
            resolved = self.backend_for(llm_id)
            if resolved is None:
                errors.append(f"llm {llm_id!r}: backend not registered")
                continue
            backend, config = resolved
            try:
                completion = await backend.chat(messages, temperature=temperature, options=options)
            except Exception as exc:
                logger.warning("llm %r (backend %r) failed: %s", llm_id, config.provider, exc)
                errors.append(f"{llm_id}: {self._redact(str(exc))}")
                continue
            completion.llm_id = llm_id
            completion.backend = config.provider
            logger.debug(
                "llm %r served by backend %r model=%r", llm_id, config.provider, completion.model
            )
            return completion

        detail = "; ".join(errors) if errors else "no llm backends configured"
        raise LLMRouteError(detail)
