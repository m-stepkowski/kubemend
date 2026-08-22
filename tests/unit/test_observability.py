"""Prometheus and Loki providers (ARCHITECTURE.md §3.2).

Driven through `httpx.MockTransport` rather than a live backend: the shaping
decisions (downsampling, limit clamping, empty-is-a-hint, error classification)
are where the bugs live, and none of them need a cluster to exercise.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from kubemend.tools.base import ClientError, TransportError
from kubemend.tools.observability.loki import MAX_LIMIT, LokiProvider
from kubemend.tools.observability.prometheus import PrometheusProvider, auto_step_seconds
from kubemend.tools.observability.provider import (
    LogQuery,
    MetricQuery,
    TimeRangeError,
    downsample,
    parse_time,
)

FIXED_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _prometheus(handler: Handler) -> PrometheusProvider:
    return PrometheusProvider("http://prom:9090", client=_client(handler), now=lambda: FIXED_NOW)


def _loki(handler: Handler) -> LokiProvider:
    return LokiProvider("http://loki:3100", client=_client(handler), now=lambda: FIXED_NOW)


# -- time parsing ---------------------------------------------------------


def test_parse_time_handles_relative_absolute_and_now() -> None:
    assert parse_time("now", now=FIXED_NOW) == FIXED_NOW
    assert parse_time("-30m", now=FIXED_NOW) == datetime(2026, 8, 12, 11, 30, tzinfo=UTC)
    assert parse_time("-2h", now=FIXED_NOW) == datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    assert parse_time("2026-08-12T09:00:00Z", now=FIXED_NOW) == datetime(
        2026, 8, 12, 9, 0, tzinfo=UTC
    )


def test_parse_time_rejects_nonsense_with_a_usable_message() -> None:
    with pytest.raises(TimeRangeError, match="relative offset"):
        parse_time("last tuesday", now=FIXED_NOW)


# -- downsampling ---------------------------------------------------------


def test_downsample_caps_points_and_always_keeps_the_last() -> None:
    points = [(float(i), float(i)) for i in range(1000)]

    kept, reduced = downsample(points, 100)

    assert reduced is True
    assert len(kept) <= 101, "stride plus the forced final sample"
    assert kept[0] == (0.0, 0.0)
    assert kept[-1] == (999.0, 999.0), "the newest sample says whether it is still happening"


def test_downsample_leaves_small_series_alone() -> None:
    points = [(1.0, 1.0), (2.0, 2.0)]
    assert downsample(points, 100) == (points, False)


def test_auto_step_targets_the_point_budget() -> None:
    start, end = datetime(2026, 8, 12, 6, tzinfo=UTC), datetime(2026, 8, 12, 12, tzinfo=UTC)
    assert auto_step_seconds(start, end, 100) == 216  # 6h / 100


# -- prometheus -----------------------------------------------------------


def test_query_metrics_shapes_series_and_records_resolution() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"pod": "shop-api-1", "namespace": "shop"},
                            "values": [[1.0, "0.5"], [2.0, "0.7"]],
                        }
                    ],
                },
            },
        )

    result = _prometheus(handler).query_metrics(
        MetricQuery(query="rate(http_errors[5m])", start="-30m", end="now")
    )

    assert captured["query"] == "rate(http_errors[5m])"
    assert captured["step"] == "18s", "auto step derived from a 30m window and 100 points"
    assert len(result.series) == 1
    assert result.series[0].labels == {"pod": "shop-api-1", "namespace": "shop"}
    assert result.series[0].points == [(1.0, 0.5), (2.0, 0.7)]
    assert result.resolution_note is not None


def test_empty_metric_result_is_a_hint_not_an_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    result = _prometheus(handler).query_metrics(MetricQuery("up", "-5m", "now"))

    assert result.series == []
    assert result.hint is not None and "label selectors" in result.hint


def test_explicit_step_overrides_the_auto_step() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    _prometheus(handler).query_metrics(MetricQuery("up", "-1h", "now", step="1m"))

    assert captured["step"] == "1m"


def test_prometheus_5xx_is_transport_class_and_4xx_is_client_class() -> None:
    """The split decides whether the registry retries (I2)."""
    with pytest.raises(TransportError):
        _prometheus(lambda _r: httpx.Response(503)).query_metrics(MetricQuery("up", "-5m", "now"))

    with pytest.raises(ClientError, match="rejected"):
        _prometheus(
            lambda _r: httpx.Response(400, json={"error": "parse error at char 3"})
        ).query_metrics(MetricQuery("up{", "-5m", "now"))


def test_inverted_time_range_is_rejected_before_the_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the server")

    with pytest.raises(ClientError, match="must be after"):
        _prometheus(handler).query_metrics(MetricQuery("up", "now", "-30m"))


# -- loki -----------------------------------------------------------------


def test_search_logs_clamps_the_limit_server_side() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"data": {"result": []}})

    _loki(handler).search_logs(LogQuery('{app="shop-api"}', "-15m", "now", limit=100_000))

    assert int(captured["limit"]) == MAX_LIMIT


def test_search_logs_redacts_every_line() -> None:
    """Logs are the most likely place for a credential to surface."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "result": [
                        {
                            "stream": {"pod": "shop-api-1"},
                            "values": [
                                [
                                    "1",
                                    "upstream call, Authorization: Bearer sk-abc123XYZ789",
                                ],
                                ["2", "dsn=postgres://app:hunter2@db:5432/shop"],
                            ],
                        }
                    ]
                }
            },
        )

    result = _loki(handler).search_logs(LogQuery('{app="shop-api"}', "-15m", "now"))

    rendered = json.dumps([list(line) for line in result.streams[0].lines])
    assert "sk-abc123XYZ789" not in rendered
    assert "hunter2" not in rendered
    assert "<redacted:bearer_token>" in rendered
    assert "<redacted:connection_password>" in rendered
    assert "postgres://app:" in rendered, "the non-secret part stays diagnostic"


def test_search_logs_flags_when_the_limit_was_hit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        values = [[str(i), f"line {i}"] for i in range(5)]
        return httpx.Response(200, json={"data": {"result": [{"stream": {}, "values": values}]}})

    result = _loki(handler).search_logs(LogQuery("{}", "-5m", "now", limit=5))

    assert result.total_lines == 5
    assert result.limited is True, "the model needs to know it is seeing a truncated window"


def test_empty_log_result_is_a_hint_not_an_error() -> None:
    result = _loki(lambda _r: httpx.Response(200, json={"data": {"result": []}})).search_logs(
        LogQuery("{}", "-5m", "now")
    )

    assert result.streams == []
    assert result.hint is not None
