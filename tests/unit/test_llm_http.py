"""Backend request/response contracts with fake HTTP transports; no model is contacted."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from mailflow.config import LLMConfig


class FakeHTTPResponse:
    """Stands in for ``httpx.Response``: the backend reads the body, the
    content type and, on a parse failure, the raw text for its diagnosis."""

    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any],
        reason: str = "",
        *,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.reason_phrase = reason
        self.headers: dict[str, str] = {"content-type": content_type}

    def json(self) -> dict[str, Any]:
        return self._payload

    @property
    def text(self) -> str:
        import json as _json

        return _json.dumps(self._payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://fake")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class FakeAsyncClient:
    """Records POSTs; returns a canned response or raises."""

    instances: ClassVar[list[FakeAsyncClient]] = []

    def __init__(self, *, error: Exception | None = None, **kwargs: object) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> FakeHTTPResponse:
        if self.error is not None:
            raise self.error
        self.calls.append({"url": url, "headers": headers, "params": params, "json": json})
        return FakeHTTPResponse(
            200,
            {
                "choices": [{"message": {"content": "the answer"}}],
                "model": "m1",
            },
        )


def make_llm_config(**overrides: object) -> LLMConfig:
    base: dict[str, object] = {
        "llm_id": "l1",
        "base_url": "https://host/v1",
        "model": "m1",
    }
    base.update(overrides)
    return LLMConfig.model_validate(base)


class TestOpenAICompatibleBackend:
    def test_request_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx
        from mailflow_llm_openai_compatible.plugin import OpenAICompatibleBackend

        FakeAsyncClient.instances.clear()
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        backend = OpenAICompatibleBackend(
            make_llm_config(
                api_key="sk-secret",
                extra_body={"temperature": 0.2},
                headers={"X-Custom": "yes"},
                query={"api-version": "2024"},
            )
        )
        import asyncio

        completion = asyncio.run(
            backend.chat(
                [{"role": "user", "content": "hi"}],
                temperature=0.9,
                options={"body": {"temperature": 0.7}},
            )
        )
        call = FakeAsyncClient.instances[-1].calls[0]
        assert call["url"] == "https://host/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer sk-secret"
        assert call["headers"]["X-Custom"] == "yes"
        assert call["params"] == {"api-version": "2024"}
        body = call["json"]
        assert body["model"] == "m1"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        # streaming is requested by default: some proxies only answer with
        # content when the request streams
        assert body["stream"] is True
        assert body["temperature"] == 0.7  # per-call option wins over extra_body
        assert completion.text == "the answer"
        assert completion.model == "m1"

    def test_no_bearer_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        import httpx
        from mailflow_llm_openai_compatible.plugin import OpenAICompatibleBackend

        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        backend = OpenAICompatibleBackend(make_llm_config())
        asyncio.run(backend.chat([{"role": "user", "content": "hi"}]))
        call = FakeAsyncClient.instances[-1].calls[0]
        assert "Authorization" not in call["headers"]

    def test_retries_exhausted_raises_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        import httpx
        from mailflow_llm_openai_compatible.plugin import OpenAICompatibleBackend

        FakeAsyncClient.instances.clear()

        def client_factory(**kwargs: object) -> FakeAsyncClient:
            return FakeAsyncClient(error=httpx.ConnectError("boom"))

        monkeypatch.setattr(httpx, "AsyncClient", client_factory)
        backend = OpenAICompatibleBackend(make_llm_config(max_retries=2, api_key="sk-topsecret"))
        with pytest.raises(RuntimeError, match="transport error"):
            asyncio.run(backend.chat([{"role": "user", "content": "hi"}]))
        # exactly max_retries + 1 attempts, and no secret/url in the message
        assert len(FakeAsyncClient.instances) == 3

    def test_http_error_status_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        import httpx
        from mailflow_llm_openai_compatible.plugin import OpenAICompatibleBackend

        class FailingResponse(FakeHTTPResponse):
            def raise_for_status(self) -> None:
                request = httpx.Request("POST", "https://host/secret-path?key=topsecret")
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

        class Client(FakeAsyncClient):
            async def post(self, *args: object, **kwargs: object) -> FakeHTTPResponse:
                return FailingResponse(401, {})

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        backend = OpenAICompatibleBackend(make_llm_config(api_key="topsecret"))
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(backend.chat([{"role": "user", "content": "hi"}]))
        assert "topsecret" not in str(excinfo.value)
        assert "401" in str(excinfo.value)


class TestAnthropicBackend:
    async def test_chat_uses_messages_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mailflow.config import LLMConfig
        from mailflow_llm_anthropic.plugin import AnthropicBackend

        captured: list[dict[str, Any]] = []

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def post(self, url: str, **kwargs: Any) -> FakeResponse:
                captured.append({"url": url, **kwargs})
                return FakeResponse(
                    {
                        "content": [{"type": "text", "text": "the answer"}],
                        "model": "claude-3-5-sonnet",
                    }
                )

        class FakeResponse:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload
                self.status_code = 200
                self.text = ""

            def json(self) -> dict[str, Any]:
                return self._payload

        monkeypatch.setattr("mailflow_llm_anthropic.plugin.httpx.AsyncClient", FakeClient)
        backend = AnthropicBackend(
            LLMConfig(
                llm_id="claude",
                provider="anthropic",
                model="claude-3-5-sonnet",
                api_key="sk-ant-secret",
                base_url="https://api.anthropic.com/v1/messages",
            )
        )
        completion = await backend.chat(
            [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Summarize this mail."},
            ]
        )
        assert completion.text == "the answer"
        assert completion.model == "claude-3-5-sonnet"
        body = captured[0]["json"]
        assert body["model"] == "claude-3-5-sonnet"
        assert body["system"] == "Be concise."
        assert body["messages"] == [{"role": "user", "content": "Summarize this mail."}]
        assert captured[0]["headers"]["x-api-key"] == "sk-ant-secret"
        assert captured[0]["headers"]["anthropic-version"] == "2023-06-01"
