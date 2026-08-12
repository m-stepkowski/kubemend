"""Redaction fixtures (ARCHITECTURE.md §3.3, invariant I3).

The four patterns in the M2 acceptance list, plus the property that matters more
than any individual pattern: redaction runs inside the executor wrapper, so it
cannot be bypassed by adding a tool that forgets to call it.
"""

from __future__ import annotations

import json

from kubemend.core.model import ToolCall
from kubemend.tools.base import ToolSpec
from kubemend.tools.redact import redact, redact_text
from kubemend.tools.registry import ToolRegistry

PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAx7Vk9F4pQq2n3mJmH0pQ\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_bearer_token_is_masked() -> None:
    out = redact_text("GET /v1 Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6" not in out
    assert "<redacted:bearer_token>" in out


def test_aws_access_key_is_masked() -> None:
    for key in ("AKIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7EXAMPLE"):
        out = redact_text(f"using key {key} for s3")
        assert key not in out
        assert "<redacted:aws_key>" in out


def test_pem_private_key_block_is_masked() -> None:
    out = redact_text(f"loaded key:\n{PEM}\ndone")
    assert "MIIEowIBAAKCAQEAx7Vk9F4pQq2n3mJmH0pQ" not in out
    assert "<redacted:pem>" in out
    assert "loaded key:" in out and "done" in out, "surrounding context survives"


def test_connection_string_password_is_masked_but_the_rest_survives() -> None:
    out = redact_text("dsn=postgres://app_user:s3cr3t-p4ss@db.internal:5432/shop")

    assert "s3cr3t-p4ss" not in out
    assert "<redacted:connection_password>" in out
    # Host, user and database are exactly what makes the log line diagnostic.
    assert "postgres://app_user:" in out
    assert "@db.internal:5432/shop" in out


def test_redaction_walks_nested_structures() -> None:
    payload = {
        "streams": [{"lines": [["1", "Authorization: Bearer abcdefghijklmno"]]}],
        "meta": {"note": "AKIAIOSFODNN7EXAMPLE"},
    }

    rendered = json.dumps(redact(payload))

    assert "abcdefghijklmno" not in rendered
    assert "AKIAIOSFODNN7EXAMPLE" not in rendered


def test_redaction_is_applied_by_the_executor_wrapper_not_the_tool() -> None:
    """I3 in one test: a tool that never redacts still cannot leak.

    This is the property that makes the invariant structural. Any future tool
    author gets redaction whether or not they know it exists.
    """
    leaky = ToolSpec(
        name="leaky",
        description="Returns a credential verbatim.",
        parameters={"type": "object", "properties": {}},
        executor=lambda _args: {"log": "Authorization: Bearer super-secret-token-value"},
    )
    registry = ToolRegistry([leaky])

    outcome = registry.execute(ToolCall(id="c1", name="leaky", arguments={}))

    rendered = json.dumps(outcome.payload)
    assert "super-secret-token-value" not in rendered
    assert "<redacted:bearer_token>" in rendered
