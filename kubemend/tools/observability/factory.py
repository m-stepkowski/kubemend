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


# Registration order, which is the order the model sees the tools in.
PILLARS = ("metrics", "logs", "traces")


def build_observability_tools(cfg: ObservabilityConfig) -> list[ToolSpec]:
    """Tool specs for the pillars `cfg.enable` turns on, in `PILLARS` order.

    A disabled pillar contributes no tool at all rather than one that errors:
    the model can only spend iterations on a backend it can see, and the whole
    point of the toggle is that not every cluster has all three.

    A pillar enabled against a provider that cannot serve it is a config
    mistake, not a runtime condition — it fails here, at wiring time, with
    both halves named.
    """
    wanted = [pillar for pillar in PILLARS if getattr(cfg.enable, pillar)]
    if not wanted:
        raise ObservabilityConfigError(
            "observability.enable turns off metrics, logs and traces — a run with no "
            "observability tool at all can only read Kubernetes state; enable at least one"
        )
    available = _specs_for_provider(cfg, wanted)
    for pillar in wanted:
        if pillar not in available:
            raise ObservabilityConfigError(
                f"observability.enable.{pillar} is true but provider {cfg.provider!r} "
                f"does not support {pillar}"
            )
    return [available[pillar] for pillar in wanted]


def _specs_for_provider(cfg: ObservabilityConfig, wanted: list[str]) -> dict[str, ToolSpec]:
    """Build only the pillars asked for — so a disabled pillar never
    constructs a client or reads a credential it doesn't need."""
    specs: dict[str, ToolSpec] = {}
    if cfg.provider == "prometheus_loki":
        if "metrics" in wanted:
            specs["metrics"] = metrics_tool_spec(PrometheusProvider(cfg.prometheus_url))
        if "logs" in wanted:
            specs["logs"] = logs_tool_spec(LokiProvider(cfg.loki_url))
        return specs
    if cfg.provider == "datadog":
        if {"metrics", "logs"} & set(wanted):
            datadog = DatadogProvider(
                site=cfg.datadog_site,
                api_key=_read_token(
                    cfg.datadog_api_key_file, "datadog", "observability.datadog_api_key_file"
                ),
                app_key=_read_token(
                    cfg.datadog_app_key_file, "datadog", "observability.datadog_app_key_file"
                ),
            )
            if "metrics" in wanted:
                specs["metrics"] = datadog_metrics_tool_spec(datadog)
            if "logs" in wanted:
                specs["logs"] = datadog_logs_tool_spec(datadog)
        return specs
    if cfg.provider == "grafana_cloud":
        token = _read_token(
            cfg.grafana_cloud_token_file, "grafana_cloud", "observability.grafana_cloud_token_file"
        )
        if "metrics" in wanted:
            specs["metrics"] = metrics_tool_spec(
                PrometheusProvider(
                    _require_set(
                        cfg.grafana_cloud_prometheus_url,
                        "observability.grafana_cloud_prometheus_url",
                    ),
                    auth=httpx.BasicAuth(
                        _require_set(
                            cfg.grafana_cloud_prometheus_instance_id,
                            "observability.grafana_cloud_prometheus_instance_id",
                        ),
                        token,
                    ),
                )
            )
        if "logs" in wanted:
            specs["logs"] = logs_tool_spec(
                LokiProvider(
                    _require_set(
                        cfg.grafana_cloud_loki_url, "observability.grafana_cloud_loki_url"
                    ),
                    auth=httpx.BasicAuth(
                        _require_set(
                            cfg.grafana_cloud_loki_instance_id,
                            "observability.grafana_cloud_loki_instance_id",
                        ),
                        token,
                    ),
                )
            )
        return specs
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
