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
from kubemend.core.model import RunResult, Task, TerminationReason
from kubemend.llm.client import LLMClient
from kubemend.prompts import render
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.cost import load_pricing, price_for, usd
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
    pricing = load_pricing(cfg.model.pricing_table)
    price = price_for(cfg.model.main.name, pricing)
    ctx = Context(system=render("system.md.j2", task=task), task=task, config=cfg.context)
    budget = Budget(
        max_iterations=cfg.budgets.max_iterations,
        max_cost_usd=cfg.model.main.max_cost_usd_per_run,
        max_wall_seconds=cfg.budgets.max_wall_seconds,
    )
    detector = LoopDetector()
    barren_claims = 0
    trace.run_header(task, cfg)

    while True:
        if budget.exhausted() is not None:
            return _handoff(ctx, llm, trace, budget, reason="budget_exhausted")

        response = llm.call(ctx.render(), tools=registry.schemas(), tier="main")
        cost = usd(response.usage, price)
        budget.charge_usd(cost)
        budget.tick()
        trace.model_turn(response, tier="main", cost_usd=cost)

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


def _handoff(
    ctx: Context,
    llm: LLMClient,
    trace: TraceRecorder,
    budget: Budget,
    *,
    reason: TerminationReason,
) -> RunResult:
    """Every non-verified exit goes through here, so no run ends silently."""
    report = request_handoff(ctx, llm, reason=reason)
    trace.handoff(report)
    result = RunResult(
        success=False,
        reason=reason,
        handoff=report,
        cost_usd=budget.cost_usd,
        iterations=budget.iterations,
        wall_seconds=budget.elapsed_seconds,
        trace_path=trace.path,
    )
    trace.result(result)
    return result
