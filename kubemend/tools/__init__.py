"""Tool layer (ARCHITECTURE.md §3).

The security boundary. Executors are pure `args -> payload`; the registry
wrapper around them applies schema validation, timeouts, the retry rule,
redaction, and truncation. Schemas are contracts — see
docs/knowledge/tool-contracts.md before changing one.
"""
