"""Anthropic implementation of LLMClient (ARCHITECTURE.md §2.7).

Responsible for prompt-cache breakpoints (after the pinned system+task block and
after the stable conversation prefix) and for per-call usage accounting —
input, cached-input, and output tokens converted to USD via config/pricing.yaml.

The two model tiers come from config: `model.main` runs agent turns,
`model.cheap` runs compaction, handoff, and dev sweeps.
"""
