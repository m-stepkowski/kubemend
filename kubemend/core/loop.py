"""The agent loop (ARCHITECTURE.md §2.2).

One function: call the model, execute the tool calls it asks for, and when it
stops asking, hand the run to the verification gate rather than believing the
claim (I1). Budgets bound it (I4); the loop detector stops repetition; any
non-verified termination produces a handoff report.

This module stays small and boring on purpose — it is the artifact the project
is judged by. If it grows past ~150 lines, that is a signal to have a design
discussion, not to keep appending.
"""

from __future__ import annotations

from kubemend.config import RunConfig
from kubemend.core.budget import Budget
from kubemend.core.context import Context
from kubemend.core.handoff import request_handoff
from kubemend.core.loop_detector import LoopDetector
from kubemend.core.model import HandoffReport, ModelTier, RunResult, Task, TerminationReason
from kubemend.llm.client import LLMClient, LLMError, LLMResponse
from kubemend.prompts import render
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.cost import load_pricing
from kubemend.trace.meter import MeteredLLM
from kubemend.trace.recorder import TraceRecorder
from kubemend.verify.gate import VerificationGate

# Consecutive completion claims tolerated after a failed verdict before the
# run is handed off. Mirrors LoopDetector.abort_after: the model gets the
# failure back, one genuine retry, and then the run stops.
MAX_BARREN_CLAIMS = 3


def run(
    task: Task,
    cfg: RunConfig,
    *,
    llm: LLMClient,
    registry: ToolRegistry,
    gate: VerificationGate,
    trace: TraceRecorder,
) -> RunResult:
    """Drive one incident to a verified proposal or a structured handoff."""
    # Per-model override; falls back to the config-owned global (harness-design.md).
    window = cfg.model.main.window_tokens or cfg.context.model_window_tokens
    context_cfg = cfg.context.model_copy(update={"model_window_tokens": window})
    ctx = Context(system=render("system.md.j2", task=task), task=task, config=context_cfg)
    budget = Budget(
        max_iterations=cfg.budgets.max_iterations,
        max_cost_usd=cfg.model.main.max_cost_usd_per_run,
        max_wall_seconds=cfg.budgets.max_wall_seconds,
    )
    detector = LoopDetector()
    barren_claims = 0
    trace.run_header(task, cfg)

    def _record(response: LLMResponse, tier: ModelTier, cost: float) -> None:
        budget.charge_usd(cost)
        trace.model_turn(response, tier=tier, cost_usd=cost)

    llm = MeteredLLM(llm, cfg, load_pricing(cfg.model.pricing_table), record=_record)
    try:
        while True:
            if budget.exhausted() is not None:
                return _handoff(ctx, llm, trace, budget, reason="budget_exhausted")

            response = llm.call(ctx.render(), tools=registry.schemas(), tier="main")
            budget.tick()

            if response.tool_calls:
                for call in response.tool_calls:
                    if (nudge := detector.observe(call)) is not None:
                        # Repeat of the previous call: nudge instead of re-running it.
                        ctx.append_system_nudge(nudge)
                        trace.nudge(nudge, call)
                        if detector.should_abort():
                            return _handoff(ctx, llm, trace, budget, reason="loop_detected")
                        continue
                    barren_claims = 0  # real work happened; the streak resets
                    outcome = registry.execute(call)
                    ctx.append_tool_exchange(call, outcome)
                    trace.tool_call(call, outcome)
                ctx.maybe_compact(llm)
                continue

            # No tool calls means the model claims completion. Never trust the claim.
            if not registry.has_write_path():
                # Nothing can propose a change, so no verdict could ever pass and
                # re-prompting only burns budget. Read-only runs end here, in the
                # designed outcome: a handoff.
                return _handoff(ctx, llm, trace, budget, reason="handoff")

            verdict = gate.verify()
            trace.verdict(verdict)
            if verdict.passed:
                result = RunResult(
                    success=True,
                    reason="verified",
                    verdict=verdict,
                    cost_usd=budget.cost_usd,
                    iterations=budget.iterations,
                    wall_seconds=budget.elapsed_seconds,
                    trace_path=trace.path,
                )
                trace.result(result)
                return result

            ctx.append_verification_failure(verdict)
            # A claim that follows a failed verdict without any intervening tool
            # call is the model repeating itself, and the loop detector cannot see
            # it because there are no tool calls to compare. Left unchecked this
            # spends the whole budget re-asserting the same thing: one real run
            # burned 12 of 15 iterations that way.
            barren_claims += 1
            if barren_claims >= MAX_BARREN_CLAIMS:
                return _handoff(ctx, llm, trace, budget, reason="loop_detected")
    except LLMError as exc:
        # The model call failed (auth, connectivity, ...); not routed through
        # _handoff, which would ask the same unreachable model to summarize.
        return _fatal_error(trace, budget, exc)


def _handoff(
    ctx: Context, llm: LLMClient, trace: TraceRecorder, budget: Budget, *, reason: TerminationReason
) -> RunResult:
    """Every non-verified exit goes through here, so no run ends silently."""
    return _finish(trace, budget, reason=reason, handoff=request_handoff(ctx, llm, reason=reason))


def _fatal_error(trace: TraceRecorder, budget: Budget, exc: LLMError) -> RunResult:
    report = HandoffReport(blocking_reason=f"llm_error: {exc}")
    return _finish(trace, budget, reason="fatal_error", handoff=report)


def _finish(
    trace: TraceRecorder, budget: Budget, *, reason: TerminationReason, handoff: HandoffReport
) -> RunResult:
    trace.handoff(handoff)
    result = RunResult(
        success=False,
        reason=reason,
        handoff=handoff,
        cost_usd=budget.cost_usd,
        iterations=budget.iterations,
        wall_seconds=budget.elapsed_seconds,
        trace_path=trace.path,
    )
    trace.result(result)
    return result
