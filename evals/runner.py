"""N-run sweeps and reports (ARCHITECTURE.md §7).

Runs each scenario N times through the reset -> break -> wait-for-symptom ->
agent run -> checker -> reset protocol, then emits report.md and report.json
with pass rate, mean iterations, mean cost, and p95 wall per scenario.

Non-determinism is the point: no conclusion is drawn from n=1. See
docs/knowledge/lab-and-evals.md for the triage rules — a scenario below 50%
gets a written diagnosis before anyone touches a prompt.
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from evals.lab import KubeQuery, Lab, LabHandle, LogSearch, SymptomTimeout
from evals.models import CheckReport, ScenarioSpec
from evals.scenario import SCENARIOS_ROOT, CheckerFn, list_scenarios, load_scenario
from kubemend.config import RunConfig, load_config
from kubemend.core.model import RunResult, Task
from kubemend.llm.client import LLMClient

evals_app = typer.Typer(help="Scenario sweeps against the lab.", no_args_is_help=True)


@dataclass(frozen=True)
class IterationResult:
    scenario: str
    run_result: RunResult | None
    check: CheckReport | None
    wall_seconds: float
    error: str | None = None  # symptom never manifested / git op failed — no run happened

    @property
    def passed(self) -> bool:
        return self.check is not None and self.check.passed


@dataclass(frozen=True)
class ScenarioSummary:
    scenario: str
    n: int
    passed: int
    mean_iterations: float
    mean_cost_usd: float
    p95_wall_seconds: float
    failures: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.n if self.n else 0.0


@dataclass(frozen=True)
class SweepReport:
    model: str
    summaries: list[ScenarioSummary]
    iterations: list[IterationResult]


def run_sweep(
    scenario_names: list[str],
    n: int,
    cfg: RunConfig,
    *,
    llm: LLMClient,
    lab: Lab,
    lab_bin: Path = Path(".lab/bin"),
    trace_dir: Path = Path("traces"),
    scenarios_root: Path = SCENARIOS_ROOT,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda _msg: None,
) -> SweepReport:
    """Drive every (scenario, iteration) pair through the runner protocol.

    A fresh known-good snapshot is taken once, up front — every iteration of
    every scenario resets to that same commit, so a leftover from one
    iteration can never leak into the next.
    """
    lab.snapshot()
    iterations: list[IterationResult] = []
    for name in scenario_names:
        spec, checker = load_scenario(name, scenarios_root)
        for i in range(n):
            log(f"{name} [{i + 1}/{n}]")
            iterations.append(
                _run_one(
                    spec,
                    checker,
                    cfg,
                    llm=llm,
                    lab=lab,
                    lab_bin=lab_bin,
                    trace_dir=trace_dir,
                    scenarios_root=scenarios_root,
                    clock=clock,
                )
            )
    summaries = [
        _summarize(name, [it for it in iterations if it.scenario == name])
        for name in scenario_names
    ]
    return SweepReport(model=cfg.model.main.name, summaries=summaries, iterations=iterations)


def _run_one(
    spec: ScenarioSpec,
    checker: CheckerFn,
    cfg: RunConfig,
    *,
    llm: LLMClient,
    lab: Lab,
    lab_bin: Path,
    trace_dir: Path,
    scenarios_root: Path,
    clock: Callable[[], float],
) -> IterationResult:
    # Local import: execute_incident lives in cli.py, and cli.py imports this
    # module's evals_app — importing at call time avoids a circular import
    # while keeping the CLI wiring the sole place tools get assembled.
    from kubemend.cli import execute_incident

    started = clock()
    try:
        lab.reset()
        lab.apply_break(scenarios_root / spec.name / "break.patch", f"break: inject {spec.name}")
        lab.wait_for_symptom(spec.symptom_probe, spec.scope)
    except (SymptomTimeout, RuntimeError) as exc:
        lab.reset()
        return IterationResult(spec.name, None, None, clock() - started, error=str(exc))

    # Timestamp prefix makes traces/ and `kubemend/<run_id>` branches sortable
    # and identifiable at a glance; the hex suffix keeps same-second runs unique.
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    task = Task(statement=spec.task_prompt, scope=spec.scope)
    result = execute_incident(
        cfg, task, run_id, llm=llm, read_only=False, lab_bin=lab_bin, trace_dir=trace_dir
    )
    check = checker(result, lab)
    wall = clock() - started
    lab.reset()
    return IterationResult(spec.name, result, check, wall)


def _summarize(name: str, its: list[IterationResult]) -> ScenarioSummary:
    n = len(its)
    passed = sum(1 for it in its if it.passed)
    completed = [it for it in its if it.run_result is not None]
    mean_iterations = (
        statistics.fmean(it.run_result.iterations for it in completed if it.run_result)
        if completed
        else 0.0
    )
    mean_cost = (
        statistics.fmean(it.run_result.cost_usd for it in completed if it.run_result)
        if completed
        else 0.0
    )
    wall_values = [it.wall_seconds for it in its]
    failures = []
    for it in its:
        if it.error:
            failures.append(f"symptom/harness error: {it.error}")
        elif it.check is not None and not it.check.passed:
            failures.append(it.check.detail)
    return ScenarioSummary(
        scenario=name,
        n=n,
        passed=passed,
        mean_iterations=mean_iterations,
        mean_cost_usd=mean_cost,
        p95_wall_seconds=_p95(wall_values),
        failures=failures,
    )


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    return ordered[index]


# -- report rendering -------------------------------------------------------


def render_report_md(report: SweepReport) -> str:
    lines = [
        "# Eval sweep report",
        "",
        f"model: {report.model}",
        "",
        "| scenario | pass | iters (avg) | cost (avg) | p95 wall |",
        "|---|---|---|---|---|",
    ]
    for s in report.summaries:
        lines.append(
            f"| {s.scenario} | {s.passed}/{s.n} | {s.mean_iterations:.1f} | "
            f"${s.mean_cost_usd:.2f} | {s.p95_wall_seconds:.0f}s |"
        )
    triage = [s for s in report.summaries if s.n and s.pass_rate < 0.5]
    if triage:
        lines += ["", "## Below 50% — needs a written diagnosis before any prompt change", ""]
        for s in triage:
            lines.append(f"### {s.scenario} ({s.passed}/{s.n})")
            for failure in s.failures[:5]:
                lines.append(f"- {failure}")
            lines.append("")
    return "\n".join(lines) + "\n"


def render_report_json(report: SweepReport) -> str:
    payload = {
        "model": report.model,
        "scenarios": [
            {
                "scenario": s.scenario,
                "n": s.n,
                "passed": s.passed,
                "pass_rate": s.pass_rate,
                "mean_iterations": s.mean_iterations,
                "mean_cost_usd": s.mean_cost_usd,
                "p95_wall_seconds": s.p95_wall_seconds,
                "failures": s.failures,
            }
            for s in report.summaries
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# -- CLI ----------------------------------------------------------------


def _build_lab(cfg: RunConfig) -> LabHandle:
    from kubemend.tools.kubernetes.api import KubeApiClient
    from kubemend.tools.observability.loki import LokiProvider

    kube: KubeQuery = KubeApiClient(
        cfg.kubernetes.kubeconfig, context=cfg.kubernetes.context or None
    )
    loki: LogSearch = LokiProvider(cfg.observability.loki_url)
    return LabHandle(
        workspace=Path(cfg.gitops.repo_path).expanduser().resolve(),
        base_branch=cfg.gitops.base_branch,
        kube=kube,
        loki=loki,
        helm_bin=(Path(".lab/bin") / "helm").resolve(),
    )


@evals_app.command("run")
def run(
    scenarios: Annotated[
        str,
        typer.Option("--scenarios", "-s", help="Comma-separated scenario names, or 'all'"),
    ] = "all",
    n: Annotated[int, typer.Option("-n", "--runs", help="Iterations per scenario")] = 5,
    model: Annotated[str, typer.Option("--model", help="main | cheap")] = "cheap",
    config: Annotated[Path, typer.Option("--config")] = Path("kubemend.yaml"),
    report_dir: Annotated[
        Path, typer.Option("--report-dir", help="Where to write report.md/report.json")
    ] = Path("evals/reports/latest"),
) -> None:
    """Run scenario sweeps and emit the pass-rate report."""
    import anthropic

    from kubemend.cli import resolve_model_tier
    from kubemend.llm.anthropic_client import AnthropicClient

    cfg = load_config(config)
    try:
        cfg = resolve_model_tier(cfg, model)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    workspace = Path(cfg.gitops.repo_path).expanduser()
    if not (workspace / ".git").exists():
        typer.echo(
            f"no GitOps workspace at {workspace} — run `task lab:workspace` first. "
            "A sweep cannot degrade to read-only: every iteration would end in a "
            "handoff and the report would say nothing about the harness.",
            err=True,
        )
        raise typer.Exit(code=1)

    available = list_scenarios()
    names = available if scenarios == "all" else [s.strip() for s in scenarios.split(",")]
    unknown = [name for name in names if name not in available]
    if unknown:
        typer.echo(f"unknown scenario(s): {unknown}; available: {available}", err=True)
        raise typer.Exit(code=2)
    if not names:
        typer.echo("no scenarios to run", err=True)
        raise typer.Exit(code=1)

    try:
        llm = AnthropicClient(cfg)
    except anthropic.AnthropicError as exc:
        typer.echo(f"could not authenticate to the Anthropic API: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    lab = _build_lab(cfg)
    report = run_sweep(names, n, cfg, llm=llm, lab=lab, log=lambda msg: typer.echo(f"-> {msg}"))

    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.md").write_text(render_report_md(report))
    (report_dir / "report.json").write_text(render_report_json(report))

    typer.echo("")
    typer.echo(render_report_md(report))
    typer.echo(f"report written to {report_dir}/")
