"""Datadog provider (ARCHITECTURE.md §3.2, M9, tool contracts `query_metrics`/`search_logs`).

A second ObservabilityProvider, on raw httpx rather than the vendor SDK (no
agent-framework-adjacent dependency creep — CLAUDE.md hard rule 1). Datadog's
v2 timeseries and logs-search APIs are shaped very differently from
Prometheus/Loki: metrics come back as a shared time axis zipped against
per-series values (with `None` gaps), and logs come back as a flat,
ungrouped list of events rather than pre-grouped streams — both are
reshaped here into the same provider-neutral MetricResult/LogResult types
prometheus.py/loki.py already produce, so the tool layer and the loop never
learn anything Datadog-specific.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from kubemend.tools.base import ClientError, ToolSpec, TransportError
from kubemend.tools.observability.provider import (
    LogQuery,
    LogResult,
    LogStream,
    MetricQuery,
    MetricResult,
    MetricSeries,
    TimeRangeError,
    downsample,
    parse_time,
)
from kubemend.tools.redact import redact_text

MAX_LIMIT = 500

EMPTY_METRIC_HINT = "no series matched; check the query and time range"
EMPTY_LOG_HINT = "no log lines matched; check the query and time range"

_STEP = re.compile(r"^(?P<amount>\d+)(?P<unit>s|m|h)$")
_STEP_SECONDS = {"s": 1, "m": 60, "h": 3600}


def _interval_ms(step: str) -> int:
    match = _STEP.match(step.strip())
    if match is None:
        raise ClientError(f"cannot parse step '{step}'; use e.g. 30s, 1m, 1h")
    return int(match.group("amount")) * _STEP_SECONDS[match.group("unit")] * 1000


def _labels_from_tags(tags: list[object] | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for tag in tags or []:
        key, _, value = str(tag).partition(":")
        labels[key] = value
    return labels


class DatadogProvider:
    def __init__(
        self,
        *,
        site: str,
        api_key: str,
        app_key: str,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = f"https://api.{site}"
        self._api_key = api_key
        self._app_key = app_key
        self._client = client or httpx.Client(timeout=timeout)
        self._now = now or (lambda: datetime.now(UTC))

    # -- metrics ------------------------------------------------------------

    def query_metrics(self, query: MetricQuery) -> MetricResult:
        reference = self._now()
        try:
            start = parse_time(query.start, now=reference)
            end = parse_time(query.end, now=reference)
        except TimeRangeError as exc:
            raise ClientError(str(exc)) from exc
        if end <= start:
            raise ClientError(f"end ({query.end}) must be after start ({query.start})")

        attributes: dict[str, Any] = {
            "formulas": [{"formula": "a"}],
            "queries": [{"data_source": "metrics", "query": query.query, "name": "a"}],
            "from": int(start.timestamp() * 1000),
            "to": int(end.timestamp() * 1000),
        }
        if query.step:
            attributes["interval"] = _interval_ms(query.step)

        payload = self._post(
            "/api/v2/query/timeseries",
            {"data": {"type": "timeseries_request", "attributes": attributes}},
        )
        return self._to_metric_result(payload, max_points=query.max_points)

    def _to_metric_result(self, payload: dict[str, object], *, max_points: int) -> MetricResult:
        data = payload.get("data")
        attrs = data.get("attributes", {}) if isinstance(data, dict) else {}
        try:
            times = list(attrs.get("times", []))
            series_meta = list(attrs.get("series", []))
            values = list(attrs.get("values", []))
            series: list[MetricSeries] = []
            downsampled = False
            for meta, series_values in zip(series_meta, values, strict=True):
                points = [
                    (float(ts) / 1000.0, float(value))
                    for ts, value in zip(times, series_values, strict=True)
                    if value is not None
                ]
                kept, reduced = downsample(points, max_points)
                downsampled = downsampled or reduced
                labels = _labels_from_tags(meta.get("group_tags"))
                series.append(MetricSeries(labels=labels, points=kept))
        except (KeyError, TypeError, ValueError) as exc:
            raise ClientError("datadog returned a malformed timeseries response") from exc

        if not series:
            return MetricResult(series=[], hint=EMPTY_METRIC_HINT)
        note = "datadog rollup"
        if downsampled:
            note += f"; downsampled to <={max_points} points per series"
        return MetricResult(series=series, resolution_note=note)

    # -- logs -----------------------------------------------------------------

    def search_logs(self, query: LogQuery) -> LogResult:
        reference = self._now()
        try:
            start = parse_time(query.start, now=reference)
            end = parse_time(query.end, now=reference)
        except TimeRangeError as exc:
            raise ClientError(str(exc)) from exc
        if end <= start:
            raise ClientError(f"end ({query.end}) must be after start ({query.start})")

        limit = max(1, min(query.limit, MAX_LIMIT))
        payload = self._post(
            "/api/v2/logs/events/search",
            {
                "filter": {"query": query.query, "from": start.isoformat(), "to": end.isoformat()},
                "sort": "timestamp" if query.direction == "forward" else "-timestamp",
                "page": {"limit": limit},
            },
        )
        return self._to_log_result(payload)

    def _to_log_result(self, payload: dict[str, object]) -> LogResult:
        raw_events = payload.get("data")
        events = raw_events if isinstance(raw_events, list) else []
        groups: dict[tuple[str, ...], list[tuple[str, str]]] = {}
        try:
            for event in events:
                attrs = event.get("attributes", {})
                ts = str(attrs.get("timestamp", ""))
                message = redact_text(str(attrs.get("message", "")))
                tags = tuple(sorted(str(tag) for tag in attrs.get("tags", []) or []))
                groups.setdefault(tags, []).append((ts, message))
        except (AttributeError, TypeError) as exc:
            raise ClientError("datadog returned a malformed log-search response") from exc

        streams = [
            LogStream(labels=_labels_from_tags(list(tags)), lines=lines)
            for tags, lines in groups.items()
        ]
        if not streams:
            return LogResult(streams=[], total_lines=0, limited=False, hint=EMPTY_LOG_HINT)

        total = sum(len(stream.lines) for stream in streams)
        meta = payload.get("meta")
        page = meta.get("page", {}) if isinstance(meta, dict) else {}
        limited = bool(page.get("after")) if isinstance(page, dict) else False
        return LogResult(streams=streams, total_lines=total, limited=limited)

    # -- transport ------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, object]:
        try:
            response = self._client.post(
                f"{self.base_url}{path}",
                json=body,
                headers={"DD-API-KEY": self._api_key, "DD-APPLICATION-KEY": self._app_key},
            )
        except httpx.TimeoutException as exc:
            raise TransportError(f"datadog timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"datadog unreachable: {exc}") from exc

        if response.status_code >= 500:
            raise TransportError(f"datadog returned {response.status_code}")
        if response.status_code >= 400:
            raise ClientError(f"datadog rejected the request: {_error_detail(response)}")
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise ClientError("datadog returned an unexpected body")
        return parsed


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        return str(body)[:200]
    return str(body)[:200]


def metrics_tool_spec(provider: DatadogProvider) -> ToolSpec:
    """`query_metrics` as the model sees it, Datadog-flavored (docs/knowledge/tool-contracts.md).

    The schema is a contract: changing it means updating the doc and the
    schema test in the same PR.
    """

    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        result = provider.query_metrics(
            MetricQuery(
                query=str(args["metric_query"]),
                start=str(args["start"]),
                end=str(args["end"]),
                step=str(args["step"]) if args.get("step") else None,
            )
        )
        out: dict[str, Any] = {
            "series": [
                {"labels": s.labels, "points": [list(p) for p in s.points]} for s in result.series
            ]
        }
        if result.resolution_note:
            out["resolution_note"] = result.resolution_note
        if result.hint:
            out["hint"] = result.hint
        return out

    return ToolSpec(
        name="query_metrics",
        description=(
            "Run a Datadog metric query against the cluster's Datadog integration, e.g. "
            "avg:kubernetes.cpu.usage.total{pod_name:shop-api-*}. Narrow by tags; wide "
            "queries will be truncated."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric_query": {"type": "string"},
                "start": {"type": "string", "description": "RFC3339 or relative like -30m"},
                "end": {"type": "string", "description": "RFC3339 or 'now'"},
                "step": {
                    "type": "string",
                    "description": "e.g. 30s, 1m; default Datadog's own rollup",
                },
            },
            "required": ["metric_query", "start", "end"],
        },
        executor=_execute,
        tier="read",
        timeout_s=20.0,
    )


def logs_tool_spec(provider: DatadogProvider) -> ToolSpec:
    """`search_logs` as the model sees it, Datadog-flavored (docs/knowledge/tool-contracts.md)."""

    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        result = provider.search_logs(
            LogQuery(
                query=str(args["log_query"]),
                start=str(args["start"]),
                end=str(args["end"]),
                limit=int(args.get("limit", 200)),
                direction="forward" if args.get("direction") == "forward" else "backward",
            )
        )
        out: dict[str, Any] = {
            "streams": [
                {"labels": s.labels, "lines": [list(line) for line in s.lines]}
                for s in result.streams
            ],
            "total_lines": result.total_lines,
            "limited": result.limited,
        }
        if result.hint:
            out["hint"] = result.hint
        return out

    return ToolSpec(
        name="search_logs",
        description=(
            "Search logs via Datadog's log search syntax, e.g. "
            "'service:shop-api status:error'. Results over the limit are cut "
            "server-side; narrow the time range or add filters."
        ),
        parameters={
            "type": "object",
            "properties": {
                "log_query": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "limit": {"type": "integer", "maximum": 500, "default": 200},
                "direction": {
                    "type": "string",
                    "enum": ["backward", "forward"],
                    "default": "backward",
                },
            },
            "required": ["log_query", "start", "end"],
        },
        executor=_execute,
        tier="read",
        timeout_s=20.0,
    )
