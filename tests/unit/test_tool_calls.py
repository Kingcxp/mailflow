"""Tool-calling support across the four LLM backends.

The smart-action loop is only possible when a backend can carry a tool
definition out and a tool call back. Each backend speaks a different shape, so
each is pinned here: OpenAI's ``tool_calls`` (streamed in fragments and in one
JSON reply), Anthropic's ``tool_use`` blocks, and Gemini/Vertex's
``functionCall`` parts. The regression that matters most is the third case
below: a turn that carries a tool call and *no text* must not be reported as an
empty response.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import httpx
import pytest
from mailflow.config import LLMConfig
from mailflow.contracts import MessageDict
from mailflow_llm_anthropic.plugin import AnthropicBackend
from mailflow_llm_google_generative_ai.plugin import GeminiBackend
from mailflow_llm_google_vertex.plugin import VertexBackend
from mailflow_llm_openai_compatible.plugin import (
    OpenAIBackend,
    ResponsesBackend,
    decode_sse_completion,
)

# ---------------------------------------------------------------------------
# A fake transport: every backend goes through httpx.AsyncClient
# ---------------------------------------------------------------------------


class FakeHTTPResponse:
    def __init__(
        self,
        payload: dict[str, Any] | str,
        *,
        content_type: str = "application/json",
        status_code: int = 200,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.reason_phrase = "OK"
        self.headers: dict[str, str] = {"content-type": content_type}

    def json(self) -> dict[str, Any]:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload

    @property
    def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)

    def raise_for_status(self) -> None:
        return None


class CapturingClient:
    """Records the body each backend posts; replays a canned reply."""

    instances: ClassVar[list[CapturingClient]] = []

    def __init__(self, response: FakeHTTPResponse, **kwargs: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        CapturingClient.instances.append(self)

    async def __aenter__(self) -> CapturingClient:
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
        self.calls.append({"url": url, "headers": headers, "params": params, "json": json})
        return self.response


def _install(monkeypatch: pytest.MonkeyPatch, response: FakeHTTPResponse) -> None:
    CapturingClient.instances.clear()

    def factory(**_kwargs: Any) -> CapturingClient:
        return CapturingClient(response)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _config(provider: str, **options: Any) -> LLMConfig:
    return LLMConfig(
        llm_id="l1",
        provider=provider,
        base_url="https://host/v1",
        api_key="sk-test",
        model="m1",
        timeout_seconds=5.0,
        max_retries=0,
        options=dict(options),
    )


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_mail",
            "description": "Search stored mail.",
            "parameters": {
                "type": "object",
                "properties": {"contains": {"type": "string"}},
                "required": ["contains"],
            },
        },
    }
]


# ---------------------------------------------------------------------------
# OpenAI chat-completions: streamed fragments and whole replies
# ---------------------------------------------------------------------------


def test_streamed_tool_call_fragments_are_joined_and_parsed() -> None:
    """``function.arguments`` arrives split at arbitrary offsets."""
    frames = [
        {
            "model": "m1",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "find_mail", "arguments": '{"con'},
                            }
                        ]
                    }
                }
            ],
        },
        {
            "model": "m1",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'tains": "seminar"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
    ]
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"

    text, model, finish, calls = decode_sse_completion(body)

    assert text == ""
    assert model == "m1"
    assert finish == "tool_calls"
    assert len(calls) == 1
    assert calls[0].call_id == "call_1"
    assert calls[0].name == "find_mail"
    assert calls[0].arguments == {"contains": "seminar"}


def test_streamed_tool_call_with_unparseable_arguments_still_returns() -> None:
    """Broken JSON must not fail the request; the tool layer reports it."""
    frame = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "find_mail", "arguments": "{"},
                        }
                    ]
                }
            }
        ]
    }
    body = f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n"

    calls = decode_sse_completion(body)[3]

    assert calls[0].arguments == {}


def test_streamed_turn_with_only_a_tool_call_is_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "no content" guard must not swallow a tool-calling turn."""
    frame: dict[str, Any] = {
        "model": "m1",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "list_actions", "arguments": "{}"},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    body = f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n"
    _install(monkeypatch, FakeHTTPResponse(body, content_type="text/event-stream"))

    completion = asyncio.run(
        OpenAIBackend(_config("openai-completions")).chat(
            [{"role": "user", "content": "what is on my schedule"}], tools=TOOLS
        )
    )

    assert completion.text == ""
    assert [call.name for call in completion.tool_calls] == ["list_actions"]


