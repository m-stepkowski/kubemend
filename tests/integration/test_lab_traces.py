"""Traces against the live lab Tempo (M13 acceptance).

Marked `lab` and excluded from `task test`. Needs `task lab:up` (which now
installs Tempo) plus `task lab:forward` for :3200 (query) and :4318 (OTLP).

This is the test that actually closes M13's acceptance bar. The unit tests in
test_tempo.py drive `MockTransport`, so they prove the shaping the *fixtures*
describe — but the fixtures are my reading of the OTLP/Tempo response format,
and a misread there would pass every unit test while failing in production.
Validating against the hosted Grafana Cloud account could not close that gap
either: its search endpoint answered correctly, but the instance held no
traces, so `/api/traces/<id>` and the OTLP `batches`/`scopeSpans`/`attributes`
parsing never ran.

So this pushes a real trace over OTLP and reads it back through the real
provider. Same `TempoProvider` code path Grafana Cloud uses — hosted Tempo
serves the identical HTTP API, which is why one implementation covers both.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from kubemend.tools.observability.provider import TraceQuery
from kubemend.tools.observability.tempo import TempoProvider

pytestmark = pytest.mark.lab

OTLP_ENDPOINT = "http://localhost:4318/v1/traces"
TEMPO_URL = "http://localhost:3200"
SERVICE = "kubemend-lab-traces-test"
# Fixed so a re-run overwrites rather than accumulating fixtures in Tempo.
TRACE_ID = "4d2e1f0a9b8c7d6e5f4a3b2c1d0e9f88"
# Planted to prove redaction runs on real span data, not just on fixtures.
PLANTED_SECRET = "Bearer sk-labtracevalidation0123456789"


def _span(
    span_id: str, name: str, ms: int, parent: str | None, attrs: dict[str, str], *, error: bool
) -> dict[str, Any]:
    start = time.time_ns() - 60_000_000_000
    return {
        "traceId": TRACE_ID,
        "spanId": span_id,
        **({"parentSpanId": parent} if parent else {}),
        "name": name,
        "kind": 2,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + ms * 1_000_000),
        "status": {"code": 2} if error else {},
        "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()],
    }


@pytest.fixture(scope="module")
def pushed_trace() -> str:
    """Push one trace over OTLP and wait for Tempo to index it."""
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": SERVICE}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "kubemend.lab"},
                        "spans": [
                            _span(
                                "bbbbbbbbbbbbbbb1",
                                "GET /checkout",
                                900,
                                None,
                                {"http.method": "GET"},
                                error=False,
                            ),
                            _span(
                                "bbbbbbbbbbbbbbb2",
                                "db.query",
                                700,
                                "bbbbbbbbbbbbbbb1",
                                {
                                    "db.system": "postgresql",
                                    "http.header.authorization": PLANTED_SECRET,
                                },
                                error=True,
                            ),
                            _span(
                                "bbbbbbbbbbbbbbb3",
                                "cache.get",
                                12,
                                "bbbbbbbbbbbbbbb1",
                                {"cache.hit": "false"},
                                error=False,
                            ),
                        ],
                    }
                ],
            }
        ]
    }
    try:
        response = httpx.post(OTLP_ENDPOINT, json=payload, timeout=20.0)
    except httpx.HTTPError as exc:  # pragma: no cover - lab wiring
        pytest.skip(f"lab Tempo OTLP endpoint unreachable ({exc}); run `task lab:forward`")
    assert response.status_code < 300, response.text

    provider = TempoProvider(TEMPO_URL)
    query = TraceQuery(query=f'{{resource.service.name="{SERVICE}"}}', start="-1h", end="now")
    # Tempo indexes asynchronously; the lab flushes fast but not instantly.
    for _ in range(30):
        time.sleep(4)
        if provider.query_traces(query).traces:
            return TRACE_ID
    pytest.fail("trace never became searchable in Tempo within 120s")


@pytest.fixture(scope="module")
def provider() -> TempoProvider:
    return TempoProvider(TEMPO_URL)


def test_traceql_search_finds_the_pushed_trace(provider: TempoProvider, pushed_trace: str) -> None:
    result = provider.query_traces(
        TraceQuery(query=f'{{resource.service.name="{SERVICE}"}}', start="-1h", end="now")
    )

    assert [t.trace_id for t in result.traces] == [pushed_trace]


def test_spans_are_fetched_and_parsed_from_the_otlp_response(
    provider: TempoProvider, pushed_trace: str
) -> None:
    """The half no hosted account could prove: `/api/traces/<id>` plus the
    OTLP batches/scopeSpans/attributes walk."""
    trace = provider.query_traces(
        TraceQuery(query=f'{{resource.service.name="{SERVICE}"}}', start="-1h", end="now")
    ).traces[0]

    assert trace.span_count == 3
    assert trace.root_name == "GET /checkout", "root is the parentless span"
    assert [s.name for s in trace.spans] == ["GET /checkout", "db.query", "cache.get"], (
        "spans come back slowest-first"
    )
    assert trace.spans[0].duration_ms == pytest.approx(900, abs=1), "nanos -> ms"
    assert all(s.service == SERVICE for s in trace.spans), "service.name from resource attrs"
    assert trace.spans[1].attributes["db.system"] == "postgresql"
    assert trace.spans[1].status, "an error span carries a status code"


def test_span_attributes_are_redacted_on_real_data(
    provider: TempoProvider, pushed_trace: str
) -> None:
    """Span attributes are user-controlled strings that reach the model, and a
    real backend is where that actually matters (I3)."""
    trace = provider.query_traces(
        TraceQuery(query=f'{{resource.service.name="{SERVICE}"}}', start="-1h", end="now")
    ).traces[0]

    rendered = str([s.attributes for s in trace.spans])
    assert PLANTED_SECRET not in rendered
    assert "redacted" in rendered


def test_min_duration_filters_server_side(provider: TempoProvider, pushed_trace: str) -> None:
    matched = provider.query_traces(
        TraceQuery(
            query=f'{{resource.service.name="{SERVICE}"}}',
            start="-1h",
            end="now",
            min_duration_ms=500,
        )
    )
    filtered_out = provider.query_traces(
        TraceQuery(
            query=f'{{resource.service.name="{SERVICE}"}}',
            start="-1h",
            end="now",
            min_duration_ms=5000,
        )
    )

    assert matched.traces, "a 900ms trace must survive a 500ms floor"
    assert not filtered_out.traces, "and must be excluded by a 5s floor"


def test_a_selector_matching_nothing_returns_the_actionable_hint(
    provider: TempoProvider,
) -> None:
    result = provider.query_traces(
        TraceQuery(query='{resource.service.name="no-such-service"}', start="-1h", end="now")
    )

    assert result.traces == []
    assert result.hint is not None and "min_duration_ms" in result.hint
