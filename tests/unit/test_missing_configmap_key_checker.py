"""Regression test for the missing-configmap-key checker's false negative.

The first live sweep marked 3 of 5 correctly-diagnosed, gate-verified runs as
failed because the checker demanded a *non-empty* FEATURE_FLAGS value.
configMapKeyRef only requires the key to be present — an empty string starts
the container fine — so requiring non-empty was checking the checker's own
opinion, not the property that actually determines whether the fix works.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from evals.models import CheckReport
from kubemend.core.model import CheckResult, DiffSummary, RunResult, Verdict

CHECKER_PATH = Path("lab/scenarios/missing-configmap-key/checker.py")

PASSING_VERDICT = Verdict(
    passed=True,
    checks=[CheckResult("helm_template", True, "rendered 1 app(s)")],
    diff_summary=DiffSummary(resources=[("ConfigMap", "shop", "shop-api-config")]),
)


def _load_checker() -> object:
    spec = importlib.util.spec_from_file_location(
        "missing_configmap_key_checker_test", CHECKER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


class FakeLab:
    def __init__(self, values_yaml: str) -> None:
        self._values_yaml = values_yaml

    def read_file(self, rel_path: str) -> str:
        assert rel_path == "apps/shop-api/values.yaml"
        return self._values_yaml


def _result() -> RunResult:
    return RunResult(success=True, reason="verified", verdict=PASSING_VERDICT)


def test_an_empty_string_value_is_a_valid_fix_not_a_failure() -> None:
    """The exact case the first sweep got wrong: FEATURE_FLAGS: "" starts the
    container fine, since configMapKeyRef only needs the key present."""
    check = _load_checker()
    lab = FakeLab('config:\n  FEATURE_FLAGS: ""\n')

    report = check(_result(), lab)  # type: ignore[operator]

    assert isinstance(report, CheckReport)
    assert report.passed is True


def test_a_real_value_still_passes() -> None:
    check = _load_checker()
    lab = FakeLab('config:\n  FEATURE_FLAGS: "checkout_v2=on"\n')

    report = check(_result(), lab)  # type: ignore[operator]

    assert report.passed is True


def test_a_missing_key_still_fails() -> None:
    """The property that actually matters: the key itself must be present."""
    check = _load_checker()
    lab = FakeLab("config: {}\n")

    report = check(_result(), lab)  # type: ignore[operator]

    assert report.passed is False
    assert "missing" in report.detail


def test_no_config_section_at_all_still_fails() -> None:
    check = _load_checker()
    lab = FakeLab("replicaCount: 2\n")

    report = check(_result(), lab)  # type: ignore[operator]

    assert report.passed is False
