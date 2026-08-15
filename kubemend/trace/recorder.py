"""JSONL trace recorder (ARCHITECTURE.md §7).

Writes `traces/<run_id>.jsonl`: a run header (config hash, model names), one
event per model turn with token counts and cost, one per tool call with
arguments, truncated payload, raw_bytes and duration, then verdicts and the
final result.

Events are plain JSON-safe dicts rather than dataclasses, because the round-trip
guarantee (record then replay yields an identical sequence) is what lets a
failed run become a permanent fixture. Anything that does not survive
`json.dumps` / `json.loads` unchanged does not belong in an event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kubemend.config import RunConfig
from kubemend.core.model import (
    HandoffReport,
    ModelTier,
    RunResult,
    Task,
    ToolCall,
    ToolOutcome,
    Verdict,
)
from kubemend.llm.client import LLMResponse

Event = dict[str, Any]


def _verdict_dict(verdict: Verdict) -> Event:
    return {
        "passed": verdict.passed,
        "checks": [
            {"name": c.name, "passed": c.passed, "detail": c.detail} for c in verdict.checks
        ],
        "diff_summary": (
            [list(r) for r in verdict.diff_summary.resources] if verdict.diff_summary else None
        ),
    }


def _handoff_dict(handoff: HandoffReport) -> Event:
    return {
        "root_cause_hypotheses": [
            {"statement": h.statement, "confidence": h.confidence, "evidence": list(h.evidence)}
            for h in handoff.root_cause_hypotheses
        ],
        "what_was_ruled_out": list(handoff.what_was_ruled_out),
        "suggested_next_steps": list(handoff.suggested_next_steps),
        "blocking_reason": handoff.blocking_reason,
    }


@dataclass
class TraceRecorder:
    path: Path
    events: list[Event] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path | str) -> TraceRecorder:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
        return cls(path=target)

    def emit(self, event: Event) -> None:
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    # -- event kinds ------------------------------------------------------

    def run_header(self, task: Task, cfg: RunConfig) -> None:
        self.emit(
            {
                "type": "run_header",
                "task": task.statement,
                "namespace": task.scope.namespace,
                "app": task.scope.app,
                "window": task.window,
                "model_main": cfg.model.main.name,
                "model_cheap": cfg.model.cheap.name,
                "max_iterations": cfg.budgets.max_iterations,
                "max_cost_usd": cfg.model.main.max_cost_usd_per_run,
                "max_wall_seconds": cfg.budgets.max_wall_seconds,
            }
        )

    def model_turn(self, response: LLMResponse, *, tier: ModelTier, cost_usd: float) -> None:
        self.emit(
            {
                "type": "model_turn",
                "tier": tier,
                "model": response.model,
                "text": response.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in response.tool_calls
                ],
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "cached_input_tokens": response.usage.cached_input_tokens,
                    "cache_creation_tokens": response.usage.cache_creation_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                "cost_usd": cost_usd,
            }
        )

    def tool_call(self, call: ToolCall, outcome: ToolOutcome) -> None:
        self.emit(
            {
                "type": "tool_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "ok": outcome.ok,
                "payload": outcome.payload,
                "truncated": outcome.truncated,
                "raw_bytes": outcome.raw_bytes,
                "duration_ms": outcome.duration_ms,
            }
        )

    def nudge(self, text: str, call: ToolCall) -> None:
        self.emit({"type": "nudge", "text": text, "name": call.name, "arguments": call.arguments})

    def verdict(self, verdict: Verdict) -> None:
        self.emit({"type": "verdict", **_verdict_dict(verdict)})

    def handoff(self, handoff: HandoffReport) -> None:
        self.emit({"type": "handoff", **_handoff_dict(handoff)})

    def result(self, result: RunResult) -> None:
        self.emit(
            {
                "type": "result",
                "success": result.success,
                "reason": result.reason,
                "pr_ref": result.pr_ref,
                "cost_usd": result.cost_usd,
                "iterations": result.iterations,
                "wall_seconds": result.wall_seconds,
            }
        )
