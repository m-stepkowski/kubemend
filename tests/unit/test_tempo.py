"""Tempo trace provider (ARCHITECTURE.md §3.2, tool contract `query_traces`, M13).

Wire-level against `httpx.MockTransport`: no Tempo, no Grafana Cloud account.
What matters is the shaping — search returns metadata only, so spans come from
a second fetch — and that the executor never raises into the loop (I2) or
leaks a credential-looking span attribute past redaction (I3).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from kubemend.tools.base import ClientError, TransportError
from kubemend.tools.observability.provider import TraceQuery
from kubemend.tools.observability.tempo import (
    MAX_LIMIT,
    MAX_SPANS_PER_TRACE,
    TempoProvider,
    traces_tool_spec,
)

FIXED_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _span(
    name: str, start_ns: int, end_ns: int, attrs: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "status": {"code": "STATUS_CODE_ERROR"},
        "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in (attrs or {}).items()],
    }


def _trace_body(spans: list[dict[str, Any]], service: str = "shop-api") -> dict[str, Any]:
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


Handler = Callable[[httpx.Request], httpx.Response]


def _provider(handler: Handler) -> TempoProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TempoProvider("https://tempo.example", client=client, now=lambda: FIXED_NOW)


def _search_then_trace(
    search: dict[str, Any], trace: dict[str, Any], calls: list[httpx.Request] | None = None
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if request.url.path == "/api/search":
            return httpx.Response(200, json=search)
        return httpx.Response(200, json=trace)

    return handler


def test_search_metadata_is_joined_with_spans_from_the_follow_up_fetch() -> None:
    provider = _provider(
        _search_then_trace(
            {"traces": [{"traceID": "abc", "rootTraceName": "GET /checkout", "durationMs": 900}]},
            _trace_body([_span("db.query", 0, 700_000_000)]),
        )
    )

    result = provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))

    assert len(result.traces) == 1
    trace = result.traces[0]
    assert trace.trace_id == "abc"
    assert trace.root_name == "GET /checkout"
    assert trace.duration_ms == 900
    assert [s.name for s in trace.spans] == ["db.query"]
    assert trace.spans[0].service == "shop-api"
    assert trace.spans[0].duration_ms == pytest.approx(700.0)


def test_spans_come_back_slowest_first_because_that_names_the_culprit() -> None:
    provider = _provider(
        _search_then_trace(
            {"traces": [{"traceID": "abc", "durationMs": 900}]},
            _trace_body(
                [
                    _span("fast", 0, 10_000_000),
                    _span("slowest", 0, 800_000_000),
                    _span("middling", 0, 100_000_000),
                ]
            ),
        )
    )

    result = provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))

    assert [s.name for s in result.traces[0].spans] == ["slowest", "middling", "fast"]


def test_min_duration_is_sent_as_a_tempo_duration_string() -> None:
    calls: list[httpx.Request] = []
    provider = _provider(
        _search_then_trace({"traces": []}, {}, calls),
    )

    provider.query_traces(TraceQuery(query="{}", start="-30m", end="now", min_duration_ms=250))

    assert calls[0].url.params["minDuration"] == "250ms"


def test_relative_times_are_resolved_provider_side_into_epoch_seconds() -> None:
    calls: list[httpx.Request] = []
    provider = _provider(_search_then_trace({"traces": []}, {}, calls))

    provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))

    params = calls[0].url.params
    assert int(params["end"]) - int(params["start"]) == 30 * 60


def test_a_limit_above_the_cap_is_clamped_because_each_hit_costs_a_round_trip() -> None:
    calls: list[httpx.Request] = []
    provider = _provider(_search_then_trace({"traces": []}, {}, calls))

    provider.query_traces(TraceQuery(query="{}", start="-30m", end="now", limit=500))

    assert int(calls[0].url.params["limit"]) == MAX_LIMIT


def test_spans_per_trace_are_capped(caplog: pytest.LogCaptureFixture) -> None:
    many = [_span(f"s{i}", 0, (i + 1) * 1_000_000) for i in range(MAX_SPANS_PER_TRACE + 30)]
    provider = _provider(
        _search_then_trace({"traces": [{"traceID": "abc", "durationMs": 9}]}, _trace_body(many))
    )

    result = provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))

    assert len(result.traces[0].spans) == MAX_SPANS_PER_TRACE


def test_an_unreadable_trace_degrades_to_metadata_rather_than_losing_the_others() -> None:
    """One bad trace must not cost the other nineteen — knowing a slow trace
    exists is still worth the turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": "abc", "durationMs": 900}]})
        return httpx.Response(500)

    provider = _provider(handler)

    result = provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))

    assert len(result.traces) == 1
    assert result.traces[0].trace_id == "abc"
    assert result.traces[0].duration_ms == 900
    assert result.traces[0].spans == []


