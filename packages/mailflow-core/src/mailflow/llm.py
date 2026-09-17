"""Named LLM routing with ordered fallback and de-duplication.

The router maps a named LLM id to its backend instance, tries backends in
fallback order, stamps the completion with the backend/llm actually used and
raises a single ``LLMRouteError`` aggregating sanitized per-backend failures.
"""

from __future__ import annotations

import asyncio
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

    @property
    def _secrets(self) -> list[str]:
        # read live so keys configured after startup are still redacted
        return [cfg.api_key for cfg in self._configs.values() if cfg.api_key]

    def backend_for(self, llm_id: str) -> tuple[LLMBackend, LLMConfig] | None:
        backend = self._backends.get(llm_id)
        config = self._configs.get(llm_id)
        if backend is None or config is None:
            return None
        return backend, config

    async def warmup(self) -> bool:
        """Load the first backend once so the first analysis is not cold.

        A locally hosted model can spend minutes loading the weights on its
        first request; measured against the user's own endpoint, the first call
        exceeded 180 s while later ones were fast. Paying that with one tiny
        completion keeps the per-mail timeout for actual analysis. Best effort:
        a failure only warns.
        """
        primary = next((name for name in self._configs), "")
        resolved = self.backend_for(primary) if primary else None
        if resolved is None:
            return False
        backend, config = resolved
        try:
            await asyncio.wait_for(
                backend.chat(
                    [{"role": "user", "content": "Reply with the single word: ok"}],
                    temperature=0.0,
                    options={"max_tokens": 4},
                ),
                # a cold load legitimately takes minutes; the user's first mail
                # must not be the one paying for it
                timeout=max(300.0, float(config.timeout_seconds or 0) or 0.0),
            )
        except Exception as exc:
            logger.warning(
                "llm warm-up failed (%s); the first mail may pay the cold start",
                type(exc).__name__,
            )
            return False
        logger.info("llm warm-up done (%s)", primary)
        return True

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
            deadline = max(1.0, float(config.timeout_seconds or 0) or 120.0)
            try:
                # The configured timeout is a wall-clock deadline for the whole
                # request. The transport timeout alone is per read/write chunk,
                # so a model that trickles tokens could run far past the value
                # the user set and still succeed; wait_for makes it a hard bound
                # and the caller learns the request exceeded it.
                completion = await asyncio.wait_for(
                    backend.chat(messages, temperature=temperature, options=options),
                    timeout=deadline,
                )
            except TimeoutError:
                logger.warning(
                    "llm %r (backend %r) exceeded its %.0fs request timeout",
                    llm_id,
                    config.provider,
                    deadline,
                )
                errors.append(f"{llm_id}: request exceeded {deadline:g}s")
                continue
            except Exception as exc:
                logger.warning("llm %r (backend %r) failed: %s", llm_id, config.provider, exc)
                errors.append(f"{llm_id}: {self._redact(str(exc)) or type(exc).__name__}")
                continue
            completion.llm_id = llm_id
            completion.backend = config.provider
            logger.info(
                "llm %r served by backend %r model=%r", llm_id, config.provider, completion.model
            )
            return completion

        detail = "; ".join(errors) if errors else "no llm backends configured"
        raise LLMRouteError(detail)
