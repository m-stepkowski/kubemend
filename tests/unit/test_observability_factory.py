"""Observability provider dispatch (ARCHITECTURE.md §3.2, M9).

`build_observability_tools` is the only place that branches on
`ObservabilityConfig.provider` — mirrors tests/unit/test_kube_factory.py's
dispatch-test style for the equivalent kubernetes seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kubemend.config import ObservabilityConfig
from kubemend.tools.observability.factory import ObservabilityConfigError, build_observability_tools


def test_prometheus_loki_is_the_default_dispatch() -> None:
    cfg = ObservabilityConfig()

    metrics_spec, logs_spec = build_observability_tools(cfg)

    assert metrics_spec.name == "query_metrics"
    assert "promql" in metrics_spec.parameters["properties"]
    assert logs_spec.name == "search_logs"
    assert "logql" in logs_spec.parameters["properties"]


def test_datadog_dispatch_reads_credentials_from_files(tmp_path: Path) -> None:
    api_key_file = tmp_path / "dd-api-key"
    app_key_file = tmp_path / "dd-app-key"
    api_key_file.write_text("dd-api-secret\n")
    app_key_file.write_text("dd-app-secret\n")
    cfg = ObservabilityConfig(
        provider="datadog",
        datadog_site="datadoghq.eu",
        datadog_api_key_file=api_key_file,
        datadog_app_key_file=app_key_file,
    )

    metrics_spec, logs_spec = build_observability_tools(cfg)

    assert metrics_spec.name == "query_metrics"
    assert "metric_query" in metrics_spec.parameters["properties"]
    assert logs_spec.name == "search_logs"
    assert "log_query" in logs_spec.parameters["properties"]


def test_datadog_dispatch_raises_a_useful_error_when_the_api_key_file_is_missing(
    tmp_path: Path,
) -> None:
    cfg = ObservabilityConfig(
        provider="datadog",
        datadog_api_key_file=tmp_path / "missing-api-key",
        datadog_app_key_file=tmp_path / "missing-app-key",
    )

    with pytest.raises(ObservabilityConfigError, match="datadog_api_key_file"):
        build_observability_tools(cfg)


def test_datadog_dispatch_raises_a_useful_error_when_only_the_app_key_file_is_missing(
    tmp_path: Path,
) -> None:
    api_key_file = tmp_path / "dd-api-key"
    api_key_file.write_text("dd-api-secret\n")
    cfg = ObservabilityConfig(
        provider="datadog",
        datadog_api_key_file=api_key_file,
        datadog_app_key_file=tmp_path / "missing-app-key",
    )

    with pytest.raises(ObservabilityConfigError, match="datadog_app_key_file"):
        build_observability_tools(cfg)


def test_grafana_cloud_dispatch_reads_credentials_and_reuses_the_prometheus_loki_schema(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "glc-token"
    token_file.write_text("glc-secret\n")
    cfg = ObservabilityConfig(
        provider="grafana_cloud",
        grafana_cloud_prometheus_url="https://prometheus-prod-1.grafana.net",
        grafana_cloud_prometheus_instance_id="123456",
        grafana_cloud_loki_url="https://logs-prod-1.grafana.net",
        grafana_cloud_loki_instance_id="654321",
        grafana_cloud_token_file=token_file,
    )

    metrics_spec, logs_spec = build_observability_tools(cfg)

    assert metrics_spec.name == "query_metrics"
    assert "promql" in metrics_spec.parameters["properties"]
    assert logs_spec.name == "search_logs"
    assert "logql" in logs_spec.parameters["properties"]


def test_grafana_cloud_dispatch_raises_a_useful_error_when_the_token_file_is_missing(
    tmp_path: Path,
) -> None:
    cfg = ObservabilityConfig(
        provider="grafana_cloud",
        grafana_cloud_prometheus_url="https://prometheus-prod-1.grafana.net",
        grafana_cloud_prometheus_instance_id="123456",
        grafana_cloud_loki_url="https://logs-prod-1.grafana.net",
        grafana_cloud_loki_instance_id="654321",
        grafana_cloud_token_file=tmp_path / "missing-token",
    )

    with pytest.raises(ObservabilityConfigError, match="grafana_cloud_token_file"):
        build_observability_tools(cfg)


def test_grafana_cloud_dispatch_raises_a_useful_error_when_a_required_field_is_unset(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "glc-token"
    token_file.write_text("glc-secret\n")
    cfg = ObservabilityConfig(
        provider="grafana_cloud",
        grafana_cloud_prometheus_url="",
        grafana_cloud_prometheus_instance_id="123456",
        grafana_cloud_loki_url="https://logs-prod-1.grafana.net",
        grafana_cloud_loki_instance_id="654321",
        grafana_cloud_token_file=token_file,
    )

    with pytest.raises(ObservabilityConfigError, match="grafana_cloud_prometheus_url"):
        build_observability_tools(cfg)
