"""Alertmanager webhook receiver (docs/knowledge/operator-design.md).

`do_POST` order is deliberate: auth check first, using only the request
headers — before the body is even read, let alone parsed — so an
unauthenticated request never reaches `extract_incident` or the cooldown
tracker. Any exception during handling is caught and turned into a 500 plus
a logged decision line rather than an unhandled exception; `errors return,
never raise` (I2's spirit) applies here even though this sits outside
`core/loop.py`.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from kubemend.operator.cooldown import CooldownTracker
from kubemend.operator.jobs import JobCreationFailed, create_job
from kubemend.operator.scope import RejectReason, extract_incident

WEBHOOK_PATH = "/webhook"
HEALTHZ_PATH = "/healthz"
READYZ_PATH = "/readyz"
_BEARER_PREFIX = "Bearer "

_LOGGER = logging.getLogger("kubemend.operator")


def is_authorized(authorization_header: str | None, token: str) -> bool:
    """Pure so it's testable without spinning up an HTTP server."""
    if authorization_header is None or not authorization_header.startswith(_BEARER_PREFIX):
        return False
    presented = authorization_header[len(_BEARER_PREFIX) :]
    return hmac.compare_digest(presented, token)


def make_handler(
    *,
    token: str,
    cooldown: CooldownTracker,
    cooldown_seconds: float,
    chart_dir: Path,
    job_values_file: Path,
    helm_bin: Path,
    kubectl_bin: Path,
    namespace: str,
    release_name: str,
) -> type[BaseHTTPRequestHandler]:
    """Closes over the operator's dependencies and returns a handler class —
    `ThreadingHTTPServer` instantiates one per connection, so the
    dependencies can't be constructor arguments on the instance itself."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in (HEALTHZ_PATH, READYZ_PATH):
                self._respond(200, {"status": "ok"})
                return
            self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != WEBHOOK_PATH:
                self._respond(404, {"error": "not found"})
                return

            if not is_authorized(self.headers.get("Authorization"), token):
                _LOGGER.info("decision=rejected_unauthorized path=%s", self.path)
                self._respond(401, {"error": "unauthorized"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw)
                alerts = payload.get("alerts", [])
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                _LOGGER.info("decision=rejected_malformed detail=%s", exc)
                self._respond(400, {"error": "malformed payload"})
                return

            try:
                results = [self._handle_one_alert(alert) for alert in alerts]
            except Exception as exc:  # never let one malformed alert crash the whole request
                _LOGGER.exception("decision=handler_error detail=%s", exc)
                self._respond(500, {"error": "internal error"})
                return

            self._respond(200, {"results": results})

        def _handle_one_alert(self, alert: dict[str, Any]) -> dict[str, Any]:
            incident = extract_incident(alert)
            if isinstance(incident, RejectReason):
                _LOGGER.info("decision=rejected_malformed detail=%s", incident.reason)
                return {"decision": "rejected", "reason": incident.reason}

            key = (incident.scope.namespace, incident.scope.app)
            if not cooldown.try_acquire(key, time.time(), cooldown_seconds):
                _LOGGER.info("decision=rejected_cooldown namespace=%s app=%s", *key)
                return {"decision": "rejected_cooldown", "namespace": key[0], "app": key[1]}

            result = create_job(
                incident,
                chart_dir=chart_dir,
                values_file=job_values_file,
                helm_bin=helm_bin,
                kubectl_bin=kubectl_bin,
                namespace=namespace,
                release_name=release_name,
            )
            if isinstance(result, JobCreationFailed):
                _LOGGER.error("decision=job_creation_failed detail=%s", result.detail)
                return {"decision": "failed", "detail": result.detail}

            _LOGGER.info("decision=triggered job=%s namespace=%s app=%s", result.name, *key)
            return {"decision": "triggered", "job": result.name}

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, log_format: str, *args: object) -> None:
            _LOGGER.debug("%s - %s", self.address_string(), log_format % args)

    return Handler
