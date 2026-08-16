"""MeteredLLM (ARCHITECTURE.md §2.7, §7).

Exists to fix a real bug: before this wrapper, `loop.py` only priced and
traced the one main-tier call it made directly — cheap-tier compaction and
handoff calls were either priced at the *main* model's rate or not charged
at all. These tests assert the fix at the unit level; `test_loop_model_window.py`
exercises it through a real `loop.run()`.
"""

from __future__ import annotations

from kubemend.config import ModelConfig, ModelSpec, RunConfig
from kubemend.core.model import ModelTier
from kubemend.llm.client import LLMResponse, Message, Usage
from kubemend.llm.fake import FakeLLM, text_turn
from kubemend.trace.cost import ModelPrice
from kubemend.trace.meter import MeteredLLM


def _cfg() -> RunConfig:
    return RunConfig(
        model=ModelConfig(main=ModelSpec(name="main-model"), cheap=ModelSpec(name="cheap-model"))
    )


_PRICING = {
    "main-model": ModelPrice(input=10.0, output=10.0),
    "cheap-model": ModelPrice(input=1.0, output=1.0),
}


def test_meter_prices_the_main_tier_using_the_main_models_rate() -> None:
    inner = FakeLLM(main=[LLMResponse(text="done", usage=Usage(input_tokens=1_000_000))])
    recorded: list[tuple[ModelTier, float]] = []
    meter = MeteredLLM(inner, _cfg(), _PRICING, record=lambda r, t, c: recorded.append((t, c)))

    meter.call([Message("user", "hi")], tier="main")

    assert recorded == [("main", 10.0)]


def test_meter_prices_the_cheap_tier_using_the_cheap_models_rate_not_main() -> None:
    """The bug this class fixes: a cheap-tier call must never be priced as
    if it ran on the main model."""
    inner = FakeLLM(cheap=[LLMResponse(text="summary", usage=Usage(input_tokens=1_000_000))])
    recorded: list[tuple[ModelTier, float]] = []
    meter = MeteredLLM(inner, _cfg(), _PRICING, record=lambda r, t, c: recorded.append((t, c)))

    meter.call([Message("user", "hi")], tier="cheap")

    assert recorded == [("cheap", 1.0)]


def test_meter_delegates_and_returns_the_wrapped_response_unchanged() -> None:
    inner = FakeLLM(main=[text_turn("the fix is in place")])
    meter = MeteredLLM(inner, _cfg(), {}, record=lambda r, t, c: None)

    result = meter.call([Message("user", "hi")], tier="main")

    assert result.text == "the fix is in place"


def test_meter_reports_exactly_once_per_call() -> None:
    inner = FakeLLM(main=[text_turn(), text_turn()])
    reports: list[int] = []
    meter = MeteredLLM(inner, _cfg(), {}, record=lambda r, t, c: reports.append(1))

    meter.call([Message("user", "hi")], tier="main")
    meter.call([Message("user", "hi")], tier="main")

    assert len(reports) == 2


def test_meter_falls_back_to_a_non_zero_price_for_an_unpriced_model() -> None:
    """An empty pricing table must never make a call look free."""
    inner = FakeLLM(main=[LLMResponse(usage=Usage(input_tokens=1_000_000))])
    recorded: list[float] = []
    meter = MeteredLLM(inner, _cfg(), {}, record=lambda r, t, c: recorded.append(c))

    meter.call([Message("user", "hi")], tier="main")

    assert recorded[0] > 0
