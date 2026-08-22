"""Datadog provider (M9, ARCHITECTURE.md §3.2, docs/knowledge/tool-contracts.md).

Driven through `httpx.MockTransport`, same style as test_observability.py — no
lab Datadog instance exists, so the shaping decisions (timeseries zip, gap
handling, tag-set grouping, error classification) are what's under test here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from kubemend.tools.base import ClientError, TransportError
from kubemend.tools.observability.datadog import MAX_LIMIT, DatadogProvider
from kubemend.tools.observability.provider import LogQuery, MetricQuery

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
