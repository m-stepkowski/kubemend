"""The agent loop (ARCHITECTURE.md §2.2).

One function: call the model, execute the tool calls it asks for, and when it
stops asking, hand the run to the verification gate rather than believing the
claim (I1). Budgets bound it (I4); the loop detector stops repetition; any
non-verified termination produces a handoff report.

This module stays small and boring on purpose — it is the artifact the project
is judged by. If it grows past ~150 lines, that is a signal to have a design
discussion, not to keep appending.
"""
