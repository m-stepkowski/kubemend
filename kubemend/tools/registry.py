"""Tool registry and executor wrapper (ARCHITECTURE.md §3.1).

Every tool call goes through one path: validate arguments against the JSON
Schema, execute with a per-tool timeout, apply the I2 retry rule (exactly one
retry for transport-class failures, never for 4xx), redact, truncate, time, and
emit a trace event.

Redaction living in this wrapper rather than in the tools is what makes I3
structural: no future tool can bypass it.
"""
