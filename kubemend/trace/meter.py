"""Cost metering wrapper (ARCHITECTURE.md §2.7, §7).

Wraps any `LLMClient` so *every* call is priced at its own tier's rate and
reported — not just the main-tier agent turns `loop.py` calls directly.
Before this, cheap-tier compaction (`Context.compact`) and handoff
(`core/handoff.request_handoff`) calls were silently unpriced and untraced:
the loop only charged/traced the one call it made itself, at the main
model's price regardless of which tier actually ran.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kubemend.config import RunConfig
from kubemend.core.model import ModelTier
from kubemend.llm.client import LLMClient, LLMResponse, Message
from kubemend.trace.cost import ModelPrice, price_for, usd

RecordFn = Callable[[LLMResponse, ModelTier, float], None]


class MeteredLLM:
    """Satisfies the `LLMClient` Protocol; delegates to the wrapped client,
    then prices the response at the tier's own configured model and reports
    it via `record` — callers decide what "report" means (charge a budget,
    write a trace event, both)."""

    def __init__(
        self, client: LLMClient, cfg: RunConfig, pricing: dict[str, ModelPrice], *, record: RecordFn
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._pricing = pricing
        self._record = record

    def call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tier: ModelTier = "main",
    ) -> LLMResponse:
        response = self._client.call(messages, tools=tools, tier=tier)
        model_name = self._cfg.model.cheap.name if tier == "cheap" else self._cfg.model.main.name
        cost = usd(response.usage, price_for(model_name, self._pricing))
        self._record(response, tier, cost)
        return response
