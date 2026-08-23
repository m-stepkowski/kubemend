"""ObservabilityProvider Protocol (ARCHITECTURE.md §3.2).

`query_metrics` and `search_logs`, plus `query_traces` (M13) behind its own
`TracesSource` seam, over provider-neutral query and result types. This is one
of the three seams (with GitBackend and LLMClient) where the project grows
later without touching kubemend/core.

Traces are a *separate* Protocol rather than a third method on
`ObservabilityProvider`: metrics and logs exist in every backend this project
targets, tracing does not, and `observability.enable.traces` defaults off for
that reason. Folding it into the combined Protocol would oblige every provider
to implement a method most of them cannot serve.

The types below are deliberately not PromQL/LogQL-shaped beyond the query string
itself: a Dynatrace or CloudWatch provider should be able to satisfy this
interface by translating its own results into `MetricSeries` / `LogStream`,
without the tool layer or the loop learning anything new.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

Direction = Literal["backward", "forward"]

_RELATIVE = re.compile(r"^-(?P<amount>\d+)(?P<unit>[smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class TimeRangeError(ValueError):
    """Raised for a timestamp the provider cannot interpret."""


def parse_time(value: str, *, now: datetime | None = None) -> datetime:
    """Resolve `now`, a relative offset like `-30m`, or an RFC3339 timestamp.

    Relative forms are resolved provider-side rather than by the model, so the
    same trace replays to the same absolute window regardless of when it is read.
    """
    reference = now or datetime.now(UTC)
    text = value.strip()
    if text == "now":
        return reference
    if (match := _RELATIVE.match(text)) is not None:
        unit = _UNITS[match.group("unit")]
        return reference - timedelta(**{unit: int(match.group("amount"))})
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeRangeError(
            f"cannot parse time '{value}'; use RFC3339, 'now', or a relative offset like -30m"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def downsample(
    points: list[tuple[float, float]], max_points: int
) -> tuple[list[tuple[float, float]], bool]:
    """Keep at most `max_points` samples by striding, always keeping the last.

    The final sample is what says whether the problem is still happening, so it
    survives even when the stride would otherwise drop it. Provider-neutral —
    every backend downsamples client-side the same way once it has returned
    whatever points it has.
    """
    if max_points <= 0 or len(points) <= max_points:
        return points, False
    stride = math.ceil(len(points) / max_points)
    reduced = points[::stride]
    if reduced[-1] != points[-1]:
        reduced.append(points[-1])
    return reduced, True


@dataclass(frozen=True)
class MetricQuery:
    """`start`/`end` accept RFC3339 or a relative form like `-30m` / `now`.

    Relative times are resolved by the provider rather than the model, so a run
    is reproducible from its trace without depending on when it is replayed.
    """

    query: str
    start: str
    end: str
    step: str | None = None
    max_points: int = 100


@dataclass(frozen=True)
class MetricSeries:
    labels: dict[str, str]
    points: list[tuple[float, float]] = field(default_factory=list)


@dataclass(frozen=True)
class MetricResult:
    series: list[MetricSeries] = field(default_factory=list)
    resolution_note: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class LogQuery:
    query: str
    start: str
    end: str
    limit: int = 200
    direction: Direction = "backward"


@dataclass(frozen=True)
class LogStream:
    labels: dict[str, str]
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class LogResult:
    streams: list[LogStream] = field(default_factory=list)
    total_lines: int = 0
    limited: bool = False
    hint: str | None = None


@dataclass(frozen=True)
class TraceQuery:
    """A trace search, in whatever selector dialect the provider speaks
    (TraceQL for Tempo, a span-search query for Datadog APM).

    `min_duration_ms` is first-class rather than left to the query string:
    "show me the slow ones" is the question tracing is actually asked during
    an incident, and every backend expresses it differently.
    """

    query: str
    start: str
    end: str
    min_duration_ms: float | None = None
    limit: int = 20


@dataclass(frozen=True)
class Span:
    """One span, flattened to what a diagnosis needs.

    Deliberately not a tree: reconstructing parent/child in context costs
    tokens the model rarely spends well. `depth` preserves the shape that
    matters — which call sits under which — without the nesting.
    """

    name: str
    service: str
    duration_ms: float
    depth: int = 0
    status: str = ""
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Trace:
    trace_id: str
    root_name: str
    duration_ms: float
    span_count: int
    start_time: str = ""
    spans: list[Span] = field(default_factory=list)


@dataclass(frozen=True)
class TraceResult:
    traces: list[Trace] = field(default_factory=list)
    limited: bool = False
    hint: str | None = None


class MetricsSource(Protocol):
    def query_metrics(self, query: MetricQuery) -> MetricResult: ...


class LogsSource(Protocol):
    def search_logs(self, query: LogQuery) -> LogResult: ...


class TracesSource(Protocol):
    def query_traces(self, query: TraceQuery) -> TraceResult: ...


class ObservabilityProvider(MetricsSource, LogsSource, Protocol):
    """Both halves behind one seam, so a future single-vendor backend
    (Dynatrace, CloudWatch) can replace the pair without the loop noticing."""


@dataclass(frozen=True)
class CompositeProvider:
    """The `prometheus_loki` provider: two backends presented as one.

    Structural typing means this never imports the concrete classes, so the
    Prometheus and Loki modules stay independent of each other.
    """

    metrics: MetricsSource
    logs: LogsSource

    def query_metrics(self, query: MetricQuery) -> MetricResult:
        return self.metrics.query_metrics(query)

    def search_logs(self, query: LogQuery) -> LogResult:
        return self.logs.search_logs(query)
