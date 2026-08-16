"""Reference conformance tests for `LLMClient.call()` implementations.

`AnthropicClient.call()` — the content-block parsing and Usage mapping, as
opposed to `_split()`'s request shaping already covered by
`test_anthropic_client.py` — had no test at all before this file. Any new
provider client must satisfy the same behavior these tests pin down: text
concatenation, tool_use -> ToolCall mapping, Usage field mapping, model
passthrough, and omitting `tools` from the request when none are registered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx2
import openai

from kubemend.llm.anthropic_client import AnthropicClient
from kubemend.llm.client import Message
from kubemend.llm.openai_client import OpenAICompatibleClient


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    # Deliberately no cache_* attributes by default: AnthropicClient.call()
    # reads them via getattr(..., 0), and a response with prompt caching
    # disabled genuinely lacks these fields on the SDK object.


@dataclass
class _CachedUsage(_Usage):
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Response:
    content: list[Any]
    usage: _Usage = field(default_factory=_Usage)
    model: str = "claude-sonnet-5"


class _StubMessages:
    def __init__(self, response: _Response) -> None:
        self._response = response
        self.last_request: dict[str, object] | None = None

    def create(self, **kwargs: object) -> _Response:
        self.last_request = kwargs
        return self._response


class _StubAnthropic:
    """Conforms to the surface AnthropicClient actually calls: `.messages.create()`."""

    def __init__(self, response: _Response) -> None:
        self.messages = _StubMessages(response)


def _client(response: _Response) -> tuple[AnthropicClient, _StubAnthropic]:
    from kubemend.config import RunConfig

    stub = _StubAnthropic(response)
    return AnthropicClient(RunConfig(), client=stub), stub  # type: ignore[arg-type]


def test_text_blocks_are_concatenated() -> None:
    response = _Response(content=[_TextBlock("first. "), _TextBlock("second.")])
    client, _ = _client(response)

    result = client.call([Message("user", "hi")])

    assert result.text == "first. second."


def test_tool_use_blocks_map_to_tool_calls() -> None:
    response = _Response(
        content=[
            _ToolUseBlock(id="call_1", name="query_metrics", input={"promql": "up"}),
            _ToolUseBlock(id="call_2", name="search_logs", input={"logql": "{}"}),
        ]
    )
    client, _ = _client(response)

    result = client.call([Message("user", "hi")])

    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ("call_1", "query_metrics", {"promql": "up"}),
        ("call_2", "search_logs", {"logql": "{}"}),
    ]
    assert result.text == "", "no text blocks in this response"


def test_usage_without_cache_fields_defaults_to_zero() -> None:
    response = _Response(content=[], usage=_Usage(input_tokens=100, output_tokens=20))
    client, _ = _client(response)

    result = client.call([Message("user", "hi")])

    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cached_input_tokens == 0
    assert result.usage.cache_creation_tokens == 0


def test_usage_with_cache_fields_maps_both() -> None:
    response = _Response(
        content=[],
        usage=_CachedUsage(
            input_tokens=50,
            output_tokens=10,
            cache_read_input_tokens=200,
            cache_creation_input_tokens=30,
        ),
    )
    client, _ = _client(response)

    result = client.call([Message("user", "hi")])

    assert result.usage.cached_input_tokens == 200
    assert result.usage.cache_creation_tokens == 30


def test_response_model_passes_through() -> None:
    response = _Response(content=[], model="claude-haiku-4-5-20260101")
    client, _ = _client(response)

    result = client.call([Message("user", "hi")])

    assert result.model == "claude-haiku-4-5-20260101"


def test_tier_selects_the_configured_model_name() -> None:
    from kubemend.config import ModelConfig, ModelSpec, RunConfig

    response = _Response(content=[])
    stub = _StubAnthropic(response)
    cfg = RunConfig(
        model=ModelConfig(main=ModelSpec(name="main-model"), cheap=ModelSpec(name="cheap-model"))
    )
    client = AnthropicClient(cfg, client=stub)  # type: ignore[arg-type]

    client.call([Message("user", "hi")], tier="cheap")

    assert stub.messages.last_request is not None
    assert stub.messages.last_request["model"] == "cheap-model"


def test_no_tools_key_in_request_when_tools_is_none() -> None:
    response = _Response(content=[])
    client, stub = _client(response)

    client.call([Message("user", "hi")], tools=None)

    assert stub.messages.last_request is not None
    assert "tools" not in stub.messages.last_request


def test_no_tools_key_in_request_when_tools_is_empty() -> None:
    """Handoff and compaction both call with `tools=[]` (core/handoff.py,
    core/context.py) — the request must omit the key entirely, not send an
    empty list, so this stays uniform across providers that reject `tools: []`."""
    response = _Response(content=[])
    client, stub = _client(response)

    client.call([Message("user", "hi")], tools=[])

    assert stub.messages.last_request is not None
    assert "tools" not in stub.messages.last_request


def test_tools_are_translated_to_the_anthropic_input_schema_key() -> None:
    response = _Response(content=[])
    client, stub = _client(response)

    client.call(
        [Message("user", "hi")],
        tools=[
            {
                "name": "query_metrics",
                "description": "Run a PromQL query.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )

    assert stub.messages.last_request is not None
    sent = stub.messages.last_request["tools"]
    assert sent == [
        {
            "name": "query_metrics",
            "description": "Run a PromQL query.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]


def test_max_tokens_is_set_on_every_request() -> None:
    response = _Response(content=[])
    client, stub = _client(response)

    client.call([Message("user", "hi")])

    assert stub.messages.last_request is not None
    assert stub.messages.last_request["max_tokens"] == 16_000


# -- OpenAICompatibleClient: same properties, mirrored ------------------------
#
# A stub `client=` injection here — same shape as the Anthropic tests above —
# covers the response-mapping properties fast and symmetrically. It cannot
# catch a wrong *request* shape, since the stub never inspects what was sent
# through the real SDK's request-building code; that gap is what the
# MockTransport test below exists for.


def _oai_response(
    *,
    content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    usage_extra: dict[str, Any] | None = None,
    model: str = "gpt-x",
) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, **(usage_extra or {})
    )
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage, model=model)


class _StubOpenAICompletions:
    def __init__(self, response: SimpleNamespace) -> None:
        self._response = response
        self.last_request: dict[str, object] | None = None

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.last_request = kwargs
        return self._response


def _oai_client(response: SimpleNamespace) -> tuple[OpenAICompatibleClient, _StubOpenAICompletions]:
    from kubemend.config import RunConfig

    stub = _StubOpenAICompletions(response)
    fake_sdk = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    return OpenAICompatibleClient(RunConfig(), client=fake_sdk), stub  # type: ignore[arg-type]


def test_openai_text_content_passes_through() -> None:
    client, _ = _oai_client(_oai_response(content="the fix is in place"))

    result = client.call([Message("user", "hi")])

    assert result.text == "the fix is in place"


def test_openai_none_content_becomes_empty_string() -> None:
    """A tool-calling turn's `message.content` is `None`, not `""`, on this SDK."""
    client, _ = _oai_client(_oai_response(content=None))

    result = client.call([Message("user", "hi")])

    assert result.text == ""


