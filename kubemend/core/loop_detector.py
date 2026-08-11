"""Repeated-tool-call detector (ARCHITECTURE.md §2.5).

Signature is `(name, canonical_json(arguments))`. Two consecutive identical
signatures inject a nudge and skip execution; three abort to handoff. The
signature memory is held out-of-band rather than in context, so it survives
compaction.
"""
