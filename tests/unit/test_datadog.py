"""Datadog provider (M9, ARCHITECTURE.md §3.2, docs/knowledge/tool-contracts.md).

Driven through `httpx.MockTransport`, same style as test_observability.py — no
lab Datadog instance exists, so the shaping decisions (timeseries zip, gap
handling, tag-set grouping, error classification) are what's under test here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from kubemend.tools.base import ClientError, TransportError
from kubemend.tools.observability.datadog import (
    MAX_LIMIT,
    SPANS_PER_TRACE_ESTIMATE,
    DatadogProvider,
)
from kubemend.tools.observability.datadog import traces_tool_spec as datadog_traces_tool_spec
from kubemend.tools.observability.provider import LogQuery, MetricQuery, TraceQuery

FIXED_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _datadog(handler: Handler) -> DatadogProvider:
    return DatadogProvider(
        site="datadoghq.com",
        api_key="test-api-key",
        app_key="test-app-key",
        client=_client(handler),
        now=lambda: FIXED_NOW,
    )


def _empty_timeseries_response() -> httpx.Response:
    return httpx.Response(
        200, json={"data": {"attributes": {"times": [], "series": [], "values": []}}}
    )


# -- metrics ----------------------------------------------------------------


def test_query_metrics_zips_times_and_values_per_series() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {
                    "attributes": {
                        "times": [1000, 2000, 3000],
                        "series": [{"group_tags": ["pod:shop-api-1", "namespace:shop"]}],
                        "values": [[0.5, 0.7, 0.9]],
                    }
                }
            },
        )

    result = _datadog(handler).query_metrics(
        MetricQuery(query="avg:kubernetes.cpu.usage.total{*}", start="-30m", end="now")
    )

    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["dd-api-key"] == "test-api-key"
    assert headers["dd-application-key"] == "test-app-key"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["data"]["attributes"]["queries"][0]["query"] == "avg:kubernetes.cpu.usage.total{*}"

    assert len(result.series) == 1
    assert result.series[0].labels == {"pod": "shop-api-1", "namespace": "shop"}
    assert result.series[0].points == [(1.0, 0.5), (2.0, 0.7), (3.0, 0.9)]


def test_query_metrics_drops_gap_points() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "attributes": {
                        "times": [1000, 2000, 3000],
                        "series": [{"group_tags": []}],
                        "values": [[0.5, None, 0.9]],
                    }
                }
            },
        )

    result = _datadog(handler).query_metrics(MetricQuery(query="up", start="-5m", end="now"))

    assert result.series[0].points == [(1.0, 0.5), (3.0, 0.9)], "gaps are dropped, not kept as 0"


def test_empty_metric_result_is_a_hint_not_an_error() -> None:
    result = _datadog(lambda _r: _empty_timeseries_response()).query_metrics(
        MetricQuery("up", "-5m", "now")
    )

    assert result.series == []
    assert result.hint is not None


def test_malformed_metric_response_is_a_client_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"attributes": {"times": [1000], "series": "not-a-list"}}}
        )

    with pytest.raises(ClientError, match="malformed"):
        _datadog(handler).query_metrics(MetricQuery("up", "-5m", "now"))


def test_datadog_5xx_is_transport_class_and_4xx_is_client_class() -> None:
    with pytest.raises(TransportError):
        _datadog(lambda _r: httpx.Response(503)).query_metrics(MetricQuery("up", "-5m", "now"))

    with pytest.raises(ClientError, match="rejected"):
        _datadog(
            lambda _r: httpx.Response(400, json={"errors": ["query parameter is required"]})
        ).query_metrics(MetricQuery("up{", "-5m", "now"))


def test_inverted_time_range_is_rejected_before_the_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the server")

    with pytest.raises(ClientError, match="must be after"):
        _datadog(handler).query_metrics(MetricQuery("up", "now", "-30m"))


def test_step_maps_to_interval_ms() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _empty_timeseries_response()

    _datadog(handler).query_metrics(MetricQuery("up", "-30m", "now", step="1m"))

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["data"]["attributes"]["interval"] == 60_000


def test_omitted_step_leaves_interval_out_of_the_request() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _empty_timeseries_response()

    _datadog(handler).query_metrics(MetricQuery("up", "-30m", "now"))

    body = captured["body"]
    assert isinstance(body, dict)
    assert "interval" not in body["data"]["attributes"]


# -- logs ---------------------------------------------------------------------


def test_search_logs_groups_flat_events_by_tag_set() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "timestamp": "2026-08-12T11:59:00Z",
                            "message": "starting up",
                            "tags": ["pod:shop-api-1", "namespace:shop"],
                        }
                    },
                    {
                        "attributes": {
                            "timestamp": "2026-08-12T11:59:05Z",
                            "message": "ready",
                            "tags": ["namespace:shop", "pod:shop-api-1"],
                        }
                    },
                    {
                        "attributes": {
                            "timestamp": "2026-08-12T11:59:10Z",
                            "message": "starting up",
                            "tags": ["pod:shop-api-2", "namespace:shop"],
                        }
                    },
                ]
            },
        )

    result = _datadog(handler).search_logs(LogQuery("service:shop-api", "-15m", "now"))

    assert len(result.streams) == 2, "two distinct tag sets, tag order within a set doesn't matter"
    assert result.total_lines == 3
    by_pod = {s.labels["pod"]: s for s in result.streams}
    assert len(by_pod["shop-api-1"].lines) == 2
    assert len(by_pod["shop-api-2"].lines) == 1


def test_search_logs_redacts_every_message() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "timestamp": "2026-08-12T11:59:00Z",
                            "message": "upstream call, Authorization: Bearer sk-abc123XYZ789",
                            "tags": ["pod:shop-api-1"],
                        }
                    }
                ]
            },
        )

    result = _datadog(handler).search_logs(LogQuery("service:shop-api", "-15m", "now"))

    rendered = json.dumps([list(line) for line in result.streams[0].lines])
    assert "sk-abc123XYZ789" not in rendered
    assert "<redacted:bearer_token>" in rendered


def test_search_logs_clamps_the_limit_server_side() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": []})

    _datadog(handler).search_logs(LogQuery("*", "-15m", "now", limit=100_000))

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["page"]["limit"] == MAX_LIMIT


def test_search_logs_flags_when_a_next_page_exists() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "timestamp": "2026-08-12T11:59:00Z",
                            "message": "line",
                            "tags": [],
                        }
                    }
                ],
                "meta": {"page": {"after": "some-cursor"}},
            },
        )

    result = _datadog(handler).search_logs(LogQuery("*", "-15m", "now"))

    assert result.limited is True


def test_search_logs_direction_controls_sort() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": []})

    _datadog(handler).search_logs(LogQuery("*", "-15m", "now", direction="forward"))
    assert captured["body"]["sort"] == "timestamp"  # type: ignore[index]

    _datadog(handler).search_logs(LogQuery("*", "-15m", "now", direction="backward"))
    assert captured["body"]["sort"] == "-timestamp"  # type: ignore[index]


def test_empty_log_result_is_a_hint_not_an_error() -> None:
    result = _datadog(lambda _r: httpx.Response(200, json={"data": []})).search_logs(
        LogQuery("*", "-15m", "now")
    )

    assert result.streams == []
    assert result.hint is not None


# -- traces (M13) ---------------------------------------------------------


def _span_event(
    trace_id: str,
    resource: str,
    duration_ns: int,
    *,
    parent_id: str | None = None,
    service: str = "shop-api",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "trace_id": trace_id,
        "resource_name": resource,
        "duration": duration_ns,
        "service": service,
        "tags": tags or [],
    }
    if parent_id is not None:
        attributes["parent_id"] = parent_id
    return {"attributes": attributes}


def test_flat_spans_are_regrouped_into_traces() -> None:
    """Datadog returns spans, not traces — unlike Tempo, grouping is this
    provider's job."""
    provider = _datadog(
        lambda _r: httpx.Response(
            200,
            json={
                "data": [
                    _span_event("t1", "GET /checkout", 900_000_000),
                    _span_event("t1", "db.query", 700_000_000, parent_id="a"),
                    _span_event("t2", "GET /cart", 120_000_000),
                ]
            },
        )
    )

    result = provider.query_traces(TraceQuery(query="service:shop-api", start="-30m", end="now"))

    assert [t.trace_id for t in result.traces] == ["t1", "t2"], "slowest trace first"
    assert result.traces[0].span_count == 2
    assert result.traces[0].duration_ms == pytest.approx(900.0)


