"""ToolSpec and the executor contract (ARCHITECTURE.md §3.1).

A spec carries name, description, JSON Schema, executor callable, tier
(read | propose | verify), and timeout. Errors return as structured payloads and
never raise into the loop (I2).
"""
