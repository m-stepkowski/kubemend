"""Operator HTTP server entrypoint (docs/knowledge/operator-design.md).

`ThreadingHTTPServer`, not the single-threaded `HTTPServer`: a slow
`kubectl create` for one alert must not block a concurrent request for a
different scope.
"""

from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer
from pathlib import Path

from kubemend.config import OperatorConfig
from kubemend.operator.cooldown import CooldownTracker
from kubemend.operator.webhook import make_handler


class OperatorHTTPServer(ThreadingHTTPServer):
    """`HTTPServer.server_bind()` calls `socket.getfqdn()` to set
    `server_name` — a reverse-DNS lookup that can hang for tens of seconds on
    some networks (observed on macOS) and is never actually used by this
    handler. Skip it; `server_name`/`server_port` still get set correctly."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host if isinstance(host, str) else host.decode()
        self.server_port = port


def serve(cfg: OperatorConfig, *, helm_bin: Path, kubectl_bin: Path) -> None:
    """Blocks forever. The caller (`kubemend operator serve`) owns process
    lifecycle — this function never decides to exit on its own."""
    token = cfg.webhook_token_file.read_text().strip()
    cooldown = CooldownTracker()
    handler = make_handler(
        token=token,
        cooldown=cooldown,
        cooldown_seconds=cfg.cooldown_seconds,
        chart_dir=cfg.chart_dir,
        job_values_file=cfg.job_values_file,
        helm_bin=helm_bin,
        kubectl_bin=kubectl_bin,
        namespace=cfg.namespace,
        release_name=cfg.release_name,
    )
    server = OperatorHTTPServer(("0.0.0.0", cfg.port), handler)
    server.serve_forever()