def test_openai_tool_calls_map_to_tool_calls() -> None:
    tool_calls = [
        SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="query_metrics", arguments='{"promql": "up"}'),
        )
    ]
    client, _ = _oai_client(_oai_response(tool_calls=tool_calls))

    result = client.call([Message("user", "hi")])

    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ("call_1", "query_metrics", {"promql": "up"})
    ]


def test_openai_malformed_tool_arguments_are_reflected_not_crashed() -> None:
    """Local models sometimes emit invalid JSON for tool arguments. The run
    must survive this as a normal tool error, not a client crash."""
    tool_calls = [
        SimpleNamespace(
            id="call_1", function=SimpleNamespace(name="query_metrics", arguments="{not json")
        )
    ]
    client, _ = _oai_client(_oai_response(tool_calls=tool_calls))

    result = client.call([Message("user", "hi")])

    assert result.tool_calls[0].arguments == {"__malformed_arguments__": "{not json"}


def test_openai_usage_without_cache_details_subtracts_nothing() -> None:
    client, _ = _oai_client(_oai_response(prompt_tokens=100, completion_tokens=20))

    result = client.call([Message("user", "hi")])

    assert result.usage.input_tokens == 100
    assert result.usage.cached_input_tokens == 0
    assert result.usage.output_tokens == 20


