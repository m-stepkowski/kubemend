"""Payload redaction (ARCHITECTURE.md §3.3, invariant I3).

Applied inside the executor wrapper, after execution and before truncation, to
every payload from every tool. Pod-spec env values become `<redacted:NAME>`
unless allow-listed; a regex pass masks bearer tokens, AWS keys, PEM blocks, and
connection-string passwords.

Secret values are structurally impossible here because they are never fetched —
not even to redact them.
"""