def test_json_reply_tool_calls_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "model": "m1",
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_7",
                            "type": "function",
                            "function": {
                                "name": "find_mail",
                                "arguments": '{"contains": "exam"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    _install(monkeypatch, FakeHTTPResponse(payload))

    completion = asyncio.run(
        OpenAIBackend(_config("openai-completions", stream=False)).chat(
            [{"role": "user", "content": "the exam mail"}], tools=TOOLS
        )
    )

    assert len(completion.tool_calls) == 1
    assert completion.tool_calls[0].call_id == "call_7"
    assert completion.tool_calls[0].arguments == {"contains": "exam"}


def test_tools_are_sent_and_a_tool_result_turn_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"model": "m1", "choices": [{"message": {"content": "done"}}]}
    _install(monkeypatch, FakeHTTPResponse(payload))
    messages: list[MessageDict] = [
        {"role": "user", "content": "the seminar mail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "find_mail", "arguments": '{"contains": "seminar"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "find_mail",
            "content": "1 mail(s) matched",
        },
    ]

    completion = asyncio.run(
        OpenAIBackend(_config("openai-completions", stream=False)).chat(messages, tools=TOOLS)
    )

    body = CapturingClient.instances[-1].calls[0]["json"]
    assert body["tools"] == TOOLS
    assert body["tool_choice"] == "auto"
    # the assistant turn and the tool result travel back verbatim
    assert body["messages"][1]["tool_calls"][0]["function"]["name"] == "find_mail"
    assert body["messages"][2]["tool_call_id"] == "call_1"
    assert completion.text == "done"


# ---------------------------------------------------------------------------
# OpenAI responses shape
# ---------------------------------------------------------------------------


def test_responses_backend_maps_function_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {
        "model": "m1",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "function_call",
                "call_id": "call_9",
                "name": "find_mail",
                "arguments": '{"contains": "seminar"}',
            },
        ],
    }
    _install(monkeypatch, FakeHTTPResponse(payload))

    completion = asyncio.run(
        ResponsesBackend(_config("openai-responses")).chat(
            [{"role": "user", "content": "seminar mail"}], tools=TOOLS
        )
    )

    body = CapturingClient.instances[-1].calls[0]["json"]
    assert body["tools"][0]["name"] == "find_mail"  # flat responses shape
    assert body["tools"][0]["parameters"]["required"] == ["contains"]
    assert len(completion.tool_calls) == 1
    assert completion.tool_calls[0].call_id == "call_9"


def test_responses_backend_sends_tool_results_as_input_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "model": "m1",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    _install(monkeypatch, FakeHTTPResponse(payload))

    asyncio.run(
        ResponsesBackend(_config("openai-responses")).chat(
            [
                {"role": "user", "content": "seminar mail"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "find_mail", "arguments": '{"contains": "x"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "1 matched"},
            ]
        )
    )

    turns = CapturingClient.instances[-1].calls[0]["json"]["input"]
    assert turns[1]["type"] == "function_call"
    assert turns[2] == {"type": "function_call_output", "call_id": "call_1", "output": "1 matched"}


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_parses_tool_use_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {
        "model": "claude",
        "content": [
            {"type": "text", "text": "Looking."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "find_mail",
                "input": {"contains": "exam"},
            },
        ],
        "stop_reason": "tool_use",
    }
    _install(monkeypatch, FakeHTTPResponse(payload))

    completion = asyncio.run(
        AnthropicBackend(_config("anthropic")).chat(
            [{"role": "user", "content": "the exam mail"}], tools=TOOLS
        )
    )

    assert completion.text == "Looking."
    assert len(completion.tool_calls) == 1
    assert completion.tool_calls[0].call_id == "toolu_1"
    assert completion.tool_calls[0].arguments == {"contains": "exam"}


def test_anthropic_tool_only_reply_is_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {
        "model": "claude",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "list_actions", "input": {}}],
        "stop_reason": "tool_use",
    }
    _install(monkeypatch, FakeHTTPResponse(payload))

    completion = asyncio.run(
        AnthropicBackend(_config("anthropic")).chat(
            [{"role": "user", "content": "schedule?"}], tools=TOOLS
        )
    )

    assert completion.text == ""
    assert [call.name for call in completion.tool_calls] == ["list_actions"]


