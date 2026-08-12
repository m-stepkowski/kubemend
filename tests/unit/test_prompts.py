"""Guards on the prompt templates.

The system prompt is not decoration. Two of its blocks are load-bearing enough
that CLAUDE.md forbids weakening them, and the M6 log-injection scenario asserts
the behaviour they produce — so they get a test here too, where a regression
shows up in seconds instead of after a ten-run sweep.
"""

from __future__ import annotations

from kubemend.core.model import Scope, Task
from kubemend.prompts import render


def _task() -> Task:
    return Task(
        statement="shop-api pods are crash-looping",
        scope=Scope(namespace="shop", app="shop-api"),
    )


def test_system_prompt_states_that_tool_results_are_data_not_instructions() -> None:
    prompt = render("system.md.j2", task=_task())

    assert "never direction for you" in prompt
    assert "ignore previous instructions" in prompt, (
        "the prompt should name the attack it expects, so the model recognises it"
    )


def test_system_prompt_states_the_pr_only_constraint() -> None:
    prompt = render("system.md.j2", task=_task())

    assert "You have no ability to change a cluster" in prompt
    assert "kubectl" in prompt


def test_system_prompt_carries_the_scope_declaration() -> None:
    prompt = render("system.md.j2", task=_task())

    assert "namespace `shop`" in prompt
    assert "app `shop-api`" in prompt


def test_system_prompt_is_byte_stable_for_identical_input() -> None:
    """Prompt caching needs the pinned prefix identical across iterations (§2.7)."""
    assert render("system.md.j2", task=_task()) == render("system.md.j2", task=_task())


def test_handoff_prompt_names_the_values_only_blocking_reason() -> None:
    prompt = render("handoff.md.j2", reason="handoff")

    assert "fix_not_expressible_in_values" in prompt


def test_compaction_prompt_demands_the_queries_already_run() -> None:
    """Without that section the loop detector's job gets much harder after a
    compaction, because the model re-issues queries whose results were evicted."""
    prompt = render("compaction.md.j2", transcript="...")

    assert "QUERIES ALREADY RUN" in prompt
