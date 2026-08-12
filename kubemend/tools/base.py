"""ToolSpec and the executor contract (ARCHITECTURE.md §3.1).

A spec carries name, description, JSON Schema, executor callable, tier
(read | propose | verify), and timeout. Errors return as structured payloads and
never raise into the loop (I2).

Executors signal failure by raising one of the two errors below; the registry
wrapper converts them into `{"error": {...}}` payloads. The split exists purely
so the wrapper can apply the retry rule: transport failures are worth one more
attempt, a rejected request never is.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kubemend.core.model import ToolTier

Executor = Callable[[dict[str, Any]], dict[str, Any]]


class ToolError(Exception):
    """Base for failures an executor reports deliberately."""

    error_type: str = "tool_error"


class TransportError(ToolError):
    """Timeout, connection reset, or 5xx — retried exactly once (I2)."""

    error_type = "transport_error"


class ClientError(ToolError):
    """A 4xx-class rejection. Never retried: the fix is a different call, and
    telling the model that immediately is how it corrects itself.
    """

    error_type = "client_error"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    executor: Executor
    tier: ToolTier = "read"
    timeout_s: float = 20.0

    def schema(self) -> dict[str, Any]:
        """The tool definition as the model sees it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