def test_anthropic_body_translates_tool_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"model": "claude", "content": [{"type": "text", "text": "ok"}]}
    _install(monkeypatch, FakeHTTPResponse(payload))

    asyncio.run(
        AnthropicBackend(_config("anthropic")).chat(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "find_mail", "arguments": '{"contains": "exam"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "toolu_1", "content": "1 matched"},
            ],
            tools=TOOLS,
        )
    )

    body = CapturingClient.instances[-1].calls[0]["json"]
    assert body["tools"] == TOOLS
    assistant = body["messages"][0]
    assert assistant["content"][0]["type"] == "tool_use"
    assert assistant["content"][0]["input"] == {"contains": "exam"}
    result = body["messages"][1]
    assert result["role"] == "user"
    assert result["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "1 matched",
    }


# ---------------------------------------------------------------------------
# Gemini and Vertex
# ---------------------------------------------------------------------------


def _gemini_payload() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": "find_mail", "args": {"contains": "seminar"}}}
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"modelVersion": "gemini-2"},
    }


@pytest.mark.parametrize(
    ("backend_cls", "provider"),
    [(GeminiBackend, "google-generative-ai"), (VertexBackend, "google-vertex")],
)
def test_google_backends_map_function_calls(
    monkeypatch: pytest.MonkeyPatch, backend_cls: Any, provider: str
) -> None:
    _install(monkeypatch, FakeHTTPResponse(_gemini_payload()))
    options: dict[str, Any] = {}
    if provider == "google-vertex":
        options = {"project": "demo", "location": "us-central1"}

    completion = asyncio.run(
        backend_cls(_config(provider, **options)).chat(
            [{"role": "user", "content": "the seminar mail"}], tools=TOOLS
        )
    )

    body = CapturingClient.instances[-1].calls[0]["json"]
    declarations = body["tools"][0]["functionDeclarations"]
    assert declarations[0]["name"] == "find_mail"
    assert declarations[0]["parameters"]["required"] == ["contains"]
    assert completion.text == ""
    assert len(completion.tool_calls) == 1
    assert completion.tool_calls[0].name == "find_mail"
    assert completion.tool_calls[0].arguments == {"contains": "seminar"}
    assert completion.tool_calls[0].call_id  # synthesised: this protocol has no id


@pytest.mark.parametrize(
    ("backend_cls", "provider"),
    [(GeminiBackend, "google-generative-ai"), (VertexBackend, "google-vertex")],
)
def test_google_backends_send_tool_results_as_function_responses(
    monkeypatch: pytest.MonkeyPatch, backend_cls: Any, provider: str
) -> None:
    _install(
        monkeypatch,
        FakeHTTPResponse({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
    )
    options: dict[str, Any] = {}
    if provider == "google-vertex":
        options = {"project": "demo", "location": "us-central1"}

    asyncio.run(
        backend_cls(_config(provider, **options)).chat(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "find_mail-0",
                            "type": "function",
                            "function": {"name": "find_mail", "arguments": '{"contains": "x"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "find_mail-0",
                    "name": "find_mail",
                    "content": "1 matched",
                },
            ]
        )
    )

    contents = CapturingClient.instances[-1].calls[0]["json"]["contents"]
    assert contents[0]["role"] == "model"
    assert contents[0]["parts"][0]["functionCall"]["args"] == {"contains": "x"}
    assert contents[1]["role"] == "user"
    assert contents[1]["parts"][0]["functionResponse"]["name"] == "find_mail"
