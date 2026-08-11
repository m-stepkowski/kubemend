"""Loki provider (ARCHITECTURE.md §3.2, tool contract `search_logs`).

LogQL range queries against `/loki/api/v1/query_range` with the line limit
enforced executor-side. Logs are simultaneously the most likely secret leak and
the injection vector the M6 scenario attacks, so every line passes redaction.
"""
