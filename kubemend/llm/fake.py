"""Scripted FakeLLM for unit tests (ARCHITECTURE.md §2.7).

Replays a fixed list of turns — tool calls, text, usage — so every loop
behaviour in M1 (truncation, compaction, loop detection, budget exhaustion,
retry policy, the verification-failure path) is exercised deterministically and
offline.
"""
