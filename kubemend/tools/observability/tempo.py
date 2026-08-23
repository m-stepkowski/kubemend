"""Tempo provider (ARCHITECTURE.md §3.2, tool contract `query_traces`, M13).

TraceQL searches against Tempo's `/api/search`, then one `/api/traces/<id>`
fetch per hit to get the spans. Grafana Cloud's hosted Tempo speaks the same
API as self-hosted, so this reuses the HTTP Basic Auth seam
`PrometheusProvider`/`LokiProvider` gained in M9b — instance ID as username,
Access Policy token as password.

Two searches happen per call by necessity: Tempo's search endpoint returns
trace *metadata* only, never spans. That is why `limit` is clamped hard here —
each result costs a second round trip, so a limit of 100 would mean 101
requests inside one tool call's timeout.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from kubemend.tools.base import ClientError, ToolSpec, TransportError
from kubemend.tools.observability.provider import (
    Span,
    TimeRangeError,
    Trace,
    TraceQuery,
    TraceResult,
    parse_time,
)
from kubemend.tools.redact import redact_text

# Each trace beyond the first costs its own /api/traces round trip, so this cap
# is about wall time inside one tool call, not about payload size.
MAX_LIMIT = 20
# Spans per trace kept in the payload. A single slow request can carry
# hundreds; the slowest handful is what names the culprit.
MAX_SPANS_PER_TRACE = 40

EMPTY_HINT = (
    "no traces matched; check the TraceQL selector, widen the time range, or lower min_duration_ms"
)


class TempoProvider:
    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 25.0,
        auth: httpx.BasicAuth | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout, auth=auth)
        self._now = now or (lambda: datetime.now(UTC))

    def query_traces(self, query: TraceQuery) -> TraceResult:
        reference = self._now()
        try:
            start = parse_time(query.start, now=reference)
            end = parse_time(query.end, now=reference)
        except TimeRangeError as exc:
            raise ClientError(str(exc)) from exc
        if end <= start:
            raise ClientError(f"end ({query.end}) must be after start ({query.start})")

        limit = max(1, min(query.limit, MAX_LIMIT))
        params: dict[str, str | int] = {
            "q": _with_min_duration(query.query, query.min_duration_ms),
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "limit": limit,
        }

        found = self._get("/api/search", params).get("traces", [])
        if not isinstance(found, list) or not found:
            return TraceResult(traces=[], limited=False, hint=EMPTY_HINT)

        traces = [self._fetch(entry) for entry in found[:limit]]
        return TraceResult(traces=traces, limited=len(found) >= limit)

    def _fetch(self, entry: dict[str, Any]) -> Trace:
        """Metadata from search, spans from a follow-up fetch.

        A failed span fetch degrades to metadata-only rather than failing the
        whole call: knowing a slow trace exists is still worth a turn, and one
        unreadable trace should not lose the other nineteen.
        """
        trace_id = str(entry.get("traceID", ""))
        duration_ms = float(entry.get("durationMs", 0) or 0)
        root_name = str(entry.get("rootTraceName", "") or "")
        start_time = str(entry.get("startTimeUnixNano", "") or "")
        try:
            spans = self._spans(trace_id)
        except (ClientError, TransportError):
            spans = []
        return Trace(
            trace_id=trace_id,
            root_name=root_name,
            duration_ms=duration_ms,
            span_count=len(spans),
            start_time=start_time,
            spans=spans[:MAX_SPANS_PER_TRACE],
        )

    def _spans(self, trace_id: str) -> list[Span]:
        payload = self._get(f"/api/traces/{trace_id}", {})
        batches = payload.get("batches", [])
        if not isinstance(batches, list):
            return []
        spans: list[Span] = []
        for batch in batches:
            service = _service_name(batch)
            scopes = batch.get("scopeSpans", []) or batch.get("instrumentationLibrarySpans", [])
            for scope in scopes:
                for raw in scope.get("spans", []):
                    spans.append(_to_span(raw, service))
        # Slowest first: the question tracing answers during an incident is
        # "what took the time", and the cap below keeps only the top of this.
        spans.sort(key=lambda s: s.duration_ms, reverse=True)
        return spans

    def _get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        try:
            response = self._client.get(f"{self.base_url}{path}", params=params)
        except httpx.TimeoutException as exc:
            raise TransportError(f"tempo timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"tempo unreachable: {exc}") from exc

        if response.status_code >= 500:
            raise TransportError(f"tempo returned {response.status_code}")
        if response.status_code >= 400:
            raise ClientError(f"tempo rejected the query: {response.text[:200]}")
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise ClientError("tempo returned an unexpected body")
        return parsed


def _with_min_duration(traceql: str, min_duration_ms: float | None) -> str:
    """Express a duration floor as TraceQL, because the API's own parameter
    does not apply to TraceQL searches.

    Tempo's `minDuration` query parameter belongs to the older tag-based
    search. When `q` carries TraceQL it is **silently ignored** — a 5s floor
    still returned a 900ms trace against a real Tempo, which the unit tests
    could never have caught: they asserted the parameter was *sent*, not that
    it was honoured.

    Composed as a second spanset (`{...} && {duration > Nms}`) rather than
    edited into the caller's braces: that needs no parsing of a query the
    model wrote, and works for `{}` as readily as for a filled selector.
    """
    if min_duration_ms is None:
        return traceql
    return f"{traceql.strip()} && {{duration > {int(min_duration_ms)}ms}}"


def _service_name(batch: dict[str, Any]) -> str:
    resource = batch.get("resource", {})
    for attribute in resource.get("attributes", []):
        if attribute.get("key") == "service.name":
            return _attr_value(attribute.get("value", {}))
    return ""


def _attr_value(value: dict[str, Any]) -> str:
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return str(value[key])
    return ""


def _to_span(raw: dict[str, Any], service: str) -> Span:
    start = int(raw.get("startTimeUnixNano", 0) or 0)
    end = int(raw.get("endTimeUnixNano", 0) or 0)
    status = raw.get("status", {}) or {}
    # Span attributes are user-controlled strings that reach the model, same as
    # log lines — redaction is the executor's invariant (I3), and this is the
    # same defence in depth loki.py applies to its own payload.
    attributes = {
        str(a.get("key", "")): redact_text(_attr_value(a.get("value", {})))
        for a in (raw.get("attributes", []) or [])
    }
    return Span(
        name=redact_text(str(raw.get("name", ""))),
        service=service,
        duration_ms=max(0.0, (end - start) / 1e6),
        status=str(status.get("code", "") or ""),
        attributes=attributes,
    )


def traces_tool_spec(provider: TempoProvider) -> ToolSpec:
    """`query_traces` as the model sees it (docs/knowledge/tool-contracts.md)."""

    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        raw_min = args.get("min_duration_ms")
        result = provider.query_traces(
            TraceQuery(
                query=str(args["traceql"]),
                start=str(args["start"]),
                end=str(args["end"]),
                min_duration_ms=float(raw_min) if raw_min is not None else None,
                limit=int(args.get("limit", 10)),
            )
        )
        payload: dict[str, Any] = {
            "traces": [
                {
                    "trace_id": t.trace_id,
                    "root_name": t.root_name,
                    "duration_ms": t.duration_ms,
                    "span_count": t.span_count,
                    "spans": [
                        {
                            "name": s.name,
                            "service": s.service,
                            "duration_ms": s.duration_ms,
                            "status": s.status,
                            "attributes": s.attributes,
                        }
                        for s in t.spans
                    ],
                }
                for t in result.traces
            ],
            "limited": result.limited,
        }
        if result.hint:
            payload["hint"] = result.hint
        return payload

    return ToolSpec(
        name="query_traces",
        description=(
            "Search distributed traces with TraceQL, e.g. "
            '{resource.service.name="shop-api"} or {status=error}. Use this to find '
            "which downstream call is slow or failing when metrics show latency but "
            "logs do not say why. Spans come back slowest-first, not as a tree. "
            "min_duration_ms is the usual way to find the pathological requests."
        ),
        parameters={
            "type": "object",
            "properties": {
                "traceql": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "min_duration_ms": {"type": "number"},
                "limit": {"type": "integer", "maximum": MAX_LIMIT, "default": 10},
            },
            "required": ["traceql", "start", "end"],
        },
        executor=_execute,
        tier="read",
        timeout_s=30.0,
    )
