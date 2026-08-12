"""Payload redaction (ARCHITECTURE.md §3.3, invariant I3).

Applied inside the executor wrapper, after execution and before truncation, to
every payload from every tool. A regex pass masks bearer tokens, AWS keys, PEM
blocks, and connection-string passwords.

Secret values are structurally impossible here because they are never fetched —
not even to redact them.

Pod-spec env-var masking (`<redacted:ENV_NAME>` unless allow-listed) lands with
the Kubernetes reader in M2, since it needs the shape of a real pod spec to be
worth testing. The regex pass below already covers the highest-risk surface,
which is log lines.
"""

from __future__ import annotations

import re
from typing import Any, cast

_PEM = r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pem", re.compile(_PEM, re.S)),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{12,}=*")),
]

# Captures the password in scheme://user:password@host so only the secret is
# masked and the rest of the connection string stays diagnostic.
_CONNECTION_STRING = re.compile(r"(?P<prefix>://[^:@/\s]+:)(?P<secret>[^@/\s]+)(?P<suffix>@)")


def redact_text(value: str) -> str:
    for name, pattern in _PATTERNS:
        value = pattern.sub(f"<redacted:{name}>", value)
    return _CONNECTION_STRING.sub(
        lambda m: f"{m.group('prefix')}<redacted:connection_password>{m.group('suffix')}",
        value,
    )


def _walk(value: object) -> object:
    """Recurse over an arbitrary JSON structure masking every string in it.

    Keys are redacted as well as values: a dict keyed by a connection string is
    unusual but not impossible, and the cost of covering it is one line.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {_walk(k): _walk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(item) for item in value]
    return value


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask secrets in a tool payload. Shape is preserved, so the cast holds."""
    return cast(dict[str, Any], _walk(payload))
