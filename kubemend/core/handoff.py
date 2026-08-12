"""Graceful handoff report (ARCHITECTURE.md §2.6).

On any non-verified termination, one final cheap-tier call with no tools
produces root-cause hypotheses with evidence refs, what was ruled out, suggested
next steps, and a `blocking_reason`. A good handoff is a designed outcome and
eval material, not a failure path.
"""

from __future__ import annotations

import json
from typing import Any

from kubemend.core.context import Context
from kubemend.core.model import HandoffReport, Hypothesis
from kubemend.llm.client import LLMClient, Message
from kubemend.prompts import render


def request_handoff(ctx: Context, llm: LLMClient, *, reason: str) -> HandoffReport:
    """Ask the cheap tier for a structured report on the way out.

    Tools are deliberately withheld: the investigation is over, and a model that
    can still call tools here will try to keep working instead of reporting.
    """
    messages = [*ctx.render(), Message("user", render("handoff.md.j2", reason=reason))]
    response = llm.call(messages, tools=[], tier="cheap")
    return parse_handoff(response.text)


def parse_handoff(text: str) -> HandoffReport:
    """Parse the model's JSON, degrading to a usable report if it is malformed.

    A run that already failed must not fail twice. If the reply is not JSON we
    keep the raw text as a next step — an unstructured handoff is still better
    than none for the human reading it.
    """
    data = _extract_json(text)
    if data is None:
        return HandoffReport(
            suggested_next_steps=[text.strip()] if text.strip() else [],
            blocking_reason="handoff_unparseable",
        )
    return HandoffReport(
        root_cause_hypotheses=[_hypothesis(h) for h in _as_list(data.get("root_cause_hypotheses"))],
        what_was_ruled_out=[str(x) for x in _as_list(data.get("what_was_ruled_out"))],
        suggested_next_steps=[str(x) for x in _as_list(data.get("suggested_next_steps"))],
        blocking_reason=data.get("blocking_reason"),
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    """Tolerate a model that wraps its JSON in prose or a code fence."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _hypothesis(raw: object) -> Hypothesis:
    if not isinstance(raw, dict):
        return Hypothesis(statement=str(raw), confidence=0.0)
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Hypothesis(
        statement=str(raw.get("statement", "")),
        confidence=confidence,
        evidence=[str(e) for e in _as_list(raw.get("evidence"))],
    )
