"""Command-line entrypoint (ARCHITECTURE.md §9).

Exposes `kubemend run | evals | trace replay`. `run` is read-only in M2: the
three read tools are registered, the gitops tools are not, so the loop can only
investigate and hand off. That is a deliberate intermediate state — it exercises
the whole harness against a real cluster before the write path exists.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

import anthropic
import typer

from kubemend.config import RunConfig, load_config
from kubemend.core.loop import run as run_loop
from kubemend.core.model import RunResult, Scope, Task, Verdict
from kubemend.llm.anthropic_client import AnthropicClient
from kubemend.tools.kubernetes.api import KubeApiClient
from kubemend.tools.kubernetes.reader import KubernetesReader, k8s_tool_spec
from kubemend.tools.observability.loki import LokiProvider, logs_tool_spec
from kubemend.tools.observability.prometheus import PrometheusProvider, metrics_tool_spec
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder

app = typer.Typer(
    name="kubemend",
    help="Diagnose Kubernetes incidents and propose GitOps fixes as draft PRs.",
    no_args_is_help=True,
)

trace_app = typer.Typer(help="Inspect the JSONL trace a run writes.", no_args_is_help=True)
app.add_typer(trace_app, name="trace")


class ReadOnlyGate:
    """Stands in for the verification gate until M3 builds the real one.

    It always fails, and that is correct rather than a placeholder cop-out: with
    no write path registered there is no proposal to verify, so a run can only
    legitimately end in a handoff. Returning `passed=True` here would fake
    exactly the trusted-self-report that I1 exists to prevent.
    """

    def verify(self) -> Verdict:
        from kubemend.core.model import CheckResult

        return Verdict(
            passed=False,
            checks=[
                CheckResult(
                    name="write_path",
                    passed=False,
                    detail=(
                        "No GitOps tools are registered in this build, so no change "
                        "was proposed and there is nothing to verify. Summarise your "
                        "findings instead."
                    ),
                )
            ],
        )


def build_read_only_registry(cfg: RunConfig) -> ToolRegistry:
    prometheus = PrometheusProvider(cfg.observability.prometheus_url)
    loki = LokiProvider(cfg.observability.loki_url)
    reader = KubernetesReader(
        KubeApiClient(cfg.kubernetes.kubeconfig, context=cfg.kubernetes.context or None)
    )
    return ToolRegistry(
        [
            metrics_tool_spec(prometheus),
            logs_tool_spec(loki),
            k8s_tool_spec(reader),
        ],
        result_token_cap=cfg.context.result_token_cap,
        retry_backoff_s=0.5,
    )


@app.command()
def run(
    task: Annotated[
        str, typer.Option("--task", help="What happened, as an on-call human would say it")
    ],
    namespace: Annotated[
        str, typer.Option("--namespace", help="Namespace the incident is scoped to")
    ],
    app_name: Annotated[str, typer.Option("--app", help="Application the incident is scoped to")],
    window: Annotated[str, typer.Option("--window", help="Time window of interest")] = "-30m",
    config: Annotated[Path, typer.Option("--config")] = Path("kubemend.yaml"),
) -> None:
    """Diagnose an incident and propose a fix (read-only until M3)."""
    cfg = load_config(config)
    incident = Task(statement=task, scope=Scope(namespace=namespace, app=app_name), window=window)
    trace = TraceRecorder.open(Path("traces") / f"{uuid.uuid4().hex[:12]}.jsonl")

    # Deliberately no ANTHROPIC_API_KEY precondition. The SDK resolves
    # credentials from several sources in order — the env var, an
    # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile on disk — so an unset
    # env var does not mean the user has no credentials. Let the SDK decide and
    # translate its refusal into something actionable.
    try:
        llm = AnthropicClient(cfg)
    except anthropic.AnthropicError as exc:
        typer.echo(
            f"could not authenticate to the Anthropic API: {exc}\n"
            "Set ANTHROPIC_API_KEY, or run `ant auth login` to store a profile.",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    result = run_loop(
        incident,
        cfg,
        llm=llm,
        registry=build_read_only_registry(cfg),
        gate=ReadOnlyGate(),
        trace=trace,
    )
    _report(result)


def _report(result: RunResult) -> None:
    typer.echo(f"\nreason:     {result.reason}")
    typer.echo(f"iterations: {result.iterations}")
    typer.echo(f"cost:       ${result.cost_usd:.4f}")
    typer.echo(f"trace:      {result.trace_path}")
    if result.handoff is None:
        return
    typer.echo("\n--- handoff ---")
    for hypothesis in result.handoff.root_cause_hypotheses:
        typer.echo(f"  [{hypothesis.confidence:.0%}] {hypothesis.statement}")
        for evidence in hypothesis.evidence:
            typer.echo(f"        evidence: {evidence}")
    if result.handoff.what_was_ruled_out:
        typer.echo("  ruled out:")
        for item in result.handoff.what_was_ruled_out:
            typer.echo(f"    - {item}")
    if result.handoff.suggested_next_steps:
        typer.echo("  next steps:")
        for item in result.handoff.suggested_next_steps:
            typer.echo(f"    - {item}")
    if result.handoff.blocking_reason:
        typer.echo(f"  blocked by: {result.handoff.blocking_reason}")


@app.command()
def evals() -> None:
    """Run scenario sweeps and emit the pass-rate report."""
    typer.echo("`kubemend evals` is implemented in M4 (see IMPLEMENTATION_PLAN.md).", err=True)
    raise typer.Exit(code=1)


@trace_app.command()
def replay() -> None:
    """Reconstruct a run's event sequence from its trace."""
    typer.echo("`kubemend trace replay` is implemented in M4.", err=True)
    raise typer.Exit(code=1)
