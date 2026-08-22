"""ObservabilityProvider Protocol (ARCHITECTURE.md §3.2).

Two methods — `query_metrics` and `search_logs` — over provider-neutral query
and result types. This is one of the three seams (with GitBackend and LLMClient)
where the project grows later without touching kubemend/core.

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


class MetricsSource(Protocol):
    def query_metrics(self, query: MetricQuery) -> MetricResult: ...


class LogsSource(Protocol):
    def search_logs(self, query: LogQuery) -> LogResult: ...


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
