"""Context assembly, truncation, and compaction (ARCHITECTURE.md §2.3-2.4).

Renders the fixed message order (pinned system, pinned task + scope, compacted
findings, live tail, latest verification failure) and keeps that prefix
byte-stable so prompt caching survives across iterations.

Compaction loses detail on purpose: raw tool payloads are recoverable by calling
the tool again, an unbounded context bill is not recoverable at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from kubemend.config import ContextConfig
from kubemend.core.model import Task, ToolCall, ToolOutcome, Verdict
from kubemend.llm.client import LLMClient, Message
from kubemend.prompts import render

# Rough bytes-per-token for budgeting. Deliberately an estimate: the real count
# needs a tokenizer round-trip per call, and the only decisions made from this
# number (truncate, compact) are threshold checks with slack on either side.
BYTES_PER_TOKEN = 4

SUMMARY_HEADER = "SUMMARY OF EARLIER INVESTIGATION (raw data evicted; re-query if needed):"

KEEP_RECENT_EXCHANGES = 2


def estimate_tokens(text: str) -> int:
    return len(text) // BYTES_PER_TOKEN


@dataclass
class Exchange:
    call: ToolCall
    outcome: ToolOutcome

    def render(self) -> list[Message]:
        arguments = json.dumps(self.call.arguments, sort_keys=True)
        payload = json.dumps(self.outcome.payload, sort_keys=True)
        return [
            Message("assistant", f"tool_call {self.call.name}({arguments})"),
            Message("user", f"tool_result {self.call.name}: {payload}"),
        ]


@dataclass
class Nudge:
    text: str

    def render(self) -> list[Message]:
        return [Message("system", self.text)]


Entry = Exchange | Nudge


@dataclass
class Context:
    system: str
    task: Task
    config: ContextConfig = field(default_factory=ContextConfig)
    _entries: list[Entry] = field(default_factory=list, repr=False)
    _summary: str | None = field(default=None, repr=False)
    _failure: str | None = field(default=None, repr=False)

    # -- mutation ---------------------------------------------------------

    def append_tool_exchange(self, call: ToolCall, outcome: ToolOutcome) -> None:
        self._entries.append(Exchange(call, outcome))

    def append_system_nudge(self, text: str) -> None:
        self._entries.append(Nudge(text))

    def append_verification_failure(self, verdict: Verdict) -> None:
        """Store the gate's verdict verbatim and check-by-check.

        Specificity is what makes the retry loop converge: "kyverno:
        disallow-privileged FAILED on Deployment/shop/api" tells the model what
        to change, where a generic "validation failed" measurably stalls runs.
        """
        lines = ["VERIFICATION FAILED — the harness re-ran validation and it did not pass:"]
        for check in verdict.checks:
            status = "PASSED" if check.passed else "FAILED"
            lines.append(f"- {check.name}: {status} — {check.detail}")
        lines.append("Fix the specific check that failed; do not start over.")
        self._failure = "\n".join(lines)

    # -- rendering --------------------------------------------------------

    def _task_block(self) -> str:
        scope = self.task.scope
        return (
            f"TASK: {self.task.statement}\n"
            f"SCOPE: namespace={scope.namespace} app={scope.app} window={self.task.window}\n"
            "Propose changes only inside this scope."
        )

    def render(self) -> list[Message]:
        messages = [
            Message("system", self.system, pinned=True),
            Message("system", self._task_block(), pinned=True),
        ]
        if self._summary is not None:
            messages.append(Message("user", f"{SUMMARY_HEADER}\n{self._summary}"))
        for entry in self._entries:
            messages.extend(entry.render())
        if self._failure is not None:
            messages.append(Message("user", self._failure))
        return messages

    def rendered_tokens(self) -> int:
        return estimate_tokens("\n".join(m.content for m in self.render()))

    # -- compaction -------------------------------------------------------

    def should_compact(self) -> bool:
        threshold = self.config.compact_threshold * self.config.model_window_tokens
        return self.rendered_tokens() > threshold

    def maybe_compact(self, llm: LLMClient) -> None:
        if self.should_compact():
            self.compact(llm)

    def compact(self, llm: LLMClient) -> None:
        """Replace the oldest half of the exchanges with a model-written summary.

        Never evicted: the system prompt, the task, the most recent two
        exchanges, and the latest verification failure. The first two the loop
        cannot reconstruct; the last two are what the model is actively working
        from.
        """
        evicted, kept = self._split_for_compaction()
        if not evicted:
            return

        transcript = "\n".join(m.content for entry in evicted for m in entry.render())
        if self._summary is not None:
            transcript = f"{SUMMARY_HEADER}\n{self._summary}\n{transcript}"

        response = llm.call(
            [Message("user", render("compaction.md.j2", transcript=transcript))],
            tools=[],
            tier="cheap",
        )
        self._summary = response.text.strip()
        self._entries = kept

    def _split_for_compaction(self) -> tuple[list[Entry], list[Entry]]:
        exchange_positions = [i for i, e in enumerate(self._entries) if isinstance(e, Exchange)]
        total = len(exchange_positions)
        evictable = max(0, total - KEEP_RECENT_EXCHANGES)
        to_evict = min(total // 2, evictable)
        if to_evict == 0:
            return [], self._entries
        cut = exchange_positions[to_evict - 1]
        return self._entries[: cut + 1], self._entries[cut + 1 :]
