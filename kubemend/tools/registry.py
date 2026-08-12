"""Tool registry and executor wrapper (ARCHITECTURE.md §3.1).

Every tool call goes through one path: validate arguments against the JSON
Schema, execute with a per-tool timeout, apply the I2 retry rule (exactly one
retry for transport-class failures, never for 4xx), redact, truncate, time, and
hand back a ToolOutcome.

Redaction living in this wrapper rather than in the tools is what makes I3
structural: no future tool can bypass it.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from kubemend.core.context import BYTES_PER_TOKEN
from kubemend.core.model import ToolCall, ToolOutcome
from kubemend.tools.base import ToolError, ToolSpec, TransportError
from kubemend.tools.redact import redact

TRUNCATION_MARKER = (
    "\n[TRUNCATED: {raw_bytes} bytes total. Narrow the query "
    "(shorter range, tighter selector, lower limit) to see more.]\n"
)

HEAD_FRACTION = 0.6

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Minimal JSON Schema check covering the subset the tool contracts use.

    `jsonschema` is not on the allowed dependency list (CLAUDE.md rule 1), and
    the schemas in docs/knowledge/tool-contracts.md only use required / type /
    enum / maximum. Returns an error detail, or None when the arguments are fine.
    """
    properties: dict[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in arguments:
            return f"missing required argument '{key}'"
    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                return f"unexpected argument '{key}'"
            continue
        if (detail := _check_value(key, value, spec)) is not None:
            return detail
    return None


def _check_value(key: str, value: object, spec: dict[str, Any]) -> str | None:
    expected = spec.get("type")
    if expected in _JSON_TYPES:
        allowed = _JSON_TYPES[expected]
        # bool is a subclass of int in Python; JSON Schema treats them apart.
        is_bool_mismatch = isinstance(value, bool) and expected in {"integer", "number"}
        if not isinstance(value, allowed) or is_bool_mismatch:
            return f"argument '{key}' must be of type {expected}, got {type(value).__name__}"
    if "enum" in spec and value not in spec["enum"]:
        return f"argument '{key}' must be one of {spec['enum']}"
    if "maximum" in spec and isinstance(value, (int, float)) and value > spec["maximum"]:
        return f"argument '{key}' must be at most {spec['maximum']}"
    if "minimum" in spec and isinstance(value, (int, float)) and value < spec["minimum"]:
        return f"argument '{key}' must be at least {spec['minimum']}"
    return None


def truncate(payload: dict[str, Any], cap_tokens: int) -> tuple[dict[str, Any], bool, int]:
    """Head 60 / tail 40 with a splice marker naming the real size.

    Head-only truncation is cheaper to implement and worse in practice: errors
    cluster at both ends of a log window, and the final stack trace is usually
    the most diagnostic line in the payload.
    """
    serialized = json.dumps(payload, sort_keys=True)
    raw_bytes = len(serialized)
    cap_bytes = cap_tokens * BYTES_PER_TOKEN
    if raw_bytes <= cap_bytes:
        return payload, False, raw_bytes

    head_len = int(cap_bytes * HEAD_FRACTION)
    tail_len = cap_bytes - head_len
    marker = TRUNCATION_MARKER.format(raw_bytes=raw_bytes)
    spliced = serialized[:head_len] + marker + serialized[raw_bytes - tail_len :]
    return {"content": spliced}, True, raw_bytes


def _error(error_type: str, detail: str) -> dict[str, Any]:
    return {"error": {"type": error_type, "detail": detail}}


class ToolRegistry:
    """Holds the tool specs and is the only way a tool ever gets invoked."""

    def __init__(
        self,
        specs: Sequence[ToolSpec] = (),
        *,
        result_token_cap: int = 6000,
        retry_backoff_s: float = 0.0,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in specs}
        self.result_token_cap = result_token_cap
        # Jittered backoff between the two attempts. Zero in tests so the suite
        # stays fast; the loop sets a real value.
        self.retry_backoff_s = retry_backoff_s

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._specs.values()]

    def execute(self, call: ToolCall) -> ToolOutcome:
        started = time.monotonic()
        spec = self._specs.get(call.name)

        if spec is None:
            known = ", ".join(sorted(self._specs)) or "none"
            return self._outcome(
                call,
                _error("unknown_tool", f"no tool named '{call.name}'; available: {known}"),
                ok=False,
                started=started,
            )

        if (detail := validate_arguments(spec.parameters, call.arguments)) is not None:
            return self._outcome(
                call, _error("invalid_arguments", detail), ok=False, started=started
            )

        payload, ok = self._invoke(spec, call.arguments)
        return self._outcome(call, payload, ok=ok, started=started)

    def _invoke(self, spec: ToolSpec, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Run the executor, applying the I2 retry rule. Never raises."""
        for attempt in (1, 2):
            try:
                return self._call_with_timeout(spec, arguments), True
            except TransportError as exc:
                if attempt == 2:
                    return _error(exc.error_type, str(exc)), False
                if self.retry_backoff_s:
                    time.sleep(self.retry_backoff_s * random.random())
            except ToolError as exc:
                # Client-class: repeating an identically bad request cannot help.
                return _error(exc.error_type, str(exc)), False
            except Exception as exc:
                # An executor bug must not take the run down (I2).
                return _error("unexpected_error", f"{type(exc).__name__}: {exc}"), False
        raise AssertionError("unreachable")  # pragma: no cover

    def _call_with_timeout(self, spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
        """Bound the executor by its per-tool timeout.

        A timed-out worker thread is abandoned rather than killed — Python has no
        safe way to interrupt one. That is acceptable because every executor is
        a bounded HTTP or subprocess call, and the alternative (a hung tool
        stalling the whole run) is worse.

        The pool is shut down with `wait=False` deliberately: a context manager
        would block on the very thread the timeout exists to escape.
        """
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(spec.executor, arguments)
        try:
            result = future.result(timeout=spec.timeout_s)
        except FutureTimeout:
            pool.shutdown(wait=False, cancel_futures=True)
            raise TransportError(f"timed out after {spec.timeout_s}s") from None
        pool.shutdown(wait=False)
        return result

    def _outcome(
        self, call: ToolCall, payload: dict[str, Any], *, ok: bool, started: float
    ) -> ToolOutcome:
        # I3: redaction precedes truncation, and both precede context.
        redacted = redact(payload)
        final, truncated, raw_bytes = truncate(redacted, self.result_token_cap)
        return ToolOutcome(
            call_id=call.id,
            ok=ok,
            payload=final,
            truncated=truncated,
            raw_bytes=raw_bytes,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
