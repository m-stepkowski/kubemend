"""Prometheus provider (ARCHITECTURE.md §3.2, tool contract `query_metrics`).

Range queries against `/api/v1/query_range`, downsampled by stride to at most
`max_points` per series. An empty result is a hint, not an error — the model
gets told no series matched so it can fix its selector.
"""
