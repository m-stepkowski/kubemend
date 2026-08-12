"""Loki provider (ARCHITECTURE.md §3.2, tool contract `search_logs`).

LogQL range queries against `/loki/api/v1/query_range` with the line limit
enforced executor-side. Logs are simultaneously the most likely secret leak and
the injection vector the M6 scenario attacks, so every line passes redaction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from kubemend.tools.base import ClientError, ToolSpec, TransportError
from kubemend.tools.observability.provider import (
    LogQuery,
    LogResult,
    LogStream,
    TimeRangeError,
    parse_time,
)
from kubemend.tools.redact import redact_text

MAX_LIMIT = 500

EMPTY_HINT = "no log lines matched; check the stream selector and the time range"


class LokiProvider:
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

    def search_logs(self, query: LogQuery) -> LogResult:
        reference = self._now()
        try:
            start = parse_time(query.start, now=reference)
            end = parse_time(query.end, now=reference)
        except TimeRangeError as exc:
            raise ClientError(str(exc)) from exc
        if end <= start:
            raise ClientError(f"end ({query.end}) must be after start ({query.start})")

        # Clamped here rather than trusted from the model: a limit of 100000
        # would blow the result cap and cost a turn to truncation.
        limit = max(1, min(query.limit, MAX_LIMIT))
        payload = self._get(
            "/loki/api/v1/query_range",
            {
                "query": query.query,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": limit,
                "direction": query.direction,
            },
        )
        return self._to_result(payload, limit=limit)

    def _get(self, path: str, params: dict[str, str | int]) -> dict[str, object]:
        try:
            response = self._client.get(f"{self.base_url}{path}", params=params)
        except httpx.TimeoutException as exc:
            raise TransportError(f"loki timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"loki unreachable: {exc}") from exc

        if response.status_code >= 500:
            raise TransportError(f"loki returned {response.status_code}")
        if response.status_code >= 400:
            raise ClientError(f"loki rejected the query: {response.text[:200]}")
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise ClientError("loki returned an unexpected body")
        return parsed

    def _to_result(self, payload: dict[str, object], *, limit: int) -> LogResult:
        data = payload.get("data")
        raw = data.get("result", []) if isinstance(data, dict) else []

        streams: list[LogStream] = []
        total = 0
        for entry in raw:
            # Redaction happens here as well as in the executor wrapper. The
            # wrapper is the invariant (I3); this is defence in depth on the one
            # payload most likely to carry a credential.
            lines = [(str(ts), redact_text(str(line))) for ts, line in entry.get("values", [])]
            total += len(lines)
            streams.append(LogStream(labels=dict(entry.get("stream", {})), lines=lines))

        if not streams:
            return LogResult(streams=[], total_lines=0, limited=False, hint=EMPTY_HINT)
        return LogResult(streams=streams, total_lines=total, limited=total >= limit)


def logs_tool_spec(provider: LokiProvider) -> ToolSpec:
    """`search_logs` as the model sees it (docs/knowledge/tool-contracts.md)."""

    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        result = provider.search_logs(
            LogQuery(
                query=str(args["logql"]),
                start=str(args["start"]),
                end=str(args["end"]),
                limit=int(args.get("limit", 200)),
                direction="forward" if args.get("direction") == "forward" else "backward",
            )
        )
        payload: dict[str, Any] = {
            "streams": [
                {"labels": s.labels, "lines": [list(line) for line in s.lines]}
                for s in result.streams
            ],
            "total_lines": result.total_lines,
            "limited": result.limited,
        }
        if result.hint:
            payload["hint"] = result.hint
        return payload

    return ToolSpec(
        name="search_logs",
        description=(
            'Run a LogQL query against Loki. Use stream selectors ({namespace="x", '
            'pod=~"y.*"}) plus line filters (|= "error"). Results over the limit are '
            "cut server-side; narrow the time range or add filters."
        ),
        parameters={
            "type": "object",
            "properties": {
                "logql": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "limit": {"type": "integer", "maximum": 500, "default": 200},
                "direction": {
                    "type": "string",
                    "enum": ["backward", "forward"],
                    "default": "backward",
                },
            },
            "required": ["logql", "start", "end"],
        },
        executor=_execute,
        tier="read",
        timeout_s=20.0,
    )
