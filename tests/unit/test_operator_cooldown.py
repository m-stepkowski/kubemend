"""Cooldown guardrail (docs/knowledge/operator-design.md)."""

from __future__ import annotations

import threading

from kubemend.operator.cooldown import CooldownTracker


def test_first_acquire_for_a_key_succeeds() -> None:
    tracker = CooldownTracker()

    assert tracker.try_acquire(("shop", "shop-api"), now=100.0, window_s=60.0) is True


def test_second_acquire_within_the_window_is_refused() -> None:
    tracker = CooldownTracker()
    tracker.try_acquire(("shop", "shop-api"), now=100.0, window_s=60.0)

    assert tracker.try_acquire(("shop", "shop-api"), now=130.0, window_s=60.0) is False


def test_acquire_after_the_window_elapses_succeeds() -> None:
    tracker = CooldownTracker()
    tracker.try_acquire(("shop", "shop-api"), now=100.0, window_s=60.0)

    assert tracker.try_acquire(("shop", "shop-api"), now=161.0, window_s=60.0) is True


def test_different_keys_do_not_share_a_cooldown() -> None:
    tracker = CooldownTracker()
    tracker.try_acquire(("shop", "shop-api"), now=100.0, window_s=60.0)

    assert tracker.try_acquire(("shop", "shop-worker"), now=100.0, window_s=60.0) is True


def test_concurrent_acquires_for_the_same_key_only_one_wins() -> None:
    """The lock must make check-then-set atomic — two threads racing on the
    same key must never both succeed."""
    tracker = CooldownTracker()
    key = ("shop", "shop-api")
    results: list[bool] = []
    results_lock = threading.Lock()

    def attempt() -> None:
        won = tracker.try_acquire(key, now=100.0, window_s=60.0)
        with results_lock:
            results.append(won)

    threads = [threading.Thread(target=attempt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
