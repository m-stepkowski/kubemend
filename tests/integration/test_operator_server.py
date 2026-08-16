"""Real HTTP server, real requests, mocked Job creation (docs/knowledge/
operator-design.md). Self-contained — a `ThreadingHTTPServer` on an
OS-assigned localhost port, no external process or cluster — so unlike the
`lab`-marked suite this runs as part of `task test`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from threading import Thread

import httpx
import pytest

from kubemend.operator import webhook as webhook_module
from kubemend.operator.cooldown import CooldownTracker
from kubemend.operator.jobs import JobCreated
from kubemend.operator.server import OperatorHTTPServer
from kubemend.operator.webhook import make_handler

pytestmark = pytest.mark.integration

TOKEN = "test-webhook-token"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setattr(
        webhook_module, "create_job", lambda task, **_kw: JobCreated(name="kubemend-run-test")
    )
    handler = make_handler(
        token=TOKEN,
        cooldown=CooldownTracker(),
        cooldown_seconds=300.0,
        chart_dir=Path("/chart"),
        job_values_file=Path("/values.yaml"),
        helm_bin=Path("/bin/helm"),
        kubectl_bin=Path("/bin/kubectl"),
        namespace="kubemend-system",
        release_name="kubemend",
    )
    httpd = OperatorHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _alert(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "firing",
        "labels": {"alertname": "ShopApiCrashLooping", "namespace": "shop", "app": "shop-api"},
        "annotations": {"summary": "shop-api pods are crash-looping"},
    }
    base.update(overrides)
    return base


def test_authenticated_firing_alert_triggers_a_job(server: str) -> None:
    resp = httpx.post(
        f"{server}/webhook",
        json={"alerts": [_alert()]},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert resp.status_code == 200
    assert resp.json()["results"][0]["decision"] == "triggered"
    assert resp.json()["results"][0]["job"] == "kubemend-run-test"


def test_missing_token_is_rejected_before_reaching_job_creation(server: str) -> None:
    resp = httpx.post(f"{server}/webhook", json={"alerts": [_alert()]})

    assert resp.status_code == 401


def test_wrong_token_is_rejected(server: str) -> None:
    resp = httpx.post(
        f"{server}/webhook",
        json={"alerts": [_alert()]},
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert resp.status_code == 401


def test_malformed_body_is_rejected(server: str) -> None:
    resp = httpx.post(
        f"{server}/webhook",
        content=b"not json",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )

    assert resp.status_code == 400


def test_second_request_within_cooldown_creates_no_second_job(server: str) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    first = httpx.post(f"{server}/webhook", json={"alerts": [_alert()]}, headers=headers)
    second = httpx.post(f"{server}/webhook", json={"alerts": [_alert()]}, headers=headers)

    assert first.json()["results"][0]["decision"] == "triggered"
    assert second.json()["results"][0]["decision"] == "rejected_cooldown"


def test_resolved_alert_is_rejected_not_triggered(server: str) -> None:
    resp = httpx.post(
        f"{server}/webhook",
        json={"alerts": [_alert(status="resolved")]},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert resp.json()["results"][0]["decision"] == "rejected"


def test_healthz_does_not_require_auth(server: str) -> None:
    resp = httpx.get(f"{server}/healthz")

    assert resp.status_code == 200
