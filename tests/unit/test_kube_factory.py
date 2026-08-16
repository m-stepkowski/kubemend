"""In-cluster vs. kubeconfig-file credential dispatch (ARCHITECTURE.md §8).

`build_kube_client` is the only place that branches on
`KubernetesConfig.in_cluster` — everything else only ever sees a
`KubeApiClient`. These tests mock the two `kubernetes.config` loader calls
directly rather than requiring a real kubeconfig file or a real cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kubernetes import config as k8s_config

from kubemend.config import KubernetesConfig
from kubemend.tools.kubernetes import api as api_module
from kubemend.tools.kubernetes.factory import build_kube_client


class _FakeApiClient:
    """Stands in for kubernetes.client.ApiClient for the file-based path."""


class _FakeDynamicClient:
    """`DynamicClient.__init__` eagerly does live API discovery against the
    configured host — real cluster I/O this dispatch test has no business
    triggering. Standing in for it here keeps these tests about "did
    build_kube_client pick the right loader", not "is DynamicClient's
    discovery correct" (that's what the lab-marked integration tests are
    for, against a real cluster)."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass


def test_in_cluster_false_loads_from_the_kubeconfig_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_new_client_from_config(*, config_file: str, context: str | None) -> _FakeApiClient:
        calls.append({"config_file": config_file, "context": context})
        return _FakeApiClient()

    monkeypatch.setattr(k8s_config, "new_client_from_config", fake_new_client_from_config)
    monkeypatch.setattr(api_module, "DynamicClient", _FakeDynamicClient)

    kubeconfig = tmp_path / "kubeconfig"
    cfg = KubernetesConfig(kubeconfig=kubeconfig, context="my-context", in_cluster=False)

    client = build_kube_client(cfg)

    assert isinstance(client, api_module.KubeApiClient)
    assert calls == [{"config_file": str(kubeconfig), "context": "my-context"}]


def test_in_cluster_true_never_touches_the_kubeconfig_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_based_calls: list[object] = []
    incluster_calls: list[object] = []

    monkeypatch.setattr(
        k8s_config,
        "new_client_from_config",
        lambda **kwargs: file_based_calls.append(kwargs),
    )
    monkeypatch.setattr(
        k8s_config,
        "load_incluster_config",
        lambda: incluster_calls.append(True),
    )
    monkeypatch.setattr(api_module, "DynamicClient", _FakeDynamicClient)

    cfg = KubernetesConfig(in_cluster=True)

    client = build_kube_client(cfg)

    assert isinstance(client, api_module.KubeApiClient)
    assert incluster_calls == [True]
    assert file_based_calls == [], "in-cluster mode must never read a kubeconfig file"


def test_in_cluster_true_ignores_kubeconfig_and_context_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if kubeconfig/context are left at their (file-mode) defaults,
    in_cluster=True must not attempt to use them."""
    monkeypatch.setattr(
        k8s_config,
        "new_client_from_config",
        lambda **_kwargs: pytest.fail("file-based loader must not be called"),
    )
    monkeypatch.setattr(k8s_config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(api_module, "DynamicClient", _FakeDynamicClient)

    cfg = KubernetesConfig(in_cluster=True)  # kubeconfig/context left at defaults

    build_kube_client(cfg)
