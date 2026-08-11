"""Run budgets (ARCHITECTURE.md §2.2, invariant I4).

Three independent limits — iterations, USD, wall-clock — checked at the top of
every loop turn. Whichever trips first names the run's termination `reason`.
"""
