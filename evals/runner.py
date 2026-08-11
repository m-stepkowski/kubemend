"""N-run sweeps and reports (ARCHITECTURE.md §7).

Runs each scenario N times through the reset -> break -> wait-for-symptom ->
agent run -> check -> reset protocol, then emits report.md and report.json with
pass rate, mean iterations, mean cost, and p95 wall per scenario.

Non-determinism is the point: no conclusion is drawn from n=1. See
docs/knowledge/lab-and-evals.md for the triage rules — a scenario below 50%
gets a written diagnosis before anyone touches a prompt.
"""
