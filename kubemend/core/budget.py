"""Run budgets (ARCHITECTURE.md §2.2, invariant I4).

Three independent limits — iterations, USD, wall-clock — checked at the top of
every loop turn. Whichever trips first names the run's termination `reason`.

Naming the limit matters downstream: an eval report that says "12 runs stopped"
is noise, one that says "12 runs hit max_iterations" points at a prompt problem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

BudgetLimit = Literal["max_iterations", "max_cost_usd", "max_wall_seconds"]


@dataclass
class Budget:
    max_iterations: int
    max_cost_usd: float
    max_wall_seconds: float
    iterations: int = 0
    cost_usd: float = 0.0
    _started_at: float = field(default_factory=time.monotonic, repr=False)

    def tick(self) -> None:
        self.iterations += 1

    def charge_usd(self, amount: float) -> None:
        self.cost_usd += amount

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    def exhausted(self) -> BudgetLimit | None:
        """Return the limit that has been reached, or None to keep going."""
        if self.iterations >= self.max_iterations:
            return "max_iterations"
        if self.cost_usd >= self.max_cost_usd:
            return "max_cost_usd"
        if self.elapsed_seconds >= self.max_wall_seconds:
            return "max_wall_seconds"
        return None
