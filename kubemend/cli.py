"""Command-line entrypoint (ARCHITECTURE.md §9).

Exposes `kubemend run | evals | trace replay`. `run` is read-only in M2: the
three read tools are registered, the gitops tools are not, so the loop can only
investigate and hand off. That is a deliberate intermediate state — it exercises
the whole harness against a real cluster before the write path exists.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any

import anthropic
import typer

from kubemend.config import RunConfig, load_config
from kubemend.core.loop import run as run_loop
from kubemend.core.model import RunResult, Scope, Task, Verdict
from kubemend.llm.anthropic_client import AnthropicClient
from kubemend.prompts import render
from kubemend.tools.gitops.backend import GitBackend
from kubemend.tools.gitops.gitea_backend import GiteaBackend
from kubemend.tools.gitops.local_backend import LocalGitBackend
from kubemend.tools.gitops.proposer import Proposer, propose_tool_spec
from kubemend.tools.gitops.reader import (
    GitOpsReader,
    list_gitops_files_spec,
    read_gitops_file_spec,
)
from kubemend.tools.gitops.validator import Validator
from kubemend.tools.kubernetes.api import KubeApiClient
from kubemend.tools.kubernetes.reader import KubernetesReader, k8s_tool_spec
from kubemend.tools.observability.loki import LokiProvider, logs_tool_spec
from kubemend.tools.observability.prometheus import PrometheusProvider, metrics_tool_spec
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder
from kubemend.verify.gate import PipelineGate, validate_tool_spec

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


def build_write_path(
    cfg: RunConfig, scope: Scope, run_id: str, lab_bin: Path
) -> tuple[Proposer, PipelineGate]:
    """Assemble the proposer, validator and gate for a run that may write.

    The binaries come from the Taskfile-managed directory rather than PATH: a
    developer machine here had a helm a full major version ahead of the pinned
    one, which would render different manifests than CI does.
    """
    backend: GitBackend = LocalGitBackend(cfg.gitops.repo_path)
    if cfg.gitops.backend == "gitea":
        token_file = Path(cfg.gitops.gitea_token_file).expanduser()
        if not token_file.exists():
            raise typer.BadParameter(
                f"gitops.backend is 'gitea' but no token at {token_file} — run `task lab:workspace`"
            )
        backend = GiteaBackend(
            cfg.gitops.repo_path,
            api_url=cfg.gitops.gitea_api_url,
            owner=cfg.gitops.gitea_owner,
            repo=cfg.gitops.gitea_repo,
            token=token_file.read_text().strip(),
        )
    proposer = Proposer(
        backend=backend,
        writable_globs=list(cfg.gitops.writable_globs),
        base_branch=cfg.gitops.base_branch,
        run_id=run_id,
    )
    validator = Validator(
        repo_path=Path(cfg.gitops.repo_path).expanduser().resolve(),
        scope=scope,
        helm_bin=lab_bin / "helm",
        kyverno_bin=lab_bin / "kyverno",
        kubectl_bin=lab_bin / "kubectl",
        policies_dir=Path("policies").resolve(),
    )
    return proposer, PipelineGate(proposer=proposer, validator=validator)


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
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Investigate only; register no write path")
    ] = False,
    model: Annotated[
        str, typer.Option("--model", help="Which tier drives agent turns: main | cheap")
    ] = "main",
    config: Annotated[Path, typer.Option("--config")] = Path("kubemend.yaml"),
) -> None:
    """Diagnose an incident and propose a fix (read-only until M3)."""
    cfg = load_config(config)
    if model == "cheap":
        # Point the main tier at the cheap model rather than teaching the loop
        # about a third tier: the loop asks for "main" for agent turns and
        # "cheap" for compaction and handoff, and that split stays meaningful.
        # Same vocabulary the eval runner uses (`--model cheap|main`).
        cfg.model.main = cfg.model.cheap.model_copy(
            update={"max_cost_usd_per_run": cfg.model.main.max_cost_usd_per_run}
        )
    elif model != "main":
        typer.echo(f"--model must be 'main' or 'cheap', got {model!r}", err=True)
        raise typer.Exit(code=2)
    scope = Scope(namespace=namespace, app=app_name)
    incident = Task(statement=task, scope=scope, window=window)
    run_id = uuid.uuid4().hex[:12]
    trace = TraceRecorder.open(Path("traces") / f"{run_id}.jsonl")

    registry = build_read_only_registry(cfg)
    gate: Any = ReadOnlyGate()
    proposer: Proposer | None = None

    workspace = Path(cfg.gitops.repo_path).expanduser()
    if read_only:
        typer.echo("read-only: no write path registered")
    elif not (workspace / ".git").exists():
        # Refusing to guess is better than silently degrading: a run that
        # cannot propose looks identical in the report to one that chose not to.
        typer.echo(
            f"no GitOps workspace at {workspace} — running read-only. "
            "Run `task lab:workspace` to create one.",
            err=True,
        )
    else:
        proposer, gate = build_write_path(cfg, scope, run_id, Path(".lab/bin").resolve())
        # Reads are registered with the write path, not with the read-only
        # tools: without a proposer there is nothing to write and no reason to
        # put chart internals into context.
        reader = GitOpsReader(
            Path(cfg.gitops.repo_path).expanduser().resolve(),
            base_branch=cfg.gitops.base_branch,
        )
        registry.register(read_gitops_file_spec(reader))
        registry.register(list_gitops_files_spec(reader))
        registry.register(propose_tool_spec(proposer))
        registry.register(validate_tool_spec(gate))

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
        registry=registry,
        gate=gate,
        trace=trace,
    )

    if result.success and proposer is not None:
        result.pr_ref = _open_pr(proposer, incident, result, cfg)
    _report(result, model_name=cfg.model.main.name)


def _open_pr(proposer: Proposer, incident: Task, result: RunResult, cfg: RunConfig) -> str | None:
    """Turn a verified proposal into something a human can review.

    The body is generated from the gate's own verdict rather than from anything
    the model said about its work — that check table is the reviewer's evidence.
    """
    verdict = result.verdict
    body = render(
        "pr_body.md.j2",
        rationale=proposer.rationale,
        files=proposer.files_written,
        checks=verdict.checks if verdict else [],
        resources=(verdict.diff_summary.resources if verdict and verdict.diff_summary else []),
        scope=incident.scope,
        writable_globs=list(cfg.gitops.writable_globs),
        incident_ref=proposer.incident_ref,
    )
    title = (
        f"kubemend: {proposer.rationale.splitlines()[0][:60]}"
        if proposer.rationale
        else "kubemend: proposed fix"
    )
    pr = proposer.open_pr(title, body)
    return pr.url if pr else None


def _report(result: RunResult, *, model_name: str = "") -> None:
    if model_name:
        typer.echo(f"\nmodel:      {model_name}")
    typer.echo(f"reason:     {result.reason}")
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
