"""Core data model (ARCHITECTURE.md §2.1).

Frozen dataclasses passed between the loop, the tool layer, and the gate:
ToolCall, ToolOutcome, CheckResult, DiffSummary, Verdict, HandoffReport, and the
mutable RunResult the CLI and eval runner consume.
"""
