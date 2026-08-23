"""The eval sweep: aggregation, report rendering, and the runner protocol
(ARCHITECTURE.md §7, docs/knowledge/lab-and-evals.md).

`run_sweep` is exercised end-to-end against a scripted fake `execute_incident`
(monkeypatched onto kubemend.cli, exactly where `evals.runner._run_one` looks
it up) and a fake LabHandle — no live cluster, no real model call, no real
sleep. What matters here is the *sequence* (reset -> break -> wait -> run ->
check -> reset, every iteration, isolated from the last) and that aggregation
turns per-iteration results into the right pass rate / mean / p95 numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.lab import SymptomTimeout
from evals.models import CheckReport, SymptomProbe
from evals.runner import (
    IterationResult,
    ScenarioSummary,
    _p95,
    _scenarios_for_all,
    _summarize,
    render_report_json,
    render_report_md,
    run_sweep,
)
from kubemend.config import (
    ChartReposConfig,
    GitOpsConfig,
    RunConfig,
    ValuesReposConfig,
    ValuesRepoSpec,
)
from kubemend.core.model import RunResult, Scope, Task
from kubemend.llm.client import LLMClient
from kubemend.llm.fake import FakeLLM

SCOPE = Scope(namespace="shop", app="shop-api")


# -- _scenarios_for_all (M11) --------------------------------------------


def test_split_mode_scenario_excluded_from_all_by_default() -> None:
    names = _scenarios_for_all(RunConfig())

    assert "shop-api-split-chart-repo" not in names
    assert "bad-image-tag" in names, "the nine v0.1 scenarios must be unaffected"


def test_split_mode_scenario_included_when_chart_repos_is_configured() -> None:
    cfg = RunConfig(
        gitops=GitOpsConfig(chart_repos=ChartReposConfig(url_template="https://git.corp/{app}.git"))
    )

    names = _scenarios_for_all(cfg)

    assert "shop-api-split-chart-repo" in names


def test_multi_values_scenario_excluded_from_all_by_default() -> None:
    names = _scenarios_for_all(RunConfig())

    assert "checkout-api-values-repo" not in names
    assert "bad-image-tag" in names, "the nine v0.1 scenarios must be unaffected"


def test_multi_values_scenario_included_when_values_repos_is_configured() -> None:
    cfg = RunConfig(
        gitops=GitOpsConfig(
            values_repos=ValuesReposConfig(
                repos={"platform": ValuesRepoSpec(url="https://git.corp/platform/values.git")},
                default="platform",
            )
        )
    )

    names = _scenarios_for_all(cfg)

    assert "checkout-api-values-repo" in names


def test_each_mode_filters_only_its_own_scenarios() -> None:
    """Split mode must not drag in a multi-values scenario, or vice versa —
    the two are orthogonal, and a config in one mode is not in the other."""
    split_only = RunConfig(
        gitops=GitOpsConfig(chart_repos=ChartReposConfig(url_template="https://git.corp/{app}.git"))
    )

    names = _scenarios_for_all(split_only)

    assert "shop-api-split-chart-repo" in names
    assert "checkout-api-values-repo" not in names


# -- _p95 / _summarize --------------------------------------------------


def test_p95_of_a_single_value_is_that_value() -> None:
    assert _p95([42.0]) == 42.0


def test_p95_of_empty_is_zero() -> None:
    assert _p95([]) == 0.0


def test_p95_takes_the_high_end_of_the_distribution() -> None:
    values = [float(i) for i in range(1, 101)]  # 1..100
    assert _p95(values) >= 94.0


def _iteration(
    scenario: str, *, passed: bool, iters: int = 5, cost: float = 0.1
) -> IterationResult:
    result = RunResult(success=True, reason="verified", cost_usd=cost, iterations=iters)
    check = CheckReport(passed, "ok" if passed else "property failed")
    return IterationResult(scenario, result, check, wall_seconds=10.0)


def test_summarize_counts_pass_rate_and_means() -> None:
    its = [
        _iteration("s", passed=True, iters=4, cost=0.1),
        _iteration("s", passed=False, iters=6, cost=0.3),
    ]

    summary = _summarize("s", its)

    assert summary.n == 2
    assert summary.passed == 1
    assert summary.pass_rate == 0.5
    assert summary.mean_iterations == 5.0
    assert summary.mean_cost_usd == pytest.approx(0.2)


def test_summarize_records_failure_details() -> None:
    its = [_iteration("s", passed=False)]

    summary = _summarize("s", its)

    assert summary.failures == ["property failed"]


def test_summarize_treats_a_symptom_error_as_a_failure_with_its_own_message() -> None:
    its = [IterationResult("s", None, None, wall_seconds=5.0, error="never manifested")]

    summary = _summarize("s", its)

    assert summary.n == 1
    assert summary.passed == 0
    assert "never manifested" in summary.failures[0]


def test_summarize_of_zero_iterations_does_not_divide_by_zero() -> None:
    summary = _summarize("s", [])

    assert summary.n == 0
    assert summary.pass_rate == 0.0


# -- report rendering -----------------------------------------------------


def test_report_md_includes_the_table_and_model() -> None:
    report_summaries = [
        ScenarioSummary(
            "bad-image-tag",
            n=5,
            passed=5,
            mean_iterations=6.0,
            mean_cost_usd=0.07,
            p95_wall_seconds=90.0,
        )
    ]
    from evals.runner import SweepReport

    md = render_report_md(
        SweepReport(model="claude-haiku-4-5", summaries=report_summaries, iterations=[])
    )

    assert "claude-haiku-4-5" in md
    assert "bad-image-tag" in md
    assert "5/5" in md


def test_report_md_flags_scenarios_below_50_percent_for_triage() -> None:
    from evals.runner import SweepReport

    summaries = [
        ScenarioSummary(
            "flaky",
            n=4,
            passed=1,
            mean_iterations=8.0,
            mean_cost_usd=0.2,
            p95_wall_seconds=120.0,
            failures=["property X failed: got 6 wanted <4"],
        )
    ]
    md = render_report_md(SweepReport(model="cheap", summaries=summaries, iterations=[]))

    assert "below 50%" in md.lower()
    assert "property X failed" in md


def test_report_json_round_trips_the_numbers() -> None:
    import json

    from evals.runner import SweepReport

    summaries = [
        ScenarioSummary(
            "s", n=3, passed=2, mean_iterations=5.5, mean_cost_usd=0.15, p95_wall_seconds=42.0
        )
    ]
    payload = json.loads(
        render_report_json(SweepReport(model="m", summaries=summaries, iterations=[]))
    )

    assert payload["model"] == "m"
    assert payload["scenarios"][0]["pass_rate"] == pytest.approx(2 / 3)


# -- run_sweep: the reset/break/wait/run/check/reset sequence -------------


class FakeLab:
    def __init__(self, *, timeout_on: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.timeout_on = timeout_on or set()
        self._current_scenario: str | None = None

    def snapshot(self) -> None:
        self.calls.append("snapshot")

    def reset(self) -> None:
        self.calls.append("reset")

    def apply_break(self, patch_path: Path, message: str) -> None:
        self._current_scenario = patch_path.parent.name
        self.calls.append(f"apply_break:{self._current_scenario}")

    def wait_for_symptom(self, probe: SymptomProbe, scope: Scope) -> None:
        if self._current_scenario in self.timeout_on:
            raise SymptomTimeout(f"{self._current_scenario} never manifested")
        self.calls.append("wait_for_symptom")

    def render(self, app: str, namespace: str) -> str:
        return ""

    def read_file(self, rel_path: str) -> str:
        return ""


def _write_scenario(root: Path, name: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "break.patch").write_text("(unused: LabHandle is faked)\n")
    (directory / "scenario.yaml").write_text(
        f"title: {name}\nscope: {{namespace: shop, app: shop-api}}\n"
        f"task_prompt: investigate {name}\nexpected_outcome: pr\n"
        "symptom_probe: {kind: event_reason, value: X, timeout_s: 1, poll_interval_s: 0}\n"
    )
    (directory / "checker.py").write_text(
        "from evals.models import CheckReport\n"
        "def check(result, lab):\n"
        "    return CheckReport(result.success, 'ok' if result.success else 'run failed')\n"
    )


def test_run_sweep_follows_reset_break_wait_run_check_reset_per_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scenario(tmp_path, "s1")
    calls: list[str] = []

    def fake_execute_incident(
        cfg: RunConfig, task: Task, run_id: str, *, llm: LLMClient, read_only: bool, **_: object
    ) -> RunResult:
        calls.append("execute_incident")
        return RunResult(success=True, reason="verified", cost_usd=0.05, iterations=3)

    monkeypatch.setattr("kubemend.cli.execute_incident", fake_execute_incident)
    lab = FakeLab()

    report = run_sweep(
        ["s1"],
        2,
        RunConfig(),
        llm=FakeLLM([]),
        lab=lab,
        scenarios_root=tmp_path,
    )

    # snapshot once, then reset/break/wait/reset per iteration, run in between.
    assert lab.calls[0] == "snapshot"
    per_iteration = lab.calls[1:]
    assert per_iteration == [
        "reset",
        "apply_break:s1",
        "wait_for_symptom",
        "reset",
        "reset",
        "apply_break:s1",
        "wait_for_symptom",
        "reset",
    ]
    assert calls == ["execute_incident", "execute_incident"]
    assert report.summaries[0].n == 2
    assert report.summaries[0].passed == 2


def test_run_sweep_isolates_scenarios_a_broken_one_does_not_abort_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scenario(tmp_path, "flaky")
    _write_scenario(tmp_path, "solid")

    def fake_execute_incident(
        cfg: RunConfig, task: Task, run_id: str, *, llm: LLMClient, read_only: bool, **_: object
    ) -> RunResult:
        return RunResult(success=True, reason="verified", cost_usd=0.05, iterations=3)

    monkeypatch.setattr("kubemend.cli.execute_incident", fake_execute_incident)
    lab = FakeLab(timeout_on={"flaky"})

    report = run_sweep(
        ["flaky", "solid"],
        1,
        RunConfig(),
        llm=FakeLLM([]),
        lab=lab,
        scenarios_root=tmp_path,
    )

    flaky = next(s for s in report.summaries if s.scenario == "flaky")
    solid = next(s for s in report.summaries if s.scenario == "solid")
    assert flaky.passed == 0
    assert "never manifested" in flaky.failures[0]
    assert solid.passed == 1


def test_run_sweep_resets_even_when_the_symptom_never_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck fixture must not leave the gitops repo mid-break for whatever
    scenario runs next."""
    _write_scenario(tmp_path, "flaky")
    monkeypatch.setattr(
        "kubemend.cli.execute_incident", lambda *a, **k: pytest.fail("should not run")
    )
    lab = FakeLab(timeout_on={"flaky"})

    run_sweep(["flaky"], 1, RunConfig(), llm=FakeLLM([]), lab=lab, scenarios_root=tmp_path)

    assert lab.calls == ["snapshot", "reset", "apply_break:flaky", "reset"]


def test_run_sweep_resets_even_when_execute_incident_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncaught exception after the break has been applied (e.g. a backend
    ClientError opening the PR) must not skip lab.reset() — otherwise the
    next iteration's apply_break() fails against a repo that's still
    mid-break rather than the one that broke it."""
    _write_scenario(tmp_path, "s1")

    def fake_execute_incident(*a: object, **k: object) -> RunResult:
        raise RuntimeError("boom")

    monkeypatch.setattr("kubemend.cli.execute_incident", fake_execute_incident)
    lab = FakeLab()

    with pytest.raises(RuntimeError, match="boom"):
        run_sweep(["s1"], 1, RunConfig(), llm=FakeLLM([]), lab=lab, scenarios_root=tmp_path)

    assert lab.calls == ["snapshot", "reset", "apply_break:s1", "wait_for_symptom", "reset"]
