"""Invariant I2 — errors are information.

Executors never raise into the loop; every failure returns a structured error
payload. Transport-class failures get exactly one retry, because they are
usually transient and the model can do nothing useful about them. A 4xx-class
rejection is never retried: the fix is a *different* call, and surfacing it
immediately is how the model corrects itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from kubemend.core.model import ToolCall
from kubemend.tools.base import ClientError, ToolSpec, TransportError
from kubemend.tools.registry import ToolRegistry

from .conftest import counting_tool, echo_tool, fail_with


def test_transport_error_is_retried_exactly_once() -> None:
    spec, attempts = fail_with("transport")
    registry = ToolRegistry([spec])

    outcome = registry.execute(ToolCall(id="c1", name=spec.name, arguments={}))

    assert len(attempts) == 2, "transport failures get one retry, not zero and not more"
    assert outcome.ok is False
    assert outcome.payload["error"]["type"] == "transport_error"


def test_transport_error_that_clears_on_retry_succeeds() -> None:
    def _flaky(attempt: int) -> dict[str, Any]:
        if attempt == 1:
            raise TransportError("connection reset")
        return {"recovered": True}

    spec, attempts = counting_tool("flaky", _flaky)
    registry = ToolRegistry([spec])

    outcome = registry.execute(ToolCall(id="c1", name="flaky", arguments={}))

    assert len(attempts) == 2
    assert outcome.ok is True
    assert outcome.payload == {"recovered": True}


def test_client_error_is_never_retried() -> None:
    spec, attempts = fail_with("client")
    registry = ToolRegistry([spec])

    outcome = registry.execute(ToolCall(id="c1", name=spec.name, arguments={}))

    assert len(attempts) == 1, "a 4xx is a bad request; repeating it verbatim cannot help"
    assert outcome.ok is False
    assert outcome.payload["error"]["type"] == "client_error"


def test_unexpected_exception_becomes_an_error_payload_not_a_raise() -> None:
    """An executor bug must not take the run down — the loop only ever sees data."""

    def _boom(_attempt: int) -> dict[str, Any]:
        raise ZeroDivisionError("executor bug")

    spec, attempts = counting_tool("boom", _boom)
    registry = ToolRegistry([spec])

    outcome = registry.execute(ToolCall(id="c1", name="boom", arguments={}))

    assert outcome.ok is False
    assert outcome.payload["error"]["type"] == "unexpected_error"
    assert len(attempts) == 1, "an unexpected exception is not transport-class"


def test_invalid_arguments_are_reported_without_executing() -> None:
    spec, attempts = counting_tool("strict", lambda _a: {"ok": True})
    spec = ToolSpec(
        name="strict",
        description="Requires a string `text`.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        executor=spec.executor,
    )
    registry = ToolRegistry([spec])

    missing = registry.execute(ToolCall(id="c1", name="strict", arguments={}))
    wrong_type = registry.execute(ToolCall(id="c2", name="strict", arguments={"text": 5}))

    assert missing.payload["error"]["type"] == "invalid_arguments"
    assert wrong_type.payload["error"]["type"] == "invalid_arguments"
    assert attempts == [], "validation happens before execution"


def test_unknown_tool_is_an_error_payload() -> None:
    registry = ToolRegistry([echo_tool()])

    outcome = registry.execute(ToolCall(id="c1", name="nope", arguments={}))

    assert outcome.ok is False
    assert outcome.payload["error"]["type"] == "unknown_tool"


def test_registry_execute_never_propagates_tool_errors() -> None:
    """Belt and braces on I2: nothing an executor raises escapes the wrapper."""
    for error in (TransportError("x"), ClientError("y"), RuntimeError("z")):

        def _raise(_attempt: int, exc: Exception = error) -> dict[str, Any]:
            raise exc

        spec, _ = counting_tool("raiser", _raise)
        registry = ToolRegistry([spec])
        try:
            registry.execute(ToolCall(id="c1", name="raiser", arguments={}))
        except Exception as exc:  # pragma: no cover - the assertion is the point
            pytest.fail(f"registry.execute leaked {type(exc).__name__} into the loop")
