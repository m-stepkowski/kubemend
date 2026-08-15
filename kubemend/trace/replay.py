"""Trace replay (ARCHITECTURE.md §7).

Reconstructs a run's event sequence from its JSONL, so an interesting failure
can be turned into a unit fixture or a new scenario. The round-trip test —
record then replay yields an identical event sequence — is what keeps the format
honest as event types are added.
"""

from __future__ import annotations

import json
from pathlib import Path

from kubemend.trace.recorder import Event


def replay(path: Path | str) -> list[Event]:
    """Reconstruct the event sequence a run recorded."""
    source = Path(path)
    if not source.exists():
        return []
    return [
        json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
