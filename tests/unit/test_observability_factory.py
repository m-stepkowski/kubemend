"""Observability provider dispatch (ARCHITECTURE.md §3.2, M9).

`build_observability_tools` is the only place that branches on
`ObservabilityConfig.provider` — mirrors tests/unit/test_kube_factory.py's
dispatch-test style for the equivalent kubernetes seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kubemend.config import ObservabilityConfig, PillarToggle
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


# -- per-pillar toggle (M13) ----------------------------------------------


def test_metrics_and_logs_are_on_by_default_so_pre_m13_configs_are_unchanged() -> None:
    specs = build_observability_tools(ObservabilityConfig())

    assert [s.name for s in specs] == ["query_metrics", "search_logs"]


def test_a_disabled_pillar_registers_no_tool_at_all() -> None:
    """Not a tool that errors: the model can only waste iterations on a
    backend it can see."""
    cfg = ObservabilityConfig(enable=PillarToggle(metrics=False))

    specs = build_observability_tools(cfg)

    assert [s.name for s in specs] == ["search_logs"]


def test_logs_can_be_disabled_independently() -> None:
    cfg = ObservabilityConfig(enable=PillarToggle(logs=False))

    specs = build_observability_tools(cfg)

    assert [s.name for s in specs] == ["query_metrics"]


def test_disabling_every_pillar_is_a_config_error_not_a_silent_empty_registry() -> None:
    cfg = ObservabilityConfig(enable=PillarToggle(metrics=False, logs=False, traces=False))

    with pytest.raises(ObservabilityConfigError, match="enable at least one"):
        build_observability_tools(cfg)


def test_traces_default_off_because_not_every_cluster_runs_tracing() -> None:
    assert ObservabilityConfig().enable.traces is False


def test_a_pillar_the_provider_cannot_serve_fails_at_wiring_time(tmp_path: Path) -> None:
    """Named on both halves — which pillar, and which provider — so the fix is
    obvious from the message alone."""
    cfg = ObservabilityConfig(enable=PillarToggle(traces=True))

    with pytest.raises(ObservabilityConfigError) as exc:
        build_observability_tools(cfg)

    assert "traces" in str(exc.value)
    assert "prometheus_loki" in str(exc.value)


def test_a_disabled_pillar_never_reads_that_providers_credentials(tmp_path: Path) -> None:
    """Datadog's keys are only needed for the pillars it actually serves, so a
    logs-only run must not fail on an absent key file it never uses."""
    cfg = ObservabilityConfig(
        provider="datadog",
        datadog_api_key_file=tmp_path / "absent-api-key",
        datadog_app_key_file=tmp_path / "absent-app-key",
        enable=PillarToggle(metrics=False, logs=False, traces=False),
    )

    # All pillars off is its own error — the point is which error comes first:
    # the toggle check, before any credential file is touched.
    with pytest.raises(ObservabilityConfigError, match="enable at least one"):
        build_observability_tools(cfg)


def test_datadog_serves_traces_when_enabled(tmp_path: Path) -> None:
    api_key_file = tmp_path / "dd-api-key"
    app_key_file = tmp_path / "dd-app-key"
    api_key_file.write_text("k\n")
    app_key_file.write_text("k\n")
    cfg = ObservabilityConfig(
        provider="datadog",
        datadog_api_key_file=api_key_file,
        datadog_app_key_file=app_key_file,
        enable=PillarToggle(metrics=False, logs=False, traces=True),
    )

    specs = build_observability_tools(cfg)

    assert [s.name for s in specs] == ["query_traces"]
    assert "span_query" in specs[0].parameters["properties"]


def test_grafana_cloud_serves_traces_from_tempo_when_enabled(tmp_path: Path) -> None:
    token_file = tmp_path / "glc-token"
    token_file.write_text("glc-secret\n")
    cfg = ObservabilityConfig(
        provider="grafana_cloud",
        grafana_cloud_token_file=token_file,
        grafana_cloud_tempo_url="https://tempo-prod.grafana.net",
        grafana_cloud_tempo_instance_id="123456",
        enable=PillarToggle(metrics=False, logs=False, traces=True),
    )

    specs = build_observability_tools(cfg)

    assert [s.name for s in specs] == ["query_traces"]
    assert "traceql" in specs[0].parameters["properties"]


def test_grafana_cloud_traces_without_a_tempo_url_names_the_missing_field(tmp_path: Path) -> None:
    token_file = tmp_path / "glc-token"
    token_file.write_text("glc-secret\n")
    cfg = ObservabilityConfig(
        provider="grafana_cloud",
        grafana_cloud_token_file=token_file,
        enable=PillarToggle(metrics=False, logs=False, traces=True),
    )

    with pytest.raises(ObservabilityConfigError, match="grafana_cloud_tempo_url"):
        build_observability_tools(cfg)


def test_all_three_pillars_register_in_a_stable_order(tmp_path: Path) -> None:
    """Registration order is what the model sees; keep it predictable."""
    api_key_file = tmp_path / "dd-api-key"
    app_key_file = tmp_path / "dd-app-key"
    api_key_file.write_text("k\n")
    app_key_file.write_text("k\n")
    cfg = ObservabilityConfig(
        provider="datadog",
        datadog_api_key_file=api_key_file,
        datadog_app_key_file=app_key_file,
        enable=PillarToggle(metrics=True, logs=True, traces=True),
    )

    specs = build_observability_tools(cfg)

    assert [s.name for s in specs] == ["query_metrics", "search_logs", "query_traces"]
