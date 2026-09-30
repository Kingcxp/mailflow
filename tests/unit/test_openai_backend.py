"""OpenAI-compatible transport: streaming replies, empty answers, diagnostics.

The endpoint this was written for (an OpenAI-compatible proxy in front of a
Claude deployment) answers ``text/event-stream`` no matter what the request
asks for, and its non-streaming path returns a stream frame with no content at
all. Both used to surface to the user as
``llm request failed: Expecting value: line 1 column 1 (char 0)`` — a parse
error that names neither the status nor the content type.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from mailflow.config import LLMConfig
from mailflow_llm_openai_compatible.plugin import (
    OpenAIBackend,
    as_bool,
    decode_sse_completion,
)


def _config(**options: Any) -> LLMConfig:
    return LLMConfig(
        llm_id="llm-1",
        provider="openai-completions",
        base_url="https://endpoint.invalid/v1",
        api_key="sk-test",
        model="deepseek-flash",
        timeout_seconds=5.0,
        max_retries=0,
        options=dict(options),
    )


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> dict[str, Any]:
    """Route httpx at a fake endpoint; returns the captured request bodies."""
    captured: dict[str, Any] = {"bodies": []}
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        captured["params"] = kwargs
        return original(
            transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"}
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


def _sse(*frames: str) -> bytes:
    return ("".join(f"data: {frame}\n\n" for frame in frames) + "data: [DONE]\n\n").encode()


def _chunk(content: str, *, model: str = "m1", finish: str | None = None) -> str:
    choice: dict[str, Any] = {"index": 0, "delta": {"content": content}}
    if finish:
        choice["finish_reason"] = finish
    return json.dumps({"model": model, "choices": [choice]})


def test_decode_sse_joins_deltas_and_ignores_reasoning() -> None:
    """A thinking model's scratchpad must never become the answer."""
    body = (
        "data: "
        + json.dumps(
            {
                "model": "thinking-model",
                "choices": [
                    {"delta": {"reasoning_content": "let me think", "content": ""}},
                    {"delta": {"content": '{"summary":'}},
                ],
            }
        )
        + "\n\n"
        + "data: "
        + _chunk('"hi"}', finish="stop")
        + "\n\n"
        + "data: [DONE]\n\n"
    )
    text, model, finish = decode_sse_completion(body)
    assert text == '{"summary":"hi"}'
    # later frames override the model id, matching the endpoint's own stream
    assert model == "m1"
    assert finish == "stop"


def test_decode_sse_accepts_message_style_frames() -> None:
    """Some proxies send full ``message`` objects instead of deltas."""
    body = f"data: {json.dumps({'choices': [{'message': {'content': 'whole'}}]})}\n\n"
    assert decode_sse_completion(body)[0] == "whole"


async def test_streaming_reply_is_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(_chunk("hel"), _chunk("lo", finish="stop")),
        )

    _install(monkeypatch, handler)
    completion = await OpenAIBackend(_config()).chat([{"role": "user", "content": "hi"}])
    assert completion.text == "hello"
    assert completion.model == "m1"


async def test_streaming_is_requested_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A streaming-first endpoint only emits content when the request streams."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(_chunk("ok", finish="stop")),
        )

    _install(monkeypatch, handler)
    await OpenAIBackend(_config()).chat([{"role": "user", "content": "hi"}])
    assert seen[0]["stream"] is True


async def test_streaming_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"model": "m1", "choices": [{"message": {"content": "plain"}}]},
        )

    _install(monkeypatch, handler)
    completion = await OpenAIBackend(_config(stream=False)).chat(
        [{"role": "user", "content": "hi"}]
    )
    assert "stream" not in seen[0]
    assert completion.text == "plain"


async def test_a_stream_without_content_is_an_error_naming_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact ai-ext shape: a 200 stream whose only frame carries
    ``choices: []``. It must not reach the caller as a JSON decode error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                json.dumps({"model": "m1", "choices": [], "usage": {"completion_tokens": 0}})
            ),
        )

    _install(monkeypatch, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await OpenAIBackend(_config()).chat([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "no content" in message
    assert "Expecting value" not in message
    # the raw model text never leaks into user-facing error text
    assert "deepseek-flash" not in message


async def test_mislabelled_stream_is_still_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy that streams while claiming ``application/json``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=_sse(_chunk("rescued", finish="stop")),
        )

    _install(monkeypatch, handler)
    completion = await OpenAIBackend(_config(stream=False)).chat(
        [{"role": "user", "content": "hi"}]
    )
    assert completion.text == "rescued"


async def test_empty_json_body_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"model": "m1", "choices": [{"message": {"content": ""}}]},
        )

    _install(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="no content"):
        await OpenAIBackend(_config(stream=False)).chat([{"role": "user", "content": "hi"}])


async def test_html_error_page_names_the_real_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway error page used to surface as a bare JSON decode error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body>502 Bad Gateway</body></html>",
        )

    _install(monkeypatch, handler)
    with pytest.raises(RuntimeError) as excinfo:
        await OpenAIBackend(_config(stream=False)).chat([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "HTML" in message
    assert "Expecting value" not in message


async def test_http_status_is_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            headers={"content-type": "application/json"},
            json={"detail": {"error": {"message": "Model x is not available"}}},
        )

    _install(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        await OpenAIBackend(_config(stream=False)).chat([{"role": "user", "content": "hi"}])


def _content_parts_reply() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "model": "m1",
            "choices": [{"message": {"content": [{"type": "text", "text": "parts"}]}}],
        },
    )


async def test_structured_content_parts_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _content_parts_reply()

    _install(monkeypatch, handler)
    completion = await OpenAIBackend(_config(stream=False)).chat(
        [{"role": "user", "content": "hi"}]
    )
    assert completion.text == "parts"


async def test_content_parts_inside_a_stream_are_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = json.dumps(
        {
            "model": "m1",
            "choices": [
                {"index": 0, "delta": {"content": [{"type": "text", "text": "streamed parts"}]}}
            ],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(frame),
        )

    _install(monkeypatch, handler)
    completion = await OpenAIBackend(_config()).chat([{"role": "user", "content": "hi"}])
    assert completion.text == "streamed parts"


def test_as_bool_parses_config_strings() -> None:
    assert as_bool(True) is True
    assert as_bool(False) is False
    for value in ("false", "0", "no", "off", "FALSE", ""):
        assert as_bool(value) is False
    for value in ("true", "1", "yes", "on"):
        assert as_bool(value) is True


def test_config_string_stream_false_is_honoured() -> None:
    """TOML/`options` values arrive as strings; "false" must not read as true."""
    backend = OpenAIBackend(_config(stream="false"))
    assert cast(Any, backend)._use_stream is False
