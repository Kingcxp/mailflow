# LLM backend

An LLM backend is a chat-completions transport. One instance is created per
named LLM configuration.

## Contract

```python
class LLMBackend(Protocol):
    backend_id: str

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion: ...
```

Return `LLMCompletion(text=..., model=..., raw=response_dict)`; the router
stamps `llm_id` and `backend` (the plugin id).

## Registration

```python
def build_backend(config: LLMConfig) -> MyBackend:
    return MyBackend(config)  # base_url, api_key, model, headers, query, extra_body


registrar.add_llm("my-backend", build_backend)
```

Configuration references the adapter by component id:

```toml
[[llms]]
llm_id = "go"
provider = "my-backend"
base_url = "https://relay.example/v1"
api_key = "${TOKEN}"          # optional; omit for token-less endpoints
model = "deepseek-chat"
timeout_seconds = 60
max_retries = 2
headers = { }
query = { }
extra_body = { }
options = { }                  # backend-specific (e.g. path = "chat/completions")
```

`LLMConfig` carries everything a transport needs: interface format, remote
address, API token and optional request configuration.

## Rules for authors

- Add the `Authorization: Bearer` header **only** when a token is configured
  (use `setdefault` so user headers win).
- Merge configured `headers`/`query`/`extra_body` with per-call `options`.
- Bounded retries with backoff.
- **Secrets**: never include the request URL or query in raised error text
  (query strings may carry credentials); the router redacts keys again.
- Parse `choices[0].message.content` including the list-of-parts form some
  endpoints return.
