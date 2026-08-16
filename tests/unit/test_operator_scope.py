"""Alert-to-incident contract (docs/knowledge/operator-design.md)."""

from __future__ import annotations

from kubemend.core.model import Task
from kubemend.operator.scope import RejectReason, extract_incident


def _alert(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "firing",
        "labels": {"alertname": "ShopApiCrashLooping", "namespace": "shop", "app": "shop-api"},
        "annotations": {"summary": "shop-api pods are crash-looping"},
    }
    base.update(overrides)
    return base


def test_firing_alert_with_summary_becomes_a_task() -> None:
    result = extract_incident(_alert())

    assert isinstance(result, Task)
    assert result.scope.namespace == "shop"
    assert result.scope.app == "shop-api"
    assert "ShopApiCrashLooping" in result.statement
    assert "crash-looping" in result.statement


def test_summary_falls_back_to_description() -> None:
    result = extract_incident(_alert(annotations={"description": "pods stuck in ImagePullBackOff"}))

    assert isinstance(result, Task)
    assert "ImagePullBackOff" in result.statement


def test_resolved_alert_is_rejected_not_errored() -> None:
    result = extract_incident(_alert(status="resolved"))

    assert isinstance(result, RejectReason)
    assert "firing" in result.reason


def test_missing_namespace_label_is_rejected() -> None:
    result = extract_incident(_alert(labels={"alertname": "X", "app": "shop-api"}))

    assert isinstance(result, RejectReason)


def test_missing_app_label_is_rejected() -> None:
    result = extract_incident(_alert(labels={"alertname": "X", "namespace": "shop"}))

    assert isinstance(result, RejectReason)


def test_missing_summary_and_description_is_rejected_not_fabricated() -> None:
    result = extract_incident(_alert(annotations={}))

    assert isinstance(result, RejectReason)
