"""JSONL trace recorder (ARCHITECTURE.md §7).

Writes `traces/<run_id>.jsonl`: a run header (config hash, model names, git
SHAs), one event per model turn with token counts and cost, one per tool call
with arguments, truncated payload, raw_bytes and duration, then verdicts and the
final result.
"""
