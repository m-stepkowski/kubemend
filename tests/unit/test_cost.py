"""Token-to-USD accounting (ARCHITECTURE.md §2.7, §7).

`trace/cost.py` had zero direct test coverage before this file — only
indirect exercise via `Budget.charge_usd`. Getting the additive formula or
the fallback wrong makes every cost figure in every eval report wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kubemend.llm.client import Usage
from kubemend.trace.cost import FALLBACK_PRICE, ModelPrice, load_pricing, price_for, usd


def test_usd_sums_all_four_terms_additively() -> None:
    price = ModelPrice(input=3.00, output=15.00, cache_write_5m=3.75, cache_read=0.30)
    usage = Usage(
        input_tokens=1_000_000,
        cached_input_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        output_tokens=1_000_000,
    )

    assert usd(usage, price) == pytest.approx(3.00 + 0.30 + 3.75 + 15.00)


def test_usd_of_zero_usage_is_zero() -> None:
    assert usd(Usage(), FALLBACK_PRICE) == 0.0


def test_price_for_unknown_model_falls_back_never_free() -> None:
    """A zero rate would silently disable the cost guardrail — the fallback
    exists specifically so an unrecognized model still costs something."""
    price = price_for("some-model-not-in-the-table", {})

    assert price == FALLBACK_PRICE
    assert price.input > 0
    assert price.output > 0


def test_price_for_known_model_uses_the_table_not_the_fallback() -> None:
    pricing = {"cheap-model": ModelPrice(input=0.14, output=0.28, cache_read=0.0028)}

    price = price_for("cheap-model", pricing)

    assert price.input == 0.14
    assert price != FALLBACK_PRICE


def test_price_for_an_explicit_all_zero_entry_is_honored_not_treated_as_missing() -> None:
    """A local model with a genuine $0 cost must not be silently repriced to
    the fallback — the model NAME being present is what load_pricing keys
    on, regardless of the rates being zero (see config/pricing.yaml's
    local-model-example entry)."""
    pricing = {"local-model": ModelPrice(input=0.0, output=0.0)}

    price = price_for("local-model", pricing)

    assert price.input == 0.0
    assert price.output == 0.0


def test_load_pricing_missing_file_yields_empty_table(tmp_path: Path) -> None:
    pricing = load_pricing(tmp_path / "does-not-exist.yaml")

    assert pricing == {}
    assert price_for("anything", pricing) == FALLBACK_PRICE


def test_load_pricing_malformed_yaml_yields_empty_table_not_a_crash(tmp_path: Path) -> None:
    target = tmp_path / "pricing.yaml"
    target.write_text("models:\n  broken: [this is not valid: yaml structure\n")

    pricing = load_pricing(target)

    assert pricing == {}


def test_load_pricing_missing_optional_fields_default_to_zero_not_fallback(
    tmp_path: Path,
) -> None:
    """input/output missing fall back to FALLBACK_PRICE's rate (never free);
    cache_write_5m/cache_read missing default to a plain 0.0 — the two
    halves of the table have different missing-field semantics on purpose,
    and that asymmetry is easy to get backwards when editing this function."""
    target = tmp_path / "pricing.yaml"
    target.write_text("models:\n  partial:\n    input: 2.00\n    output: 8.00\n")

    pricing = load_pricing(target)

    assert pricing["partial"].input == 2.00
    assert pricing["partial"].output == 8.00
    assert pricing["partial"].cache_write_5m == 0.0
    assert pricing["partial"].cache_read == 0.0


def test_load_pricing_reads_every_field_from_a_full_entry(tmp_path: Path) -> None:
    target = tmp_path / "pricing.yaml"
    target.write_text(
        "models:\n"
        "  full:\n"
        "    input: 5.00\n"
        "    output: 30.00\n"
        "    cache_write_5m: 6.25\n"
        "    cache_read: 0.50\n"
    )

    pricing = load_pricing(target)

    assert pricing["full"] == ModelPrice(
        input=5.00, output=30.00, cache_write_5m=6.25, cache_read=0.50
    )
