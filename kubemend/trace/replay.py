"""Trace replay (ARCHITECTURE.md §7).

Reconstructs a run's event sequence from its JSONL, so an interesting failure
can be turned into a unit fixture or a new scenario. The round-trip test —
record then replay yields an identical event sequence — is what keeps the format
honest as event types are added.
"""
