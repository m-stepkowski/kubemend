"""docs/knowledge/tool-contracts.md must not silently drift from the real
schemas (M9). Scoped to the observability tools, the only ones with a
provider axis — `query_metrics`/`search_logs` each have two provider-flavored
schemas, and nothing enforced the doc staying in sync with the code before
this test existed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kubemend.tools.base import ToolSpec
from kubemend.tools.observability.datadog import DatadogProvider
from kubemend.tools.observability.datadog import logs_tool_spec as datadog_logs_tool_spec
from kubemend.tools.observability.datadog import metrics_tool_spec as datadog_metrics_tool_spec
from kubemend.tools.observability.loki import LokiProvider
from kubemend.tools.observability.loki import logs_tool_spec as loki_logs_tool_spec
from kubemend.tools.observability.prometheus import (
    PrometheusProvider,
)
from kubemend.tools.observability.prometheus import (
    metrics_tool_spec as prometheus_metrics_tool_spec,
)

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "knowledge" / "tool-contracts.md"

_HEADING = re.compile(r"^## (?P<tool>\w+) — (?P<provider>\w+)", re.MULTILINE)


def _specs_by_heading() -> dict[tuple[str, str], ToolSpec]:
    return {
        ("query_metrics", "prometheus_loki"): prometheus_metrics_tool_spec(
            PrometheusProvider("http://prom:9090")
        ),
        ("query_metrics", "datadog"): datadog_metrics_tool_spec(
            DatadogProvider(site="datadoghq.com", api_key="k", app_key="k")
        ),
        ("search_logs", "prometheus_loki"): loki_logs_tool_spec(LokiProvider("http://loki:3100")),
        ("search_logs", "datadog"): datadog_logs_tool_spec(
            DatadogProvider(site="datadoghq.com", api_key="k", app_key="k")
        ),
    }


def _blocks_by_heading() -> dict[tuple[str, str], dict[str, object]]:
    text = DOC_PATH.read_text()
    blocks: dict[tuple[str, str], dict[str, object]] = {}
    for match in _HEADING.finditer(text):
        key = (match.group("tool"), match.group("provider"))
        rest = text[match.end() :]
        fence = re.search(r"```json\n(.*?)\n```", rest, re.DOTALL)
        assert fence is not None, f"no json block found under heading {key}"
        blocks[key] = json.loads(fence.group(1))
    return blocks


def test_doc_has_exactly_one_heading_per_known_provider_schema() -> None:
    assert set(_blocks_by_heading()) == set(_specs_by_heading())


def test_doc_json_blocks_match_the_real_tool_schemas() -> None:
    blocks = _blocks_by_heading()
    specs = _specs_by_heading()
    for key, spec in specs.items():
        assert blocks[key] == spec.schema(), f"{key} has drifted from tool-contracts.md"
