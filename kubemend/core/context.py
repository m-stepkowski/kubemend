"""Context assembly, truncation, and compaction (ARCHITECTURE.md §2.3-2.4).

Renders the fixed message order (pinned system, pinned task + scope, compacted
findings, live tail, latest verification failure) and keeps that prefix
byte-stable so prompt caching survives across iterations.

Truncation keeps head 60% / tail 40% of the per-result cap with a splice marker
naming `raw_bytes` — errors cluster at both ends of a log window, and the marker
teaches the model to re-query narrower instead of giving up.
"""
