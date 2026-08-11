"""ObservabilityProvider Protocol (ARCHITECTURE.md §3.2).

Two methods — `query_metrics` and `search_logs` — over provider-neutral query
and result types. This is one of the three seams (with GitBackend and LLMClient)
where the project grows later without touching kubemend/core.
"""
