"""Graceful handoff report (ARCHITECTURE.md §2.6).

On any non-verified termination, one final cheap-tier call with no tools
produces root-cause hypotheses with evidence refs, what was ruled out, suggested
next steps, and a `blocking_reason`. A good handoff is a designed outcome and
eval material, not a failure path.
"""