def test_openai_cached_tokens_are_subtracted_from_input_per_the_additive_contract() -> None:
    """OpenAI's prompt_tokens INCLUDES cached tokens; Usage.input_tokens must
    exclude them, or trace/cost.py double-counts (see llm/client.py's Usage
    docstring)."""
    response = _oai_response(
        prompt_tokens=500,
        completion_tokens=20,
        usage_extra={"prompt_tokens_details": SimpleNamespace(cached_tokens=400)},
    )
    client, _ = _oai_client(response)

    result = client.call([Message("user", "hi")])

    assert result.usage.cached_input_tokens == 400
    assert result.usage.input_tokens == 100


def test_openai_deepseek_style_cache_field_is_used_when_details_are_absent() -> None:
    response = _oai_response(
        prompt_tokens=500, completion_tokens=20, usage_extra={"prompt_cache_hit_tokens": 300}
    )
    client, _ = _oai_client(response)

    result = client.call([Message("user", "hi")])

    assert result.usage.cached_input_tokens == 300
    assert result.usage.input_tokens == 200


def test_openai_model_passes_through() -> None:
    client, _ = _oai_client(_oai_response(model="deepseek-v4-flash"))

    result = client.call([Message("user", "hi")])

    assert result.model == "deepseek-v4-flash"


def test_openai_no_tools_key_when_none() -> None:
    client, stub = _oai_client(_oai_response())

    client.call([Message("user", "hi")], tools=None)

    assert stub.last_request is not None
    assert "tools" not in stub.last_request


def test_openai_no_tools_key_when_empty() -> None:
    client, stub = _oai_client(_oai_response())

    client.call([Message("user", "hi")], tools=[])

    assert stub.last_request is not None
    assert "tools" not in stub.last_request


# -- Wire-format test: the real SDK, a mocked transport ----------------------
#
# The stubs above bypass the SDK's own request-building code entirely, so they
# cannot catch a wrong request shape (tool schema translation, which
# max-tokens param, message layout). This constructs a real `openai.OpenAI`
# against a mocked HTTP transport instead. The openai SDK in this environment
# depends on `httpx2` (the httpx 2.x line, published under that name during
# its rollout) rather than the `httpx` 0.x used elsewhere in this repo for
# Prometheus/Loki — a real, if surprising, distinction, not a typo.


_Handler = Callable[[httpx2.Request], httpx2.Response]


def _mock_openai_client(handler: _Handler) -> tuple[openai.OpenAI, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def _capturing_handler(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = request.content
        return handler(request)

    http_client = httpx2.Client(transport=httpx2.MockTransport(_capturing_handler))
    sdk = openai.OpenAI(base_url="http://fake.local/v1", api_key="test", http_client=http_client)
    return sdk, captured


def _json_response(payload: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(200, json=payload)


def test_wire_format_tool_schema_and_message_layout() -> None:
    import json as _json

    from kubemend.config import RunConfig

    payload = {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-x",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _json_response(payload)

    sdk, captured = _mock_openai_client(handler)
    client = OpenAICompatibleClient(RunConfig(), base_url="http://fake.local/v1", client=sdk)

    client.call(
        [
            Message("system", "PINNED SYSTEM", pinned=True),
            Message("user", "investigate"),
        ],
        tools=[
            {
                "name": "query_metrics",
                "description": "Run a PromQL query.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )

    sent = _json.loads(captured["body"])
    assert sent["messages"][0] == {"role": "system", "content": "PINNED SYSTEM"}
    assert sent["messages"][1] == {"role": "user", "content": "investigate"}
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "query_metrics",
                "description": "Run a PromQL query.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    # base_url is set on this client, so the older `max_tokens` param is used
    # rather than `max_completion_tokens` (see openai_client.py's heuristic).
    assert sent["max_tokens"] == 16_000
    assert "max_completion_tokens" not in sent
