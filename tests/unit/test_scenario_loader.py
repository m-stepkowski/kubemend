"""Scenario loading (ARCHITECTURE.md §7, docs/knowledge/lab-and-evals.md).

Loads every real scenario under lab/scenarios/ rather than a synthetic fixture:
the point of this test is that the six v0.1 scenarios themselves stay loadable
as the loader evolves, not just that the loader works on made-up input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.models import CheckReport
from evals.scenario import SCENARIOS_ROOT, list_scenarios, load_scenario
from kubemend.core.model import RunResult

EXPECTED_SCENARIOS = {
    "bad-env-endpoint",
    "bad-image-tag",
    "bad-probe-path",
    "missing-configmap-key",
    "oom-limit",
    "quota-conflict",
    "fix-needs-template-change",
    "scope-trap",
    "log-injection",
}

NEGATIVE_SCENARIOS = {"fix-needs-template-change", "scope-trap", "log-injection"}

# M11: split mode's own app/namespace (shop-api-split / shop-split, a separate
# gitea chart repo) don't match the shared shop-api/shop-worker/shop shape the
# nine v0.1 scenarios assert on uniformly, so it gets its own set and its own
# assertions below rather than folding into EXPECTED_SCENARIOS's parametrized
# tests. It's also excluded from evals/runner.py's implicit "all" set (tagged
# "split-mode") since it needs gitops.chart_repos configured — see
# kubemend.split-mode.yaml.
SPLIT_MODE_SCENARIOS = {"shop-api-split-chart-repo"}

# M12: same reasoning, for the second-values-repo scenario (checkout-api /
# shop-payments, its own gitea values repo). Excluded from the implicit "all"
# set via the "multi-values" tag since it needs gitops.values_repos — see
# kubemend.multi-values.yaml.
MULTI_VALUES_SCENARIOS = {"checkout-api-values-repo"}

MODE_SCENARIOS = SPLIT_MODE_SCENARIOS | MULTI_VALUES_SCENARIOS


def test_lists_all_eleven_scenarios() -> None:
    """`list_scenarios()` is a raw directory scan — unlike `evals run -s all`,
    it makes no mode-based exclusion, so both mode-specific scenarios are in
    here."""
    assert set(list_scenarios()) == EXPECTED_SCENARIOS | MODE_SCENARIOS


@pytest.mark.parametrize("name", sorted(SPLIT_MODE_SCENARIOS))
def test_split_mode_scenario_loads_with_its_own_scope(name: str) -> None:
    spec, checker = load_scenario(name)

    assert spec.name == name
    assert spec.title
    assert spec.scope.namespace == "shop-split"
    assert spec.scope.app == "shop-api-split"
    assert spec.task_prompt
    assert spec.expected_outcome == "pr"
    assert spec.symptom_probe.timeout_s > 0
    assert "split-mode" in spec.tags
    assert callable(checker)


@pytest.mark.parametrize("name", sorted(MODE_SCENARIOS))
def test_mode_specific_scenario_has_a_break_patch(name: str) -> None:
    assert (SCENARIOS_ROOT / name / "break.patch").is_file()


@pytest.mark.parametrize("name", sorted(MULTI_VALUES_SCENARIOS))
def test_multi_values_scenario_loads_with_its_own_scope(name: str) -> None:
    spec, checker = load_scenario(name)

    assert spec.name == name
    assert spec.title
    assert spec.scope.namespace == "shop-payments"
    assert spec.scope.app == "checkout-api"
    assert spec.task_prompt
    assert spec.expected_outcome == "pr"
    assert spec.symptom_probe.timeout_s > 0
    assert "multi-values" in spec.tags
    assert callable(checker)


def test_missing_root_lists_nothing_rather_than_raising(tmp_path: Path) -> None:
    assert list_scenarios(tmp_path / "does-not-exist") == []


@pytest.mark.parametrize("name", sorted(EXPECTED_SCENARIOS))
def test_every_scenario_loads_with_a_scope_prompt_and_probe(name: str) -> None:
    spec, checker = load_scenario(name)

    assert spec.name == name
    assert spec.title
    assert spec.scope.namespace == "shop"
    assert spec.scope.app in ("shop-api", "shop-worker")
    assert spec.task_prompt
    expected = "handoff" if name in NEGATIVE_SCENARIOS - {"log-injection"} else "pr"
    assert spec.expected_outcome == expected
    assert spec.symptom_probe.timeout_s > 0
    assert callable(checker)


@pytest.mark.parametrize("name", sorted(EXPECTED_SCENARIOS))
def test_every_scenario_has_a_break_patch(name: str) -> None:
    assert (SCENARIOS_ROOT / name / "break.patch").is_file()


def test_missing_scenario_yaml_is_a_named_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"scenario\.yaml"):
        load_scenario("nonexistent", tmp_path)


def test_missing_checker_is_a_named_error(tmp_path: Path) -> None:
    directory = tmp_path / "half-scenario"
    directory.mkdir()
    (directory / "scenario.yaml").write_text(
        "title: t\nscope: {namespace: shop, app: shop-api}\ntask_prompt: p\n"
        "expected_outcome: pr\nsymptom_probe: {kind: event_reason, value: X}\n"
    )
    with pytest.raises(FileNotFoundError, match=r"checker\.py"):
        load_scenario("half-scenario", tmp_path)


def test_checker_missing_check_function_is_a_named_error(tmp_path: Path) -> None:
    directory = tmp_path / "bad-checker"
    directory.mkdir()
    (directory / "scenario.yaml").write_text(
        "title: t\nscope: {namespace: shop, app: shop-api}\ntask_prompt: p\n"
        "expected_outcome: pr\nsymptom_probe: {kind: event_reason, value: X}\n"
    )
    (directory / "checker.py").write_text("def not_check(): pass\n")
    with pytest.raises(AttributeError, match="check"):
        load_scenario("bad-checker", tmp_path)


def test_loaded_checker_is_actually_callable_end_to_end(tmp_path: Path) -> None:
    """A loaded checker must be a real function of (RunResult, LabHandle),
    not merely importable."""
    directory = tmp_path / "trivial"
    directory.mkdir()
    (directory / "scenario.yaml").write_text(
        "title: t\nscope: {namespace: shop, app: shop-api}\ntask_prompt: p\n"
        "expected_outcome: pr\nsymptom_probe: {kind: event_reason, value: X, timeout_s: 5}\n"
    )
    (directory / "checker.py").write_text(
        "from evals.models import CheckReport\n"
        "def check(result, lab):\n"
        "    return CheckReport(True, 'ok')\n"
    )
    _spec, checker = load_scenario("trivial", tmp_path)

    result = checker(RunResult(success=True, reason="verified"), object())  # type: ignore[arg-type]
    assert isinstance(result, CheckReport)
    assert result.passed is True


def test_two_scenarios_checkers_do_not_collide_as_modules() -> None:
    """Each checker.py is loaded under a name unique per scenario, so two
    scenarios both defining `check` never shadow one another."""
    _spec_a, checker_a = load_scenario("bad-image-tag")
    _spec_b, checker_b = load_scenario("oom-limit")

    assert checker_a is not checker_b
    assert checker_a.__module__ != checker_b.__module__
