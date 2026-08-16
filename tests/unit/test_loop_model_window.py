"""Per-model context window drives compaction (ARCHITECTURE.md §2.3-2.4, §2.7).

`ModelSpec.window_tokens` overrides the global `context.model_window_tokens`
fallback for the main tier — a smaller window (e.g. a 128k local model) must
make compaction trigger sooner than the 200k default would, proving the loop
actually threads the override through rather than just accepting the field.
"""

from __future__ import annotations

from kubemend.config import BudgetConfig, ContextConfig, ModelConfig, ModelSpec, RunConfig
from kubemend.core.loop import run
from kubemend.core.model import Task
from kubemend.llm.fake import FakeLLM, summary_turn, tool_turn
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder

from .conftest import StubGate, counting_tool


def test_per_model_window_tokens_makes_compaction_trigger_sooner(
    task: Task, trace: TraceRecorder
) -> None:
    # Context.compact() no-ops below 3 exchanges regardless of size (2 are
    # always kept), so 3 exchanges of 150k bytes each (~112.5k rendered
    # tokens by the 3rd) is the smallest case that actually exercises this:
    # crosses 0.70 x 128_000 (89_600) but stays under 0.70 x 200_000
    # (140_000) — only the override explains a compaction call happening.
    probe, _ = counting_tool("probe", lambda _a: {"data": "x" * 150_000})
    # ToolRegistry's own result_token_cap (default 6000) would otherwise
    # truncate the payload long before Context ever sees it.
    registry = ToolRegistry([probe], result_token_cap=10_000_000)

    cfg = RunConfig(
        model=ModelConfig(
            main=ModelSpec(name="fake-main", window_tokens=128_000),
            cheap=ModelSpec(name="fake-cheap"),
        ),
        budgets=BudgetConfig(max_iterations=3, max_wall_seconds=600),
        context=ContextConfig(
            result_token_cap=10_000_000,  # large enough that truncation never
            compact_threshold=0.70,  # intervenes before compaction sees the payload
            model_window_tokens=200_000,  # the global default, deliberately not overridden
        ),
    )
    # Distinct arguments per call: identical repeats would trip the loop
    # detector (nudge, then abort) before 3 real exchanges ever accumulate.
    llm = FakeLLM(
        main=[tool_turn("probe", {"i": i}) for i in range(3)],
        cheap=[summary_turn("SUMMARY: compacted")],
    )
    gate = StubGate([])

    run(task, cfg, llm=llm, registry=registry, gate=gate, trace=trace)

    # One cheap call for the compaction summary (fires on the 3rd exchange),
    # one for the budget_exhausted handoff — a single call would mean
    # compaction never fired, i.e. the global 200k window won instead.
    assert len(llm.cheap_calls) == 2