def test_no_match_returns_an_actionable_hint() -> None:
    provider = _provider(_search_then_trace({"traces": []}, {}))

    result = provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))

    assert result.traces == []
    assert result.hint is not None
    assert "min_duration_ms" in result.hint


def test_span_attributes_pass_through_redaction() -> None:
    """Span attributes are user-controlled strings reaching the model, exactly
    like log lines — defence in depth behind the executor's own I3 pass."""
    provider = _provider(
        _search_then_trace(
            {"traces": [{"traceID": "abc", "durationMs": 9}]},
            _trace_body(
                [
                    _span(
                        "http.request",
                        0,
                        1_000_000,
                        {"http.header.authorization": "Bearer sk-abcdefghijklmnopqrstuvwxyz"},
                    )
                ]
            ),
        )
    )

    result = provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))

    rendered = json.dumps(result.traces[0].spans[0].attributes)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in rendered


# -- error handling (invariant I2) ----------------------------------------


def test_a_backwards_time_range_is_a_client_error() -> None:
    provider = _provider(_search_then_trace({"traces": []}, {}))

    with pytest.raises(ClientError, match="must be after"):
        provider.query_traces(TraceQuery(query="{}", start="now", end="-30m"))


def test_an_unparseable_time_is_a_client_error() -> None:
    provider = _provider(_search_then_trace({"traces": []}, {}))

    with pytest.raises(ClientError, match="cannot parse time"):
        provider.query_traces(TraceQuery(query="{}", start="yesterday", end="now"))


def test_a_4xx_is_a_client_error_and_never_retried() -> None:
    provider = _provider(lambda _r: httpx.Response(400, text="parse error at line 1"))

    with pytest.raises(ClientError, match="rejected the query"):
        provider.query_traces(TraceQuery(query="{", start="-30m", end="now"))


def test_a_5xx_is_a_transport_error_so_the_registry_retries_once() -> None:
    provider = _provider(lambda _r: httpx.Response(503))

    with pytest.raises(TransportError):
        provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))


def test_basic_auth_reaches_the_wire_for_grafana_cloud() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"traces": []})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), auth=httpx.BasicAuth("123456", "glc-token")
    )
    provider = TempoProvider("https://tempo.example", client=client, now=lambda: FIXED_NOW)

    provider.query_traces(TraceQuery(query="{}", start="-30m", end="now"))

    assert seen[0].headers["authorization"].startswith("Basic ")


# -- tool spec ------------------------------------------------------------


def test_tool_spec_is_read_tier_with_the_traceql_argument_name() -> None:
    provider = _provider(_search_then_trace({"traces": []}, {}))

    spec = traces_tool_spec(provider)

    assert spec.name == "query_traces"
    assert spec.tier == "read"
    assert "traceql" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["traceql", "start", "end"]


def test_executor_returns_a_json_shaped_payload() -> None:
    provider = _provider(
        _search_then_trace(
            {"traces": [{"traceID": "abc", "rootTraceName": "GET /", "durationMs": 900}]},
            _trace_body([_span("db.query", 0, 700_000_000)]),
        )
    )

    payload = traces_tool_spec(provider).executor({"traceql": "{}", "start": "-30m", "end": "now"})

    assert json.dumps(payload)  # must be serialisable into the trace
    assert payload["traces"][0]["trace_id"] == "abc"
    assert payload["traces"][0]["spans"][0]["name"] == "db.query"
