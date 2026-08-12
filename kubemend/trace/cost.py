"""Token-to-USD accounting (ARCHITECTURE.md §7, §2.7).

Converts per-call usage — input, cached-input, output — into dollars via
config/pricing.yaml. Cached input is priced separately; getting that wrong makes
every cost number in the eval report wrong, so these figures get checked against
a real invoice once in M1.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from kubemend.llm.client import Usage


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens."""

    input: float
    output: float
    cache_write_5m: float = 0.0
    cache_read: float = 0.0


# An unknown model must never look free. A zero rate would silently disable the
# cost budget — the one guardrail that makes a runaway loop structurally
# impossible — so we fall back to a frontier-tier price and over-report instead.
FALLBACK_PRICE = ModelPrice(input=3.00, output=15.00, cache_write_5m=3.75, cache_read=0.30)


def load_pricing(path: Path | str) -> dict[str, ModelPrice]:
    """Read the pricing table. A missing or malformed file yields an empty table
    and therefore the fallback price, never a crash mid-run.
    """
    source = Path(path)
    if not source.exists():
        return {}
    try:
        raw = yaml.safe_load(source.read_text()) or {}
        models = raw.get("models", {})
    except yaml.YAMLError:
        return {}
    return {
        name: ModelPrice(
            input=float(entry.get("input", FALLBACK_PRICE.input)),
            output=float(entry.get("output", FALLBACK_PRICE.output)),
            cache_write_5m=float(entry.get("cache_write_5m", 0.0)),
            cache_read=float(entry.get("cache_read", 0.0)),
        )
        for name, entry in models.items()
    }


def price_for(model: str, pricing: dict[str, ModelPrice]) -> ModelPrice:
    return pricing.get(model, FALLBACK_PRICE)


def usd(usage: Usage, price: ModelPrice) -> float:
    """Cost of one call. Cached input is billed at the cache-read rate, which is
    roughly a tenth of fresh input — on this workload most input is a cache read,
    so charging it at full rate would overstate every figure in the report.
    """
    return (
        usage.input_tokens * price.input
        + usage.cached_input_tokens * price.cache_read
        + usage.output_tokens * price.output
    ) / 1_000_000
