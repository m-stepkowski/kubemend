"""Alert-to-incident contract for the operator (docs/knowledge/operator-design.md).

Pure function, no I/O: turns one Alertmanager-shaped alert into the same
`Task`/`Scope` a human's `kubemend run --task ... --namespace ... --app ...`
would produce, or a `RejectReason` explaining why it can't. Kept separate from
webhook.py so the alert->incident mapping is testable without an HTTP server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kubemend.core.model import Scope, Task


@dataclass(frozen=True)
class RejectReason:
    """Why an alert did not become an incident. Never raised — returned."""

    reason: str


def extract_incident(alert: dict[str, Any]) -> Task | RejectReason:
    """One entry from an Alertmanager webhook's `alerts` list.

    Only `status == "firing"` triggers; `resolved` is a normal, expected
    outcome (not an error) so it gets its own reason string rather than being
    lumped in with malformed input.
    """
    status = alert.get("status")
    if status != "firing":
        return RejectReason(f"not firing (status={status!r})")

    labels = alert.get("labels") or {}
    namespace = labels.get("namespace")
    app = labels.get("app")
    if not namespace or not app:
        return RejectReason("missing required label: namespace and app are both needed")

    annotations = alert.get("annotations") or {}
    alertname = labels.get("alertname", "alert")
    summary = annotations.get("summary") or annotations.get("description")
    if not summary:
        return RejectReason("missing annotations.summary and annotations.description")

    return Task(statement=f"{alertname}: {summary}", scope=Scope(namespace=namespace, app=app))
