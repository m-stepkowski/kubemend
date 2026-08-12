"""Kubernetes reader shaping and allow-list (ARCHITECTURE.md §3.2, §3.3).

Driven against fixture API objects rather than a cluster. The properties under
test are the ones that would leak a credential or blow the context budget, and
neither needs kind running to verify.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kubemend.tools.kubernetes.reader import (
    MAX_EVENTS,
    ForbiddenKind,
    K8sQuery,
    KubernetesReader,
    cap_events,
    shape_configmap,
    shape_pod,
    strip_noise,
)


class FakeClient:
    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.items = items or []
        self.events = events or []
        self.calls: list[tuple[str, str, str | None]] = []

    def list_resource(
        self, kind: str, namespace: str, selector: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("list", kind, selector))
        return self.items

    def get_resource(self, kind: str, namespace: str, name: str) -> dict[str, Any]:
        self.calls.append(("get", kind, name))
        return self.items[0]

    def list_events(self, namespace: str, involved: str | None = None) -> list[dict[str, Any]]:
        return self.events


def _pod_with_env(env: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "metadata": {"name": "shop-api-1", "managedFields": [{"manager": "kubectl"}]},
        "spec": {"containers": [{"name": "api", "image": "nginx:1.27", "env": env}]},
    }


# -- allow-list -----------------------------------------------------------


def test_non_allowlisted_kind_is_refused_with_a_helpful_error() -> None:
    reader = KubernetesReader(FakeClient())

    with pytest.raises(ForbiddenKind) as exc:
        reader.get_state(K8sQuery(kind="secret", namespace="shop"))

    assert exc.value.error_type == "forbidden_kind"
    assert "allowed kinds are" in str(exc.value), "the error should teach, not just refuse"


def test_secret_is_not_reachable_under_any_casing() -> None:
    reader = KubernetesReader(FakeClient())
    for kind in ("Secret", "SECRET", "secrets"):
        with pytest.raises(ForbiddenKind):
            reader.get_state(K8sQuery(kind=kind, namespace="shop"))


# -- redaction ------------------------------------------------------------


def test_pod_env_values_are_masked_unless_allow_listed() -> None:
    pod = _pod_with_env(
        [
            {"name": "LOG_LEVEL", "value": "debug"},
            {"name": "DATABASE_PASSWORD", "value": "hunter2"},
            {"name": "STRIPE_KEY", "value": "sk_live_abcdef"},
        ]
    )

    env = shape_pod(pod)["spec"]["containers"][0]["env"]
    by_name = {e["name"]: e["value"] for e in env}

    assert by_name["LOG_LEVEL"] == "debug", "allow-listed names stay readable"
    assert by_name["DATABASE_PASSWORD"] == "<redacted:DATABASE_PASSWORD>"
    assert by_name["STRIPE_KEY"] == "<redacted:STRIPE_KEY>"
    assert "hunter2" not in json.dumps(env)
    assert "sk_live_abcdef" not in json.dumps(env)


def test_env_valuefrom_reference_is_preserved_without_a_value() -> None:
    """Knowing a value comes from secret/db-creds is useful; the value is not."""
    pod = _pod_with_env(
        [{"name": "DB_PASS", "valueFrom": {"secretKeyRef": {"name": "db-creds", "key": "pass"}}}]
    )

    env = shape_pod(pod)["spec"]["containers"][0]["env"]

    assert env[0]["valueFrom"]["secretKeyRef"]["name"] == "db-creds"
    assert "value" not in env[0]


def test_configmap_returns_key_names_and_sizes_but_never_values() -> None:
    cm = {
        "metadata": {"name": "shop-api-config"},
        "data": {"FEATURE_FLAGS": "checkout_v2=on", "ACCIDENTAL_TOKEN": "sk-should-not-appear"},
    }

    shaped = shape_configmap(cm)

    assert shaped["keys"] == ["ACCIDENTAL_TOKEN", "FEATURE_FLAGS"]
    assert shaped["value_bytes"]["FEATURE_FLAGS"] == len("checkout_v2=on")
    assert "data" not in shaped
    assert "sk-should-not-appear" not in json.dumps(shaped), (
        "a ConfigMap is exactly where a credential ends up by accident"
    )


# -- noise and volume -----------------------------------------------------


def test_control_plane_noise_is_stripped() -> None:
    obj = {
        "metadata": {
            "name": "x",
            "managedFields": [{"manager": "kubectl"}],
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": "{...huge...}",
                "meaningful": "keep me",
            },
        }
    }

    shaped = strip_noise(obj)

    assert "managedFields" not in shaped["metadata"]
    assert shaped["metadata"]["annotations"] == {"meaningful": "keep me"}


def test_events_are_capped_and_newest_first() -> None:
    events = [
        {"lastTimestamp": f"2026-08-12T10:{i:02d}:00Z", "message": f"e{i}"} for i in range(50)
    ]

    capped = cap_events(events)

    assert len(capped) == MAX_EVENTS
    assert capped[0]["message"] == "e49", "the most recent event is the diagnostic one"


# -- query behaviour ------------------------------------------------------


def test_listing_by_selector_includes_events_by_default() -> None:
    client = FakeClient(items=[_pod_with_env([])], events=[{"lastTimestamp": "t", "message": "m"}])
    reader = KubernetesReader(client)

    payload = reader.get_state(
        K8sQuery(kind="pod", namespace="shop", selector="app.kubernetes.io/name=shop-api")
    )

    assert payload["kind"] == "pod"
    assert len(payload["items"]) == 1
    assert payload["events"][0]["message"] == "m"
    assert client.calls[0] == ("list", "pod", "app.kubernetes.io/name=shop-api")


def test_empty_result_carries_a_hint_rather_than_an_error() -> None:
    reader = KubernetesReader(FakeClient(items=[]))

    payload = reader.get_state(K8sQuery(kind="pod", namespace="shop", selector="app=nope"))

    assert payload["items"] == []
    assert "no pod found" in payload["hint"]
