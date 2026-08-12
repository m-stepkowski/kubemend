"""Integration tests against the live lab cluster (M2 acceptance).

Marked `lab` and excluded from `task test`. They need `task lab:up` plus
`task lab:forward` for the Prometheus and Loki endpoints.

The security assertions here are the ones that would be embarrassing to get
wrong: that the agent's identity cannot mutate the cluster, and that a planted
Secret is unreachable through every path the read tools expose.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from kubemend.tools.kubernetes.api import KubeApiClient
from kubemend.tools.kubernetes.reader import ForbiddenKind, K8sQuery, KubernetesReader
from kubemend.tools.observability.loki import LokiProvider
from kubemend.tools.observability.prometheus import PrometheusProvider
from kubemend.tools.observability.provider import LogQuery, MetricQuery

pytestmark = pytest.mark.lab

READONLY_KUBECONFIG = Path.home() / ".kube" / "kubemend-lab-readonly"
ADMIN_KUBECONFIG = Path(__file__).resolve().parents[2] / ".lab" / "kubeconfig"
KUBECTL = Path(__file__).resolve().parents[2] / ".lab" / "bin" / "kubectl"

PROMETHEUS_URL = os.environ.get("KUBEMEND_PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.environ.get("KUBEMEND_LOKI_URL", "http://localhost:3100")

PLANTED_SECRET_VALUE = "planted-secret-value-must-never-surface"


def _reachable(url: str) -> bool:
    try:
        httpx.get(url, timeout=2.0)
    except httpx.HTTPError:
        return False
    return True


def _kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(KUBECTL), "--kubeconfig", str(ADMIN_KUBECONFIG), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module", autouse=True)
def require_lab() -> None:
    if not ADMIN_KUBECONFIG.exists():
        pytest.skip("lab cluster not up; run `task lab:up`")
    if not READONLY_KUBECONFIG.exists():
        pytest.skip("read-only kubeconfig missing; run `task lab:rbac`")


@pytest.fixture(scope="module")
def planted_secret() -> Iterator[None]:
    """Plant a Secret whose value must never appear in any tool payload."""
    _kubectl(
        "-n",
        "shop",
        "create",
        "secret",
        "generic",
        "planted",
        f"--from-literal=token={PLANTED_SECRET_VALUE}",
    )
    yield
    _kubectl("-n", "shop", "delete", "secret", "planted", "--ignore-not-found")


@pytest.fixture
def reader() -> KubernetesReader:
    return KubernetesReader(KubeApiClient(READONLY_KUBECONFIG))


# -- the trust boundary ---------------------------------------------------


def test_agent_identity_cannot_delete_a_pod() -> None:
    """The standing proof that the read path cannot mutate the cluster.

    RBAC is the real control; the allow-list in reader.py is defence in depth.
    This asserts the control itself, using the same kubeconfig the agent holds.
    """
    pods = subprocess.run(
        [
            str(KUBECTL),
            "--kubeconfig",
            str(READONLY_KUBECONFIG),
            "-n",
            "shop",
            "get",
            "pods",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert pods, "expected demo pods to exist in the shop namespace"

    attempt = subprocess.run(
        [
            str(KUBECTL),
            "--kubeconfig",
            str(READONLY_KUBECONFIG),
            "-n",
            "shop",
            "delete",
            "pod",
            pods,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert attempt.returncode != 0
    assert "forbidden" in attempt.stderr.lower()


def test_agent_identity_cannot_read_secrets(planted_secret: None) -> None:
    attempt = subprocess.run(
        [str(KUBECTL), "--kubeconfig", str(READONLY_KUBECONFIG), "-n", "shop", "get", "secrets"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert attempt.returncode != 0
    assert "forbidden" in attempt.stderr.lower()
    assert PLANTED_SECRET_VALUE not in attempt.stdout


def test_get_k8s_state_refuses_non_allowlisted_kinds(reader: KubernetesReader) -> None:
    with pytest.raises(ForbiddenKind):
        reader.get_state(K8sQuery(kind="secret", namespace="shop"))


def test_no_tool_payload_contains_the_planted_secret(
    reader: KubernetesReader, planted_secret: None
) -> None:
    """Sweep every allow-listed kind; none may surface the planted value."""
    for kind in ("pod", "deployment", "configmap", "service", "event"):
        payload = reader.get_state(K8sQuery(kind=kind, namespace="shop"))
        assert PLANTED_SECRET_VALUE not in json.dumps(payload), f"leaked via kind={kind}"


# -- observability --------------------------------------------------------


def test_promql_range_query_returns_downsampled_series() -> None:
    if not _reachable(f"{PROMETHEUS_URL}/-/ready"):
        pytest.skip("prometheus not reachable; run `task lab:forward`")
    provider = PrometheusProvider(PROMETHEUS_URL)

    result = provider.query_metrics(MetricQuery(query="up", start="-15m", end="now", max_points=10))

    assert result.series, "expected at least the scrape target's own up series"
    assert result.resolution_note is not None
    for series in result.series:
        assert len(series.points) <= 11, "downsampled to the point budget"


def test_logql_search_returns_the_workers_heartbeat() -> None:
    if not _reachable(f"{LOKI_URL}/ready"):
        pytest.skip("loki not reachable; run `task lab:forward`")
    provider = LokiProvider(LOKI_URL)

    # The worker logs this line on a fixed cadence, so it is a known-present
    # needle rather than whatever happens to be in the log buffer.
    deadline = time.time() + 60
    while time.time() < deadline:
        result = provider.search_logs(
            LogQuery(query='{namespace="shop"} |= "heartbeat"', start="-15m", end="now", limit=10)
        )
        if result.streams:
            break
        time.sleep(5)

    assert result.streams, "no shop logs in Loki; is promtail running?"
    lines = [line for stream in result.streams for _, line in stream.lines]
    assert any("heartbeat" in line for line in lines)


def test_k8s_reader_sees_the_demo_workloads(reader: KubernetesReader) -> None:
    payload = reader.get_state(
        K8sQuery(kind="pod", namespace="shop", selector="app.kubernetes.io/name=shop-api")
    )

    assert payload["items"], "shop-api pods should be running"
    names = [item["metadata"]["name"] for item in payload["items"]]
    assert all(name.startswith("shop-api") for name in names)
