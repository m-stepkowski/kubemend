"""Observability provider dispatch (ARCHITECTURE.md §3.2, M9).

`ObservabilityConfig.provider` is the only place that branches on which
observability backend a run uses — everything else (the registry, the loop)
only ever sees the two `ToolSpec`s this returns. Mirrors
`kubemend/tools/kubernetes/factory.py:build_kube_client` and
`kubemend/llm/factory.py:make_client`'s one-branch-point pattern.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from kubemend.config import ObservabilityConfig
from kubemend.tools.base import ToolSpec
from kubemend.tools.observability.datadog import DatadogProvider
from kubemend.tools.observability.datadog import logs_tool_spec as datadog_logs_tool_spec
from kubemend.tools.observability.datadog import metrics_tool_spec as datadog_metrics_tool_spec
from kubemend.tools.observability.loki import LokiProvider, logs_tool_spec
from kubemend.tools.observability.prometheus import PrometheusProvider, metrics_tool_spec


class ObservabilityConfigError(RuntimeError):
    """Raised when the configured provider is missing a required credential file."""


def build_observability_tools(cfg: ObservabilityConfig) -> tuple[ToolSpec, ToolSpec]:
    """Return (query_metrics, search_logs) tool specs for `cfg.provider`."""
    if cfg.provider == "prometheus_loki":
        prometheus = PrometheusProvider(cfg.prometheus_url)
        loki = LokiProvider(cfg.loki_url)
        return metrics_tool_spec(prometheus), logs_tool_spec(loki)
    if cfg.provider == "datadog":
        datadog = DatadogProvider(
            site=cfg.datadog_site,
            api_key=_read_token(
                cfg.datadog_api_key_file, "datadog", "observability.datadog_api_key_file"
            ),
            app_key=_read_token(
                cfg.datadog_app_key_file, "datadog", "observability.datadog_app_key_file"
            ),
        )
        return datadog_metrics_tool_spec(datadog), datadog_logs_tool_spec(datadog)
    if cfg.provider == "grafana_cloud":
        token = _read_token(
            cfg.grafana_cloud_token_file, "grafana_cloud", "observability.grafana_cloud_token_file"
        )
        prometheus = PrometheusProvider(
            _require_set(
                cfg.grafana_cloud_prometheus_url, "observability.grafana_cloud_prometheus_url"
            ),
            auth=httpx.BasicAuth(
                _require_set(
                    cfg.grafana_cloud_prometheus_instance_id,
                    "observability.grafana_cloud_prometheus_instance_id",
                ),
                token,
            ),
        )
        loki = LokiProvider(
            _require_set(cfg.grafana_cloud_loki_url, "observability.grafana_cloud_loki_url"),
            auth=httpx.BasicAuth(
                _require_set(
                    cfg.grafana_cloud_loki_instance_id,
                    "observability.grafana_cloud_loki_instance_id",
                ),
                token,
            ),
        )
        return metrics_tool_spec(prometheus), logs_tool_spec(loki)
    raise ObservabilityConfigError(  # pragma: no cover - Literal-closed
        f"unknown observability provider {cfg.provider!r}"
    )


def _read_token(path: Path, provider: str, field_name: str) -> str:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise ObservabilityConfigError(
            f"observability.provider is {provider!r} but no credential file at "
            f"{resolved} — set {field_name}"
        )
    return resolved.read_text().strip()


def _require_set(value: str, field_name: str) -> str:
    if not value.strip():
        raise ObservabilityConfigError(
            f"observability.provider is 'grafana_cloud' but {field_name} is unset — "
            "Grafana Cloud URLs and instance IDs are account-specific with no sane default"
        )
    return value
