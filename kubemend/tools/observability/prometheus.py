"""Prometheus provider (ARCHITECTURE.md §3.2, tool contract `query_metrics`).

Range queries against `/api/v1/query_range`, downsampled by stride to at most
`max_points` per series. An empty result is a hint, not an error — the model
gets told no series matched so it can fix its selector.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from kubemend.tools.base import ClientError, ToolSpec, TransportError
from kubemend.tools.observability.provider import (
    MetricQuery,
    MetricResult,
    MetricSeries,
    TimeRangeError,
    parse_time,
)

EMPTY_HINT = "no series matched; check label selectors and the time range"


def downsample(
    points: list[tuple[float, float]], max_points: int
) -> tuple[list[tuple[float, float]], bool]:
    """Keep at most `max_points` samples by striding, always keeping the last.

    The final sample is what says whether the problem is still happening, so it
    survives even when the stride would otherwise drop it.
    """
    if max_points <= 0 or len(points) <= max_points:
        return points, False
    stride = math.ceil(len(points) / max_points)
    reduced = points[::stride]
    if reduced[-1] != points[-1]:
        reduced.append(points[-1])
    return reduced, True


def auto_step_seconds(start: datetime, end: datetime, max_points: int) -> int:
    """Pick a step that lands near `max_points` samples.

    Prometheus requires a step, and asking for a 1s step over a 6h window is
    how a query returns 21,600 points that then get thrown away — expensive on
    both sides.
    """
    span = max(1.0, (end - start).total_seconds())
    return max(1, math.ceil(span / max(1, max_points)))


class PrometheusProvider:
    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._now = now or (lambda: datetime.now(UTC))

    def query_metrics(self, query: MetricQuery) -> MetricResult:
        reference = self._now()
        try:
            start = parse_time(query.start, now=reference)
            end = parse_time(query.end, now=reference)
        except TimeRangeError as exc:
            raise ClientError(str(exc)) from exc
        if end <= start:
            raise ClientError(f"end ({query.end}) must be after start ({query.start})")

        step = query.step or f"{auto_step_seconds(start, end, query.max_points)}s"
        payload = self._get(
            "/api/v1/query_range",
            {
                "query": query.query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
        )
        return self._to_result(payload, step=step, max_points=query.max_points)

    def _get(self, path: str, params: dict[str, str | float]) -> dict[str, object]:
        try:
            response = self._client.get(f"{self.base_url}{path}", params=params)
        except httpx.TimeoutException as exc:
            raise TransportError(f"prometheus timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"prometheus unreachable: {exc}") from exc

        if response.status_code >= 500:
            raise TransportError(f"prometheus returned {response.status_code}")
        if response.status_code >= 400:
            # A 4xx is almost always a malformed PromQL expression. Surfacing the
            # server's own message is what lets the model fix its query.
            raise ClientError(f"prometheus rejected the query: {_error_detail(response)}")
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise ClientError("prometheus returned an unexpected body")
        return parsed

    def _to_result(self, payload: dict[str, object], *, step: str, max_points: int) -> MetricResult:
        if payload.get("status") != "success":
            raise ClientError(f"prometheus error: {payload.get('error', 'unknown')}")

        data = payload.get("data")
        raw = data.get("result", []) if isinstance(data, dict) else []
        series: list[MetricSeries] = []
        downsampled = False
        for entry in raw:
            points = [(float(ts), float(value)) for ts, value in entry.get("values", [])]
            kept, reduced = downsample(points, max_points)
            downsampled = downsampled or reduced
            series.append(MetricSeries(labels=dict(entry.get("metric", {})), points=kept))

        if not series:
            return MetricResult(series=[], hint=EMPTY_HINT)
        note = f"step={step}"
        if downsampled:
            note += f"; downsampled to <={max_points} points per series"
        return MetricResult(series=series, resolution_note=note)


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("error", body)) if isinstance(body, dict) else str(body)[:200]


def metrics_tool_spec(provider: PrometheusProvider) -> ToolSpec:
    """`query_metrics` as the model sees it (docs/knowledge/tool-contracts.md).

    The schema is a contract: the description below is what the model is
    steered by, and changing it means updating the doc and the schema test in
    the same PR.
    """

    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        result = provider.query_metrics(
            MetricQuery(
                query=str(args["promql"]),
                start=str(args["start"]),
                end=str(args["end"]),
                step=str(args["step"]) if args.get("step") else None,
            )
        )
        payload: dict[str, Any] = {
            "series": [
                {"labels": s.labels, "points": [list(p) for p in s.points]} for s in result.series
            ]
        }
        if result.resolution_note:
            payload["resolution_note"] = result.resolution_note
        if result.hint:
            payload["hint"] = result.hint
        return payload

    return ToolSpec(
        name="query_metrics",
        description=(
            "Run a PromQL range query against the cluster's Prometheus. Prefer "
            "rate()/increase() over raw counters. Narrow by namespace/pod labels; "
            "wide queries will be truncated."
        ),
        parameters={
            "type": "object",
            "properties": {
                "promql": {"type": "string"},
                "start": {"type": "string", "description": "RFC3339 or relative like -30m"},
                "end": {"type": "string", "description": "RFC3339 or 'now'"},
                "step": {"type": "string", "description": "e.g. 30s, 1m; default auto"},
            },
            "required": ["promql", "start", "end"],
        },
        executor=_execute,
        tier="read",
        timeout_s=20.0,
    )