def test_the_parentless_span_names_the_trace_even_when_it_is_not_the_longest() -> None:
    provider = _datadog(
        lambda _r: httpx.Response(
            200,
            json={
                "data": [
                    _span_event("t1", "slow-child", 900_000_000, parent_id="root"),
                    _span_event("t1", "GET /checkout", 50_000_000),
                ]
            },
        )
    )

    result = provider.query_traces(TraceQuery(query="", start="-30m", end="now"))

    assert result.traces[0].root_name == "GET /checkout"


def test_a_trace_sliced_across_pages_falls_back_to_its_longest_span() -> None:
    """A page can begin mid-trace, so no parentless span comes back. For "how
    long did this take" the longest span is the same answer."""
    provider = _datadog(
        lambda _r: httpx.Response(
            200,
            json={
                "data": [
                    _span_event("t1", "child-a", 300_000_000, parent_id="root"),
                    _span_event("t1", "child-b", 700_000_000, parent_id="root"),
                ]
            },
        )
    )

    result = provider.query_traces(TraceQuery(query="", start="-30m", end="now"))

    assert result.traces[0].root_name == "child-b"
    assert result.traces[0].duration_ms == pytest.approx(700.0)


def test_min_duration_becomes_a_duration_facet_term_not_a_parameter() -> None:
    """Datadog has no dedicated min-duration field; it lives in the query."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": []})

    _datadog(handler).query_traces(
        TraceQuery(query="service:shop-api", start="-30m", end="now", min_duration_ms=250)
    )

    query = seen[0]["data"]["attributes"]["filter"]["query"]
    assert "@duration:>250ms" in query
    assert "service:shop-api" in query


def test_the_span_page_over_fetches_because_limit_counts_traces() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": []})

    _datadog(handler).query_traces(TraceQuery(query="", start="-30m", end="now", limit=3))

    assert seen[0]["data"]["attributes"]["page"]["limit"] == 3 * SPANS_PER_TRACE_ESTIMATE


def test_more_traces_than_asked_for_sets_limited() -> None:
    provider = _datadog(
        lambda _r: httpx.Response(
            200,
            json={
                "data": [_span_event(f"t{i}", f"GET /{i}", (i + 1) * 1_000_000) for i in range(5)]
            },
        )
    )

    result = provider.query_traces(TraceQuery(query="", start="-30m", end="now", limit=2))

    assert len(result.traces) == 2
    assert result.limited is True


def test_no_spans_returns_an_actionable_hint() -> None:
    provider = _datadog(lambda _r: httpx.Response(200, json={"data": []}))

    result = provider.query_traces(TraceQuery(query="", start="-30m", end="now"))

    assert result.traces == []
    assert result.hint is not None and "min_duration_ms" in result.hint


def test_a_malformed_span_response_is_a_client_error_not_a_crash() -> None:
    provider = _datadog(lambda _r: httpx.Response(200, json={"data": ["not-an-object"]}))

    with pytest.raises(ClientError, match="malformed span-search"):
        provider.query_traces(TraceQuery(query="", start="-30m", end="now"))


def test_traces_tool_spec_uses_span_query_not_traceql() -> None:
    """Same tool name as Tempo's so the loop stays backend-agnostic, but a
    distinct argument name: sending TraceQL to Datadog would silently fail."""
    spec = datadog_traces_tool_spec(_datadog(lambda _r: httpx.Response(200, json={"data": []})))

    assert spec.name == "query_traces"
    assert "span_query" in spec.parameters["properties"]
    assert "traceql" not in spec.parameters["properties"]
