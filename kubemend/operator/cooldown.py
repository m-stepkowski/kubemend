"""In-process cooldown guardrail (docs/knowledge/operator-design.md).

In-memory only: resets on operator restart, so a crash-loop during an alert
storm defeats it — documented, not solved, in v1 (docs/threat-model.md §11).
"""

from __future__ import annotations

import threading


class CooldownTracker:
    """One lock guards the dict only; a slow caller never blocks a different key.

    `try_acquire` is the sole entry point so the check-then-set is atomic —
    two threads racing on the same key must never both win.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_triggered: dict[tuple[str, str], float] = {}

    def try_acquire(self, key: tuple[str, str], now: float, window_s: float) -> bool:
        with self._lock:
            last = self._last_triggered.get(key)
            if last is not None and now - last < window_s:
                return False
            self._last_triggered[key] = now
            return True
